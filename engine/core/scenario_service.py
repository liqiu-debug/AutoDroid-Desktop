# 来源：语义对齐 AutoDroid backend/api/scenarios.py（run_scenario_api / websocket_run_scenario /
#       precheck_scenario_execution）+ backend/scenario_execution.py 场景执行编排。
"""场景批量执行本地化（客户端模式：设备 I/O 全本地，结果上报云端 client-runs kind=scenario）。

WS 消息协议逐字段对齐 backend/api/scenarios.py + backend/socket_manager.py：
- log          {type, level: info|success|warning|error, message, timestamp[, attachment, attachment_type]}
- run_start    {type, status: "RUNNING", scenario_id, execution_id, batch_id, device_serial,
                total_steps, timestamp}
- run_complete {type, success, status: PASS|FAIL|WARNING|ABORTED|ERROR, summary, report_id, execution_id}

前端消费面（frontend/src/views/scenarios/ScenarioEditor.vue）只读
log.level/message/timestamp、run_start.batch_id/execution_id/device_serial、
run_complete.success/status/report_id——零改动。

上报：单场景单设备一条 client-runs（kind=scenario），逐 case 批次 steps（键含 alias，
同一 case 多 alias 分开成条，平台 _case_state_key 已支持），finish 一次收尾。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from engine.core import recording_service, run_registry
from engine.core.cloud_bridge import CloudBridge
from engine.core.execution_service import (
    collect_case_variables_map,
    prepare_case_steps_for_platform,
    precheck_case_execution,
    resolve_device_platform,
)
from engine.core.run_registry import LocalRunRecord
from engine.core.runner_service import (
    LocalRunContext,
    build_precheck_payload,
    execute_prepared_steps,
)
from engine.core.step_contract import normalize_error_strategy

logger = logging.getLogger(__name__)

Emit = Callable[[Dict[str, Any]], Awaitable[None]]


def _now_iso() -> str:
    return datetime.now().isoformat()


# --------------------------------------------------------------------------- #
#  WS 日志协议（形状对齐 backend/socket_manager.py broadcast_log）
# --------------------------------------------------------------------------- #

async def emit_log(emit: Emit, level: str, message: str, attachment: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {
        "type": "log",
        "level": level,  # info, success, warning, error
        "message": message,
        "timestamp": _now_iso(),
    }
    if attachment:
        payload["attachment"] = attachment
        payload["attachment_type"] = "image"
    await emit(payload)


def scenario_step_log_emit(emit: Emit) -> Emit:
    """把执行核心的 step_update 消息翻译为场景 WS 的 log 消息
    （对齐 scenarios._broadcast_ws_scenario_event 的 step_result 文案/级别）。"""

    async def _adapted(message: Dict[str, Any]) -> None:
        if message.get("type") != "step_update":
            await emit(message)
            return
        status = str(message.get("status") or "")
        text = str(message.get("log") or "")
        if status == "running":
            return  # 平台场景链路不推送步骤开始
        if status == "success":
            await emit_log(emit, "success", f"    ✓ 成功: {text}")
        elif status == "skipped":
            await emit_log(emit, "info", f"    ↷ 跳过: {text}")
        elif status == "warning":
            await emit_log(emit, "warning", f"    🟡 忽略错误: {message.get('error') or text}")
        elif status == "failed":
            await emit_log(emit, "error", f"    ❌ 失败: {message.get('error') or text}")
        else:
            await emit_log(emit, "info", text)

    return _adapted


async def emit_case_outcome(emit: Emit, case_status: str, duration: float, attachment: Optional[str] = None) -> None:
    """用例收尾日志（对齐 scenarios._broadcast_ws_case_outcome）。"""
    if case_status == "success":
        await emit_log(emit, "success", f"  ✓ 通过 (耗时 {duration:.2f}s)")
    elif case_status == "warning":
        await emit_log(emit, "warning", f"  ⚠ 警告 (耗时 {duration:.2f}s)")
    elif case_status == "skipped":
        await emit_log(emit, "info", f"  ↷ 全部跳过 (耗时 {duration:.2f}s)")
    else:
        await emit_log(emit, "error", f"  ✗ 失败 (耗时 {duration:.2f}s)", attachment=attachment)


# --------------------------------------------------------------------------- #
#  预检（形状对齐 scenarios.precheck_scenario_execution）
# --------------------------------------------------------------------------- #

def summarize_precheck_failure(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return "precheck failed"
    for item in payload.get("cases", []) or []:
        if isinstance(item, dict) and item.get("status") == "FAIL":
            name = item.get("alias") or item.get("case_name") or f"Case#{item.get('case_id')}"
            return f"{name}: {item.get('reason') or '用例预检失败'}"
    for item in payload.get("global_checks", []) or []:
        if isinstance(item, dict) and item.get("status") == "FAIL":
            return str(item.get("message") or item.get("code") or "global precheck failed")
    for item in payload.get("steps", []) or []:
        if isinstance(item, dict) and item.get("status") == "FAIL":
            return str(item.get("message") or item.get("code") or "step precheck failed")
    if payload.get("has_runnable_cases") is False or payload.get("has_runnable_steps") is False:
        return "all steps would be skipped on this device"
    return "precheck failed"


def _case_check_status(check: Dict[str, Any]) -> str:
    summary = check.get("summary") or {}
    global_fail_count = int(summary.get("global_fail") or 0)
    fail_count = int(summary.get("fail") or 0)
    pass_count = int(summary.get("pass") or 0)
    skip_count = int(summary.get("skip") or 0)
    all_skipped = skip_count > 0 and pass_count == 0 and fail_count == 0 and global_fail_count == 0
    if global_fail_count > 0 or fail_count > 0:
        return "FAIL"
    if all_skipped:
        return "SKIP"
    if check.get("ok"):
        return "PASS"
    return "WARNING"


def precheck_scenario_for_device(
    bridge: CloudBridge,
    scenario_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
) -> Dict[str, Any]:
    """场景本地预检：逐 case 预检 + 变量链（对齐 scenarios.precheck_scenario_execution）。"""
    scenario = bridge.fetch_scenario(scenario_id)
    steps = bridge.fetch_scenario_steps(scenario_id)

    scenario_context: Dict[str, str] = dict(bridge.fetch_env_variables(env_id) or {})
    mapping = bridge.fetch_app_key_mapping()

    case_checks: List[Dict[str, Any]] = []
    has_runnable_cases = False
    fail_cases = 0
    warning_cases = 0
    skipped_cases = 0
    pass_cases = 0

    for scenario_step in steps:
        case_id = scenario_step.get("case_id")
        try:
            case = bridge.fetch_case(case_id)
            step_rows = bridge.fetch_case_steps(case_id)
        except Exception:
            case = None
            step_rows = []
        if not case:
            fail_cases += 1
            case_checks.append(
                {
                    "step_order": scenario_step.get("order"),
                    "scenario_step_id": scenario_step.get("id"),
                    "alias": scenario_step.get("alias"),
                    "case_id": case_id,
                    "case_name": "Unknown",
                    "status": "FAIL",
                    "ok": False,
                    "reason": f"用例不存在: {case_id}",
                    "summary": {"pass": 0, "skip": 0, "fail": 1, "global_fail": 0, "total": 1},
                    "global_checks": [],
                    "steps": [
                        {
                            "order": 0,
                            "action": "system",
                            "status": "FAIL",
                            "code": "CASE_NOT_FOUND",
                            "message": f"用例不存在: {case_id}",
                        }
                    ],
                }
            )
            continue

        variables_map = collect_case_variables_map((case or {}).get("variables"), scenario_context)
        local = precheck_case_execution(
            step_rows,
            device_serial,
            variables_map=variables_map,
            mapping=mapping,
            legacy_steps=(case or {}).get("steps"),
        )
        check = build_precheck_payload(case.get("id") or case_id, device_serial, local, variables_map)

        if check.get("has_runnable_steps"):
            has_runnable_cases = True

        case_status = _case_check_status(check)
        if case_status == "FAIL":
            fail_cases += 1
        elif case_status == "SKIP":
            skipped_cases += 1
        elif case_status == "PASS":
            pass_cases += 1
        else:
            warning_cases += 1

        case_checks.append(
            {
                "step_order": scenario_step.get("order"),
                "scenario_step_id": scenario_step.get("id"),
                "alias": scenario_step.get("alias"),
                "case_id": check.get("case_id"),
                "case_name": str((case or {}).get("name") or "Unknown"),
                "status": case_status,
                "ok": bool(check.get("ok")),
                "has_runnable_steps": bool(check.get("has_runnable_steps")),
                "summary": check.get("summary"),
                "global_checks": check.get("global_checks") or [],
                "steps": check.get("steps") or [],
                "reason": summarize_precheck_failure(check) if not check.get("ok") else None,
            }
        )

        exported = check.get("exported_variables")
        if isinstance(exported, dict):
            scenario_context.update({str(k): v for k, v in exported.items() if k})

    total_cases = len(case_checks)
    all_cases_skipped = total_cases > 0 and skipped_cases == total_cases
    ok = fail_cases == 0 and has_runnable_cases

    return {
        "scenario_id": int(scenario_id),
        "scenario_name": str((scenario or {}).get("name") or ""),
        "device_serial": device_serial,
        "ok": ok,
        "has_runnable_cases": has_runnable_cases,
        "summary": {
            "total_cases": total_cases,
            "pass_cases": pass_cases,
            "warning_cases": warning_cases,
            "skip_cases": skipped_cases,
            "fail_cases": fail_cases,
            "all_cases_skipped": all_cases_skipped,
        },
        "cases": case_checks,
    }


# --------------------------------------------------------------------------- #
#  执行核心（单场景单设备）
# --------------------------------------------------------------------------- #

def _case_ui_status(step_entries: List[Dict[str, Any]], success: bool) -> str:
    if any(entry.get("report_status") == "FAIL" for entry in step_entries):
        return "failed"
    if step_entries and all(entry.get("report_status") == "SKIP" for entry in step_entries):
        return "skipped"
    if any(entry.get("report_status") == "WARNING" for entry in step_entries):
        return "warning"
    return "success" if success else "failed"


def _last_failure_strategy(step_entries: List[Dict[str, Any]]) -> str:
    for entry in reversed(step_entries):
        if entry.get("report_status") == "FAIL":
            try:
                return normalize_error_strategy(entry.get("error_strategy", "ABORT"))
            except ValueError:
                return "ABORT"
    return "ABORT"


def _summary_message(total_duration: float, case_statuses: List[str], aborted: bool) -> str:
    """对齐 scenario_results._build_scenario_summary_message 文案。"""
    if aborted:
        return "执行已被用户终止"
    success_count = sum(1 for status in case_statuses if status == "success")
    warning_count = sum(1 for status in case_statuses if status == "warning")
    skipped_count = sum(1 for status in case_statuses if status == "skipped")
    fail_count = sum(1 for status in case_statuses if status == "failed")
    return (
        f"🏁 执行结束: 总耗时 {total_duration:.2f}s | 通过 {success_count} | 警告 {warning_count} | "
        f"跳过 {skipped_count} | 失败 {fail_count}"
    )


def _overall_status(case_statuses: List[str], aborted: bool) -> str:
    """对齐 scenario_results._summarize_cases_results 聚合口径。"""
    if aborted:
        return "ABORTED"
    if any(status == "failed" for status in case_statuses):
        return "FAIL"
    if any(status == "warning" for status in case_statuses) or (
        bool(case_statuses) and all(status == "skipped" for status in case_statuses)
    ):
        return "WARNING"
    return "PASS"


async def run_scenario_local(
    *,
    scenario_id: int,
    env_id: Optional[int],
    device_serial: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    emit: Emit,
    bridge: CloudBridge,
    notify: bool = False,
    abort_event: Optional[threading.Event] = None,
    preset_run_key: Optional[Any] = None,
    run_name: Optional[str] = None,
) -> Dict[str, Any]:
    """本地执行场景（单设备）：逐 case 执行 + kind=scenario 上报 + WS 消息。

    生命周期登记到本地 run registry（kind=scenario，供 /api/runs/active 与 cancel）。
    run_name：云端执行记录展示名（定时任务触发为「定时任务: <任务名>」）；缺省场景名。
    """
    run_id = run_id or str(uuid.uuid4())
    batch_id = batch_id or str(uuid.uuid4())
    record = run_registry.registry.register(
        LocalRunRecord(
            kind="scenario",
            target_id=int(scenario_id),
            run_id=run_id,
            batch_id=batch_id,
            device_serial=device_serial,
        )
    )
    if abort_event is None:
        abort_event = record.abort_event

    loop = asyncio.get_running_loop()

    def _blocking(func, *args, **kwargs):
        return loop.run_in_executor(None, lambda: func(*args, **kwargs))

    started_at = time.time()
    case_statuses: List[str] = []
    execution_id: Optional[Any] = None
    scenario_context: Dict[str, str] = {}
    context = LocalRunContext(
        case_id=None,
        device_serial=device_serial,
        env_id=env_id,
        run_id=run_id,
        batch_id=batch_id,
        emit=emit,
        bridge=bridge,
        kind="scenario",
        scenario_id=scenario_id,
    )

    try:
        try:
            scenario = await _blocking(bridge.fetch_scenario, scenario_id)
            scenario_steps = await _blocking(bridge.fetch_scenario_steps, scenario_id)
            env_variables = await _blocking(bridge.fetch_env_variables, env_id)
            mapping = await _blocking(bridge.fetch_app_key_mapping)
        except Exception as exc:
            message = f"拉取场景数据失败: {exc}"
            await emit_log(emit, "error", f"❌ {message}")
            # T2：批量预创建（preset_run_key）的云端执行记录已存在，而 run_key 的赋值
            # 在拉取成功之后才发生——失败分支必须先回填 preset_run_key 再 finish，
            # 否则 TestExecution 永远停留 RUNNING（S5 收尾口径）。
            if context.run_key is None and preset_run_key is not None:
                context.run_key = preset_run_key
                run_registry.registry.update_execution_id(run_id, preset_run_key)
            finish_key = context.run_key
            if finish_key is not None:
                await _blocking(context.finish, False, False, 0, notify, [message], status="ERROR")
            await emit(
                {
                    "type": "run_complete",
                    "success": False,
                    "status": "ERROR",
                    "summary": message,
                    "report_id": None,
                    "execution_id": finish_key if isinstance(finish_key, int) else None,
                }
            )
            return {"success": False, "status": "ERROR", "error": message}

        scenario_name = str((scenario or {}).get("name") or f"scenario-{scenario_id}")
        platform = resolve_device_platform(device_serial)
        total_steps = len(scenario_steps)

        if preset_run_key is not None:
            context.run_key = preset_run_key
            execution_id = preset_run_key if isinstance(preset_run_key, int) else None
            # S1：批量预创建的 execution_id 回填本地登记（供 /api/runs/active + 前端 terminate）
            run_registry.registry.update_execution_id(run_id, preset_run_key)
        else:
            await _blocking(context.create_cloud_run, run_name or scenario_name, platform)
            execution_id = context.run_key if isinstance(context.run_key, int) else None

        await emit_log(emit, "info", f"🎬 开始执行场景: {scenario_name} (共 {total_steps} 个步骤)")
        await emit_log(emit, "info", "🧠 使用跨端执行引擎")
        await emit(
            {
                "type": "run_start",
                "status": "RUNNING",
                "scenario_id": int(scenario_id),
                "execution_id": execution_id,
                "batch_id": batch_id,
                "device_serial": device_serial,
                "total_steps": total_steps,
                "timestamp": _now_iso(),
            }
        )

        scenario_context = {
            str(k): v for k, v in (env_variables or {}).items() if k
        }
        step_emit = scenario_step_log_emit(emit)

        for index, scenario_step in enumerate(scenario_steps):
            if abort_event.is_set():
                await emit_log(emit, "warning", "⚠️ 收到中止信号，停止执行")
                break

            alias = scenario_step.get("alias") or f"Step {index + 1}"
            case_id = scenario_step.get("case_id")
            fetch_error: Optional[str] = None
            try:
                case = await _blocking(bridge.fetch_case, case_id)
                step_rows = await _blocking(bridge.fetch_case_steps, case_id)
            except Exception as exc:
                case = None
                step_rows = []
                fetch_error = str(exc)
                logger.warning("场景步骤取用例失败: scenario=%s case=%s error=%s", scenario_id, case_id, exc)

            if not case:
                # S3：缺失用例 / 取用例瞬时失败记失败后 continue（对齐平台
                # scenario_execution.py 缺失用例 continue 语义），不再 break 终止整场。
                if fetch_error and "404" not in fetch_error:
                    reason = f"拉取用例失败 (ID: {case_id}): {fetch_error}，跳过继续"
                else:
                    reason = f"用例不存在 (ID: {case_id})，跳过"
                await emit_log(emit, "warning", f"⚠️ 步骤 {index + 1} ({alias}): {reason}")
                await emit_case_outcome(emit, "failed", 0.0)
                case_statuses.append("failed")
                continue

            case_name = str(case.get("name") or f"case-{case_id}")
            await emit_log(
                emit,
                "info",
                f"👉 [{index + 1}/{total_steps}] 执行: {alias} ({case_name})",
            )

            variables_map = collect_case_variables_map(case.get("variables"), scenario_context)
            try:
                prepared_steps, variables_map = await _blocking(
                    prepare_case_steps_for_platform,
                    step_rows,
                    platform,
                    variables_map,
                    mapping,
                    case.get("steps"),
                )
                for step_index, step_item in enumerate(prepared_steps):
                    prepared_steps[step_index] = await _blocking(
                        recording_service.localize_step_images, step_item, bridge.ensure_template_local
                    )
            except Exception as exc:
                await emit_log(emit, "error", f"    ❌ 异常: {exc}")
                await emit_case_outcome(emit, "failed", 0.0)
                case_statuses.append("failed")
                break

            outcome = await execute_prepared_steps(
                prepared_steps,
                platform=platform,
                device_serial=device_serial,
                emit=step_emit,
                abort_event=abort_event,
            )
            if outcome.get("error"):
                await emit_log(emit, "error", f"    ❌ 异常: {outcome['error']}")
                await emit_case_outcome(emit, "failed", 0.0)
                case_statuses.append("failed")
                break

            step_entries = outcome["step_entries"]
            case_success = outcome["success"]
            runtime_variables = outcome.get("runtime_variables") or {}
            exported = {str(k): v for k, v in (variables_map or {}).items()}
            exported.update({str(k): v for k, v in runtime_variables.items()})

            # 上报（键含 alias：同一 case 多 alias 分条；success 恒显式）
            await _blocking(
                context.report_step_results,
                case_name,
                case_success,
                step_entries,
                exported,
                case_id=case.get("id") or case_id,
                alias=scenario_step.get("alias"),
            )

            scenario_context.update(exported)

            case_status = _case_ui_status(step_entries, case_success)
            case_statuses.append(case_status)
            attachment = None
            for entry in reversed(step_entries):
                if entry.get("report_status") == "FAIL" and entry.get("screenshot_base64"):
                    attachment = entry.get("screenshot_base64")
                    break
            await emit_case_outcome(emit, case_status, float(outcome.get("duration") or 0), attachment)

            if outcome.get("aborted"):
                await emit_log(emit, "warning", "⚠️ 收到中止信号，停止执行")
                break

            if not case_success:
                if _last_failure_strategy(step_entries) != "CONTINUE":
                    break

        total_duration = time.time() - started_at
        aborted = bool(abort_event.is_set())
        status = _overall_status(case_statuses, aborted)
        success = status == "PASS"
        summary_msg = _summary_message(total_duration, case_statuses, aborted)

        if context.run_key is not None:
            await emit_log(emit, "info", "📊 报告生成中")
        report_id = await _blocking(
            context.finish,
            success,
            aborted,
            total_duration,
            notify,
            ["执行已被用户终止"] if aborted else None,
            scenario_context,
            # S2：聚合状态（含 WARNING）显式透传——_overall_status 的 WARNING
            # 此前被 (success,aborted) 映射压成 FAIL/PASS。
            status=status,
        )
        if report_id:
            await emit_log(emit, "success", f"📊 报告已生成: {report_id}")
        await emit_log(emit, "success" if status == "PASS" else "warning", summary_msg)
        await emit(
            {
                "type": "run_complete",
                "success": success,
                "status": status,
                "summary": summary_msg,
                "report_id": report_id,
                "execution_id": execution_id,
            }
        )
        return {
            "success": success,
            "status": status,
            "report_id": report_id,
            "case_statuses": case_statuses,
            "total_duration": round(total_duration, 2),
            "run_id": run_id,
            "batch_id": batch_id,
        }
    except Exception as exc:
        # S5：create_cloud_run 后的意外异常也必须收尾——run_complete{status:ERROR}
        # + finish(status=ERROR)，否则云端 TestExecution 停留 RUNNING。
        message = f"场景执行异常: {exc}"
        logger.exception("场景本地执行异常: scenario=%s device=%s", scenario_id, device_serial)
        await emit_log(emit, "error", f"❌ {message}")
        try:
            report_id = await _blocking(
                context.finish,
                False,
                False,
                max(0.0, time.time() - started_at),
                notify,
                [message],
                scenario_context,
                status="ERROR",
            )
        except Exception:
            logger.exception("场景异常收尾上报失败: scenario=%s", scenario_id)
            report_id = None
        await emit(
            {
                "type": "run_complete",
                "success": False,
                "status": "ERROR",
                "summary": message,
                "report_id": report_id,
                "execution_id": execution_id,
            }
        )
        return {
            "success": False,
            "status": "ERROR",
            "error": message,
            "run_id": run_id,
            "batch_id": batch_id,
        }
    finally:
        run_registry.registry.complete(
            run_id, "ABORTED" if (abort_event is not None and abort_event.is_set()) else None
        )


# --------------------------------------------------------------------------- #
#  HTTP/WS 驱动入口
# --------------------------------------------------------------------------- #

def start_background_scenario_run(
    *,
    scenario_id: int,
    env_id: Optional[int],
    device_serial: str,
    run_id: str,
    batch_id: str,
    auth_headers: Dict[str, str],
    preset_run_key: Optional[Any] = None,
    notify: bool = False,
    run_name: Optional[str] = None,
) -> None:
    """POST /api/scenarios/{id}/run 的后台执行：线程内驱动（无 WS 订阅者，仅 registry 轮询）。"""

    def _thread_main() -> None:
        async def _emit(message: Dict[str, Any]) -> None:
            return None

        async def _run() -> None:
            bridge = CloudBridge(auth_headers=auth_headers)
            await run_scenario_local(
                scenario_id=scenario_id,
                env_id=env_id,
                device_serial=device_serial,
                run_id=run_id,
                batch_id=batch_id,
                emit=_emit,
                bridge=bridge,
                notify=notify,
                preset_run_key=preset_run_key,
                run_name=run_name,
            )

        try:
            asyncio.run(_run())
        except Exception:
            logger.exception("场景本地执行线程异常: scenario=%s device=%s", scenario_id, device_serial)

    threading.Thread(
        target=_thread_main, name=f"local-scenario-{scenario_id}-{device_serial}", daemon=True
    ).start()


async def run_scenario_via_websocket(
    websocket: Any,
    scenario_id: int,
    env_id: Optional[int],
    device_serial: Optional[str],
    auth_headers: Dict[str, str],
) -> None:
    """WS /api/scenarios/ws/run/{scenario_id}：连接即执行（协议对齐 scenarios.websocket_run_scenario）。"""
    await websocket.accept()

    async def _emit(message: Dict[str, Any]) -> None:
        try:
            await websocket.send_json(message)
        except Exception:
            logger.warning("场景 WS 推送失败: scenario=%s", scenario_id, exc_info=True)

    try:
        if not device_serial:
            await emit_log(_emit, "error", "❌ 请选择执行设备")
            await _emit(
                {
                    "type": "run_complete",
                    "success": False,
                    "status": "ERROR",
                    "summary": "请选择执行设备",
                    "report_id": None,
                    "execution_id": None,
                }
            )
            return
        bridge = CloudBridge(auth_headers=auth_headers)
        # S5：服务端预检分支（对齐 scenarios.websocket_run_scenario）——
        # 预检失败只发 log + run_complete{status:ERROR}，不进执行体。
        try:
            precheck = await asyncio.to_thread(
                precheck_scenario_for_device, bridge, scenario_id, device_serial, env_id
            )
        except Exception as exc:
            message = f"运行前预检异常: {exc}"
            await emit_log(_emit, "error", f"❌ {message}")
            await _emit(
                {
                    "type": "run_complete",
                    "success": False,
                    "status": "ERROR",
                    "summary": message,
                    "report_id": None,
                    "execution_id": None,
                }
            )
            return
        if not precheck.get("ok"):
            reason = summarize_precheck_failure(precheck)
            summary = f"运行前预检未通过: {reason}"
            await emit_log(_emit, "error", f"❌ {summary}")
            await _emit(
                {
                    "type": "run_complete",
                    "success": False,
                    "status": "ERROR",
                    "summary": summary,
                    "report_id": None,
                    "execution_id": None,
                }
            )
            return
        await run_scenario_local(
            scenario_id=scenario_id,
            env_id=env_id,
            device_serial=device_serial,
            emit=_emit,
            bridge=bridge,
        )
    except Exception as exc:
        logger.exception("场景 WS 执行失败: scenario=%s", scenario_id)
        await emit_log(_emit, "error", f"❌ 系统异常: {exc}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
