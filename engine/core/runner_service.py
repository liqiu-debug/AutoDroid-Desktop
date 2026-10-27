# 来源：部分语义对齐 AutoDroid backend/api/ws_run.py + backend/api/cases.py run 端点。
# 剥离点：DB/报告生成改由云端 /api/client-runs 完成；socket_manager 改为本地 RunBroker。
"""本地用例执行 + 结果上报（客户端模式：设备 I/O 全本地，结果上报云端）。

消息协议与 ws_run.py 对齐（LogConsole 零改动）：
- run_start     {type, case_id, case_name, total_steps, status, timestamp, batch_id, run_id, device_serial}
- step_update   {type, step_id, step_index, status, log, duration, timestamp[, screenshot][, error][, error_code][, suggestion]}
- run_complete  {type, case_id, success, status, total_duration, passed, failed, report_id, timestamp, batch_id, run_id, device_serial}
- error         {type, message}
"""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from engine.core import recording_service, run_registry
from engine.core.cloud_bridge import CloudBridge
from engine.core.drivers.cross_platform_runner import TestCaseRunner as CrossPlatformRunner
from engine.core.execution_service import (
    collect_case_variables_map,
    prepare_case_steps_for_platform,
    precheck_case_execution,
    resolve_device_platform,
)
from engine.core.run_registry import LocalRunRecord
from engine.core.step_contract import normalize_error_strategy, standard_step_to_legacy

logger = logging.getLogger(__name__)

# run_source 取值域见 backend/models.py TestExecution.run_source：app | platform。
# 桌面客户端本地执行 = 平台控机执行（'platform'，同 scenario_execution）；
# 'app' 专指 App/设备端独立执行（api/autojs.py 上报）。对齐契约倾向取值。
DEFAULT_RUN_SOURCE = "platform"

# 收尾状态取值域对齐 backend/api/client_runs.py ClientRunFinishPayload.status
# （PASS/FAIL/WARNING/ABORTED/ERROR）。S2：聚合状态（_overall_status 的 WARNING、
# 异常路径的 ERROR）必须能发出——finish 以显式 status 透传，下表仅作缺省映射。
_FINISH_STATUS_DOMAIN = ("PASS", "FAIL", "WARNING", "ABORTED", "ERROR")
_FINISH_STATUS_BY_OUTCOME = {
    (True, False): "PASS",
    (False, False): "FAIL",
    (True, True): "ABORTED",
    (False, True): "ABORTED",
}


def _resolve_finish_status(status: Optional[str], success: bool, aborted: bool) -> str:
    """显式 status 优先（S2）；缺省按 (success, aborted) 映射。"""
    normalized = str(status or "").strip().upper()
    if normalized in _FINISH_STATUS_DOMAIN:
        return normalized
    return _FINISH_STATUS_BY_OUTCOME[(bool(success), bool(aborted))]


def _now_iso() -> str:
    return datetime.now().isoformat()


class RunBroker:
    """/ws/run/{case_id} 连接管理 + 跨线程消息分发（本地 socket_manager 替代）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: Dict[int, List[Any]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, case_id: int, websocket: Any) -> None:
        with self._lock:
            self._clients.setdefault(int(case_id), []).append(websocket)

    def unregister(self, case_id: int, websocket: Any) -> None:
        with self._lock:
            clients = self._clients.get(int(case_id)) or []
            if websocket in clients:
                clients.remove(websocket)
            if not clients:
                self._clients.pop(int(case_id), None)

    def _snapshot(self, case_id: int) -> List[Any]:
        with self._lock:
            return list(self._clients.get(int(case_id)) or [])

    async def publish_async(self, case_id: int, message: Dict[str, Any]) -> None:
        for ws in self._snapshot(case_id):
            try:
                await ws.send_json(message)
            except Exception:
                logger.warning("WS 推送失败（连接可能已断开）: case=%s", case_id, exc_info=True)

    def publish_sync(self, case_id: int, message: Dict[str, Any]) -> None:
        """线程侧调用：把消息投递到事件循环。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.publish_async(case_id, message), loop)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._clients.clear()
            self._loop = None


broker = RunBroker()


class LocalRunContext:
    """一次本地执行的上下文（上报 + 消息发射）。

    上报契约字段对齐 backend/api/client_runs.py 的 ClientRun* 模型：
    - create: run_id/kind/case_id/scenario_id/device_serial/platform/run_source/
              started_at(ISO datetime)/name/env_id
    - steps : case_id/name/alias/success/exported_variables/steps
    - finish: status/aborted/duration/error/notify/exported_variables
    """

    def __init__(
        self,
        case_id: Optional[int],
        device_serial: str,
        env_id: Optional[int],
        run_id: str,
        batch_id: str,
        emit: Callable[[Dict[str, Any]], Awaitable[None]],
        bridge: CloudBridge,
        *,
        kind: str = "case",
        scenario_id: Optional[int] = None,
    ) -> None:
        self.case_id = int(case_id) if case_id is not None else None
        self.device_serial = device_serial
        self.env_id = env_id
        self.run_id = run_id
        self.batch_id = batch_id
        self.emit = emit
        self.bridge = bridge
        self.kind = str(kind or "case").strip().lower()
        self.scenario_id = int(scenario_id) if scenario_id is not None else None
        self.run_key: Optional[Any] = None
        self.steps_reported = False
        self.started_at_iso = datetime.now().isoformat()

    def create_cloud_run(self, name: str, platform: str) -> None:
        payload = {
            "run_id": self.run_id,
            "kind": self.kind,
            "case_id": self.case_id if self.kind == "case" else None,
            "scenario_id": self.scenario_id,
            "device_serial": self.device_serial,
            "platform": platform,
            "run_source": DEFAULT_RUN_SOURCE,
            # 云端 ClientRunCreate.started_at 为 datetime（ISO 8601）；不得发 epoch float
            "started_at": self.started_at_iso,
            "name": name,
            "env_id": self.env_id,
        }
        try:
            created = self.bridge.create_run(payload) or {}
            created_id = created.get("id")
            self.run_key = created_id or created.get("run_id") or self.run_id
            # S1：云端 execution_id 回填本地登记（/api/runs/active 的 items[].execution_id
            # 供前端 terminate 载荷 {batch_id, execution_ids, device_serials} 命中）。
            run_registry.registry.update_execution_id(self.run_id, created_id)
        except Exception as exc:
            logger.warning("client-runs 创建失败（本地继续执行，事后无法回看报告）: %s", exc)
            self.run_key = None

    def report_step_results(
        self,
        case_name: str,
        success: bool,
        step_entries: List[Dict[str, Any]],
        exported_variables: Optional[Dict[str, Any]] = None,
        *,
        case_id: Optional[int] = None,
        alias: Optional[str] = None,
    ) -> None:
        if self.run_key is None:
            return
        steps = [
            {
                "order": entry.get("order"),
                "action": entry.get("action"),
                "status": entry.get("report_status"),
                "duration": entry.get("duration"),
                "attempts": entry.get("attempts") or 1,
                "error": entry.get("error"),
                "error_code": entry.get("error_code"),
                "screenshot_base64": entry.get("screenshot_base64"),
                "output": entry.get("output"),
            }
            for entry in step_entries
        ]
        # 空步骤批次必须带显式 success（缺省会被 all([])==True 推导为 PASS）：
        # success 形参强制必传，缺省按 False 上报（两侧对齐 DeviceReportCase.success 默认）。
        if success is None:
            success = bool(steps)
        payload = {
            "case_id": int(case_id) if case_id is not None else self.case_id,
            # 云端 ClientRunStepsPayload 契约字段为 name（case_name 为兼容双写）
            "name": case_name,
            "case_name": case_name,
            "alias": alias,
            "success": bool(success),
            "exported_variables": exported_variables or {},
            "steps": steps,
        }
        try:
            self.bridge.report_steps(self.run_key, payload)
            self.steps_reported = True
        except Exception as exc:
            logger.warning("client-runs 步骤上报失败: %s", exc)

    def finish(
        self,
        success: bool,
        aborted: bool,
        duration: float,
        notify: bool = False,
        logs: Optional[List[str]] = None,
        exported_variables: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> Optional[str]:
        if self.run_key is None:
            return None
        # 云端 ClientRunFinishPayload 以 status 显式收尾（不能只靠步骤推导：
        # 步骤全 PASS 但收尾失败时必须落 FAIL/ABORTED；聚合 WARNING/异常 ERROR 直接透传——S2）。
        resolved_status = _resolve_finish_status(status, success, aborted)
        error_text = "\n".join(str(line) for line in (logs or []) if line)[:2000] if not success else None
        payload = {
            "status": resolved_status,
            "aborted": bool(aborted),
            "duration": round(float(duration or 0), 3),
            "error": error_text,
            "notify": bool(notify),
            "exported_variables": dict(exported_variables or {}),
        }
        try:
            finished = self.bridge.finish_run(self.run_key, payload) or {}
            return finished.get("report_id")
        except Exception as exc:
            logger.warning("client-runs 收尾上报失败: %s", exc)
            return None


def case_run_status(step_entries: List[Dict[str, Any]], success: bool, aborted: bool) -> str:
    """用例收尾聚合状态（对齐平台 _determine_case_status 口径，S2）。

    全 SKIP / 仅 IGNORE-WARNING 不落 PASS，落 WARNING（平台聚合口径）。
    """
    if aborted:
        return "ABORTED"
    if any(entry.get("report_status") == "FAIL" for entry in step_entries):
        return "FAIL"
    if step_entries and all(entry.get("report_status") == "SKIP" for entry in step_entries):
        return "WARNING"
    if any(entry.get("report_status") == "WARNING" for entry in step_entries):
        return "WARNING"
    return "PASS" if success else "FAIL"


def _legacy_step_dict(step: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return standard_step_to_legacy(step)
    except Exception:
        return {
            "action": step.get("action"),
            "selector": None,
            "selector_type": None,
            "value": step.get("value"),
            "options": {},
            "description": step.get("description"),
            "timeout": step.get("timeout", 10),
            "error_strategy": step.get("error_strategy", "ABORT"),
        }


def _capture_screenshot(runner) -> Optional[str]:
    try:
        raw = runner.driver.screenshot()
        if isinstance(raw, (bytes, bytearray)):
            return base64.b64encode(bytes(raw)).decode("utf-8")
        import io

        buffer = io.BytesIO()
        raw.convert("RGB").save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception:
        return None


async def execute_prepared_steps(
    prepared_steps: List[Dict[str, Any]],
    *,
    platform: str,
    device_serial: str,
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    abort_event: Optional[threading.Event] = None,
    runner: Any = None,
) -> Dict[str, Any]:
    """执行已准备好的标准步骤（跨端 Runner），过程消息经 emit 推送。

    单例执行与场景批量执行共用本核心；runner 可复用（场景逐 case 传入同一体）。
    返回 {step_entries, passed, failed, aborted, success, duration, runtime_variables}。
    """
    loop = asyncio.get_running_loop()

    def _blocking(func, *args, **kwargs):
        return loop.run_in_executor(None, lambda: func(*args, **kwargs))

    started_at = time.time()
    step_entries: List[Dict[str, Any]] = []
    passed = 0
    failed = 0
    aborted = False
    runtime_variables: Dict[str, str] = {}
    owns_runner = runner is None

    if runner is None:
        try:
            runner = await _blocking(
                CrossPlatformRunner,
                platform=platform,
                device_id=device_serial,
                abort_event=abort_event,
            )
        except Exception as exc:
            return {
                "step_entries": [],
                "passed": 0,
                "failed": 0,
                "aborted": False,
                "success": False,
                "duration": 0.0,
                "runtime_variables": {},
                "error": f"执行器创建失败: {exc}",
            }

    try:
        for index, step in enumerate(prepared_steps):
            if abort_event is not None and abort_event.is_set():
                aborted = True
                await emit(
                    {
                        "type": "step_update",
                        "step_id": str(index),
                        "step_index": index,
                        "status": "warning",
                        "log": "执行已被用户终止",
                        "duration": 0,
                        "timestamp": _now_iso(),
                    }
                )
                break

            action = step.get("action") if isinstance(step, dict) else ""
            desc = step.get("description", "") if isinstance(step, dict) else ""
            await emit(
                {
                    "type": "step_update",
                    "step_id": str(index),
                    "step_index": index,
                    "status": "running",
                    "log": f"[{index + 1}/{len(prepared_steps)}] 执行 {action}: {desc}",
                    "duration": 0,
                    "timestamp": _now_iso(),
                }
            )

            step_result = await _blocking(runner.run_step, step)
            if isinstance(step_result.get("runtime_variables"), dict):
                runtime_variables.update(
                    {str(k): "" if v is None else str(v) for k, v in step_result["runtime_variables"].items()}
                )
            if abort_event is not None and abort_event.is_set():
                aborted = True
                step_entries.append(
                    {
                        **_legacy_step_dict(step),
                        "order": step.get("order") or (index + 1),
                        "action": step.get("action"),
                        "status": "warning",
                        "report_status": "WARNING",
                        "duration": round(float(step_result.get("duration") or 0), 2),
                        "log": "执行已被用户终止",
                        "error": "执行已被用户终止",
                        "attempts": step_result.get("attempts") or 1,
                    }
                )
                await emit(
                    {
                        "type": "step_update",
                        "step_id": str(index),
                        "step_index": index,
                        "status": "warning",
                        "log": "执行已被用户终止",
                        "duration": round(float(step_result.get("duration") or 0), 2),
                        "timestamp": _now_iso(),
                    }
                )
                break

            status = str(step_result.get("status") or "FAIL").upper()
            strategy = normalize_error_strategy(step_result.get("error_strategy", "ABORT"))
            error_msg = step_result.get("error")
            error_code = str(step_result.get("error_code") or "").strip() or None
            error_suggestion = str(step_result.get("suggestion") or "").strip() or None
            duration = float(step_result.get("duration") or 0)
            legacy_step = _legacy_step_dict(step)

            screenshot_base64 = None
            artifacts = step_result.get("artifacts") if isinstance(step_result, dict) else None
            if isinstance(artifacts, dict):
                cached_screenshot = artifacts.get("screenshot_base64")
                if cached_screenshot:
                    screenshot_base64 = str(cached_screenshot)
            if status in ("FAIL", "WARNING") and not screenshot_base64:
                screenshot_base64 = await _blocking(_capture_screenshot, runner)

            entry_common = {
                **legacy_step,
                "order": step.get("order") or (index + 1),
                "action": step.get("action"),
                "duration": round(duration, 2),
                "attempts": step_result.get("attempts") or 1,
                "error": error_msg,
                "error_code": error_code,
                "suggestion": error_suggestion,
                "output": step_result.get("output") if isinstance(step_result.get("output"), dict) else None,
                "screenshot_base64": screenshot_base64,
            }

            if status == "PASS":
                passed += 1
                step_entries.append({**entry_common, "status": "success", "report_status": "PASS",
                                     "log": f"✓ 步骤成功 ({round(duration, 2)}s)"})
                await emit(
                    {
                        "type": "step_update",
                        "step_id": str(index),
                        "step_index": index,
                        "status": "success",
                        "log": f"✓ 步骤 {index + 1} 成功",
                        "duration": round(duration, 2),
                        "timestamp": _now_iso(),
                    }
                )
                continue

            if status == "SKIP":
                step_entries.append({**entry_common, "status": "skipped", "report_status": "SKIP",
                                     "log": f"↷ 步骤跳过: {error_msg or '平台不匹配'}"})
                await emit(
                    {
                        "type": "step_update",
                        "step_id": str(index),
                        "step_index": index,
                        "status": "skipped",
                        "log": f"↷ 步骤 {index + 1} 跳过: {error_msg or '平台不匹配'}",
                        "duration": round(duration, 2),
                        "timestamp": _now_iso(),
                        "error": error_msg,
                        "error_code": error_code,
                        "suggestion": error_suggestion,
                    }
                )
                continue

            if status == "WARNING" or (status == "FAIL" and strategy == "IGNORE"):
                step_entries.append({**entry_common, "status": "warning", "report_status": "WARNING",
                                     "log": f"⚠ 步骤失败(IGNORE): {error_msg}"})
                await emit(
                    {
                        "type": "step_update",
                        "step_id": str(index),
                        "step_index": index,
                        "status": "warning",
                        "log": f"⚠ 步骤 {index + 1} 失败(已忽略): {error_msg}",
                        "duration": round(duration, 2),
                        "timestamp": _now_iso(),
                        "screenshot": screenshot_base64,
                        "error": error_msg,
                        "error_code": error_code,
                        "suggestion": error_suggestion,
                    }
                )
                continue

            failed += 1
            step_entries.append({**entry_common, "status": "failed", "report_status": "FAIL",
                                 "log": f"✗ 步骤失败: {error_msg}"})
            await emit(
                {
                    "type": "step_update",
                    "step_id": str(index),
                    "step_index": index,
                    "status": "failed",
                    "log": f"✗ 步骤 {index + 1} 失败: {error_msg}",
                    "duration": round(duration, 2),
                    "timestamp": _now_iso(),
                    "screenshot": screenshot_base64,
                    "error": error_msg,
                    "error_code": error_code,
                    "suggestion": error_suggestion,
                }
            )

            if strategy == "ABORT":
                break
    finally:
        if owns_runner and runner is not None and hasattr(runner, "disconnect"):
            try:
                await _blocking(runner.disconnect)
            except Exception:
                logger.exception("Runner 断开失败")

    if isinstance(getattr(runner, "runtime_variables", None), dict):
        runtime_variables.update(
            {str(k): "" if v is None else str(v) for k, v in runner.runtime_variables.items()}
        )

    success = (failed == 0) and not aborted
    return {
        "step_entries": step_entries,
        "passed": passed,
        "failed": failed,
        "aborted": aborted,
        "success": success,
        "duration": time.time() - started_at,
        "runtime_variables": runtime_variables,
    }


async def run_case_local(
    *,
    case_id: int,
    env_id: Optional[int],
    device_serial: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    bridge: CloudBridge,
    notify: bool = False,
    abort_event: Optional[threading.Event] = None,
    run_kind: str = "case",
    target_id: Optional[int] = None,
) -> Dict[str, Any]:
    """本地执行用例（阻塞驱动调用走线程池），过程消息经 emit 推送，结果上报云端。

    生命周期登记到本地 run registry（/api/runs/active、/api/runs/cancel 依赖）。
    """
    loop = asyncio.get_running_loop()
    run_id = run_id or str(uuid.uuid4())
    batch_id = batch_id or str(uuid.uuid4())
    record = run_registry.registry.register(
        LocalRunRecord(
            kind=run_kind,
            target_id=int(target_id if target_id is not None else case_id),
            run_id=run_id,
            batch_id=batch_id,
            device_serial=device_serial,
        )
    )
    if abort_event is None:
        abort_event = record.abort_event

    context = LocalRunContext(
        case_id=case_id,
        device_serial=device_serial,
        env_id=env_id,
        run_id=run_id,
        batch_id=batch_id,
        emit=emit,
        bridge=bridge,
        kind=run_kind,
        scenario_id=target_id if run_kind == "scenario" else None,
    )

    def _blocking(func, *args, **kwargs):
        return loop.run_in_executor(None, lambda: func(*args, **kwargs))

    try:
        case_name = f"case-{case_id}"
        try:
            case = await _blocking(bridge.fetch_case, case_id)
            case_name = str((case or {}).get("name") or case_name)
            step_rows = await _blocking(bridge.fetch_case_steps, case_id)
            env_variables = await _blocking(bridge.fetch_env_variables, env_id)
            mapping = await _blocking(bridge.fetch_app_key_mapping)
        except Exception as exc:
            await emit({"type": "error", "message": f"拉取用例数据失败: {exc}"})
            return {"success": False, "error": str(exc)}

        variables_map = collect_case_variables_map((case or {}).get("variables"), env_variables)
        platform = resolve_device_platform(device_serial)

        try:
            prepared_steps, variables_map = await _blocking(
                prepare_case_steps_for_platform,
                step_rows,
                platform,
                variables_map,
                mapping,
                (case or {}).get("steps"),
            )
            for index, step_item in enumerate(prepared_steps):
                prepared_steps[index] = await _blocking(
                    recording_service.localize_step_images, step_item, bridge.ensure_template_local
                )
        except Exception as exc:
            await emit({"type": "error", "message": f"步骤准备失败: {exc}"})
            return {"success": False, "error": str(exc)}

        await emit(
            {
                "type": "run_start",
                "case_id": case_id,
                "case_name": case_name,
                "total_steps": len(prepared_steps),
                "status": "RUNNING",
                "timestamp": _now_iso(),
                "batch_id": batch_id,
                "run_id": run_id,
                "device_serial": device_serial,
            }
        )

        context.create_cloud_run(case_name, platform)
        run_started_at = time.time()

        # S5：create_cloud_run 后执行体包 try/except——意外异常也必须收尾
        # （run_complete{status:ERROR} + finish(status=ERROR)），否则云端
        # TestExecution 停留 RUNNING。
        try:
            outcome = await execute_prepared_steps(
                prepared_steps,
                platform=platform,
                device_serial=device_serial,
                emit=emit,
                abort_event=abort_event,
            )
            if outcome.get("error"):
                message = str(outcome["error"])
                await emit({"type": "error", "message": message})
                report_id = await _blocking(
                    context.finish,
                    False,
                    False,
                    0,
                    notify,
                    [message],
                    status="ERROR",
                )
                await emit(
                    {
                        "type": "run_complete",
                        "case_id": case_id,
                        "success": False,
                        "status": "ERROR",
                        "total_duration": 0,
                        "passed": 0,
                        "failed": 0,
                        "report_id": report_id,
                        "timestamp": _now_iso(),
                        "batch_id": batch_id,
                        "run_id": run_id,
                        "device_serial": device_serial,
                    }
                )
                return {"success": False, "status": "ERROR", "error": message}

            step_entries = outcome["step_entries"]
            passed = outcome["passed"]
            failed = outcome["failed"]
            aborted = outcome["aborted"]
            total_duration = outcome["duration"]
            success = outcome["success"]
            # S2：聚合状态含 WARNING（全跳过/仅 IGNORE），显式透传云端 finish
            final_status = case_run_status(step_entries, success, aborted)
            exported_variables = {str(k): v for k, v in (variables_map or {}).items()}
            exported_variables.update(outcome.get("runtime_variables") or {})

            if aborted:
                finish_logs = ["执行已被用户终止"]
            elif not success:
                step_errors = [
                    str(entry.get("error"))
                    for entry in step_entries
                    if entry.get("report_status") == "FAIL" and entry.get("error")
                ]
                finish_logs = step_errors[-1:] or ["用例执行失败"]
            else:
                finish_logs = None

            # 步骤结果批量上报 + finish（report_id 由云端生成；success 恒显式）
            await _blocking(
                context.report_step_results,
                case_name,
                success,
                step_entries,
                exported_variables,
            )
            report_id = await _blocking(
                context.finish,
                success,
                aborted,
                total_duration,
                notify,
                finish_logs,
                status=final_status,
            )

            await emit(
                {
                    "type": "run_complete",
                    "case_id": case_id,
                    "success": success,
                    "status": final_status,
                    "total_duration": round(total_duration, 2),
                    "passed": passed,
                    "failed": failed,
                    "report_id": report_id,
                    "timestamp": _now_iso(),
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "device_serial": device_serial,
                }
            )

            return {
                "success": success,
                "status": final_status,
                "report_id": report_id,
                "passed": passed,
                "failed": failed,
                "total_duration": round(total_duration, 2),
                "run_id": run_id,
                "batch_id": batch_id,
                "exported_variables": exported_variables,
            }
        except Exception as exc:
            message = f"执行异常: {exc}"
            logger.exception("本地执行异常: case=%s device=%s", case_id, device_serial)
            await emit({"type": "error", "message": message})
            try:
                report_id = await _blocking(
                    context.finish,
                    False,
                    False,
                    max(0.0, time.time() - run_started_at),
                    notify,
                    [message],
                    status="ERROR",
                )
            except Exception:
                logger.exception("异常收尾上报失败: case=%s", case_id)
                report_id = None
            await emit(
                {
                    "type": "run_complete",
                    "case_id": case_id,
                    "success": False,
                    "status": "ERROR",
                    "total_duration": round(max(0.0, time.time() - run_started_at), 2),
                    "passed": 0,
                    "failed": 0,
                    "report_id": report_id,
                    "timestamp": _now_iso(),
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "device_serial": device_serial,
                }
            )
            return {"success": False, "status": "ERROR", "error": message}
    finally:
        run_registry.registry.complete(run_id, "ABORTED" if (abort_event is not None and abort_event.is_set()) else None)


def build_precheck_payload(
    case_id: Optional[int],
    device_serial: str,
    local: Dict[str, Any],
    variables_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """本地 precheck_case_execution 结果 → 平台响应形状
    （backend/cross_platform_execution.precheck_case_execution 同形）。"""
    step_checks = list(local.get("step_checks") or [])
    pass_count = sum(1 for item in step_checks if item.get("status") == "PASS")
    skip_count = sum(1 for item in step_checks if item.get("status") == "SKIP")
    fail_count = sum(1 for item in step_checks if item.get("status") == "FAIL")
    global_fail_count = 1 if any(item.get("status") == "FAIL" for item in local.get("global_checks") or []) else 0
    has_runnable_steps = pass_count > 0
    exported_variables = dict(variables_map or {})
    for key in local.get("known_variable_keys") or []:
        exported_variables.setdefault(str(key), "")
    return {
        "case_id": int(case_id) if case_id is not None else None,
        "device_serial": device_serial,
        "platform": local.get("platform"),
        # 平台口径：有 FAIL 或无可执行步骤 → 预检不通过
        "ok": global_fail_count == 0 and fail_count == 0 and has_runnable_steps,
        "has_runnable_steps": has_runnable_steps,
        "summary": {
            "total": len(step_checks),
            "pass": pass_count,
            "skip": skip_count,
            "fail": fail_count,
            "global_fail": global_fail_count,
        },
        "global_checks": local.get("global_checks") or [],
        "steps": step_checks,
        "exported_variables": exported_variables,
    }


def precheck_for_device(
    bridge: CloudBridge,
    case_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
) -> Dict[str, Any]:
    """本地前置预检（响应形状对齐 backend/cross_platform_execution.precheck_case_execution）。

    - GET /api/cases/{id}/precheck 与 run-batch 的 blocked_prechecks 共用；
    - iOS 预检不因 WDA 探测失败而阻断（R4）：本地无 iOS 能力时仅告警（WARNING）。
    """
    case = bridge.fetch_case(case_id)
    step_rows = bridge.fetch_case_steps(case_id)
    env_variables = bridge.fetch_env_variables(env_id)
    mapping = bridge.fetch_app_key_mapping()
    variables_map = collect_case_variables_map((case or {}).get("variables"), env_variables)
    local = precheck_case_execution(
        step_rows,
        device_serial,
        variables_map=variables_map,
        mapping=mapping,
        legacy_steps=(case or {}).get("steps"),
    )
    return build_precheck_payload(case_id, device_serial, local, variables_map)


# --------------------------- HTTP/WS 驱动入口 --------------------------- #

def start_background_case_run(
    *,
    case_id: int,
    env_id: Optional[int],
    device_serial: str,
    run_id: str,
    batch_id: str,
    auth_headers: Dict[str, str],
    notify: bool = False,
) -> None:
    """POST /cases/{id}/run(-batch) 的后台执行：线程内驱动 + broker 广播。"""

    def _thread_main() -> None:
        async def _emit(message: Dict[str, Any]) -> None:
            # 后台线程自建事件循环，WS 连接活在主循环上：必须走 publish_sync
            # 投递到主循环，不能在本循环里直接 await ws.send_json。
            broker.publish_sync(case_id, message)

        async def _run() -> None:
            bridge = CloudBridge(auth_headers=auth_headers)
            await run_case_local(
                case_id=case_id,
                env_id=env_id,
                device_serial=device_serial,
                run_id=run_id,
                batch_id=batch_id,
                emit=_emit,
                bridge=bridge,
                notify=notify,
            )

        try:
            asyncio.run(_run())
        except Exception:
            logger.exception("本地执行线程异常: case=%s device=%s", case_id, device_serial)

    threading.Thread(target=_thread_main, name=f"local-run-{case_id}-{device_serial}", daemon=True).start()


async def run_case_via_websocket(
    websocket: Any,
    case_id: int,
    env_id: Optional[int],
    device_serial: Optional[str],
    auth_headers: Dict[str, str],
) -> None:
    """WS /ws/run/{case_id}：连接即执行（语义对齐 ws_run.websocket_run_case）。"""
    await websocket.accept()
    broker.register(case_id, websocket)

    async def _emit(message: Dict[str, Any]) -> None:
        await broker.publish_async(case_id, message)

    try:
        if not device_serial:
            await _emit({"type": "error", "message": "请选择执行设备"})
            return
        bridge = CloudBridge(auth_headers=auth_headers)
        await run_case_local(
            case_id=case_id,
            env_id=env_id,
            device_serial=device_serial,
            emit=_emit,
            bridge=bridge,
        )
    except Exception as exc:
        logger.exception("WS 本地执行失败: case=%s", case_id)
        await _emit({"type": "error", "message": str(exc)})
    finally:
        broker.unregister(case_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass
