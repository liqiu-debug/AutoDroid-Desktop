# 来源：语义对齐 AutoDroid backend/compatibility_replay.py（read_installed_package /
#       package_snapshot_digest / entry_case_safety_issues）+ backend/api/compatibility.py
#       （_replay_preflight_result / _compat_replay_scope / _compat_terminal_outcome）。
"""兼容性「升级后链路回放」本地化（P4-2，按可行边界）。

可行边界（本轮交付）：
- 预检（POST /api/compatibility/replay-preflight）本地化：
  · 设备安装包快照 = 本机 adb 读取（vendor read_installed_package，exec 参数数组）；
  · 巡检来源/冻结链路 = 云端只读 API（GET /api/inspections/runs/{id} +
    /runs/{id}/replay-paths，形状即 plan.chains）；
  · 预检响应逐字段对齐 CompatibilityReplayPreflightRead（blockers/warnings/
    plan_digest/device_snapshot_digest/chains/summary/excluded/available_prefixes）。
- 冻结路径回放执行内核：run_frozen_chain_locally = 「批量步骤执行」复用
  runner_service（chain.first_path → 标准步骤 → execute_prepared_steps），
  产出逐步 trace + completed_checkpoints。
- 回放执行 + 结果上报全链路（P4-2 收尾）：POST /api/compatibility/runs
  （installed_replay）本地拦截 → 云端 client-compatibility 建档
  （POST /api/client-compatibility/runs，client_executed=True、云端不派 worker）
  → 逐设备 × 冻结链路本地执行 → POST /api/client-compatibility/runs/{id}/report
  上报（中间态 RUNNING + 终态），云端 _update_run_summary 收尾 run。

剩余（如实后置，见验收清单）：
- reachability/safety 内核（backend/inspection/replay.py 约 2500 行：current-XML
  rebinding、checkpoint 可达性判定、terminal boundary 校验）保留云端；
- 多设备矩阵 / 证据资产 CAS / 差异对比（compare 一族）保留云端。

安全红线：token 只经 CloudBridge 内存取用；adb 走 exec 参数数组（T5 口径）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
import threading
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from engine.adb_scan import resolve_adb_binary
from engine.core.cloud_bridge import CloudBridge

logger = logging.getLogger(__name__)

AdbRunner = Callable[[str, float], Awaitable[str]]

PLAN_VERSION = 3


# --------------------------------------------------------------------------- #
#  摘要/解析（vendor 自 backend/compatibility_replay.py，纯函数）
# --------------------------------------------------------------------------- #

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def package_snapshot_digest(snapshot: Dict[str, Any]) -> str:
    """只对稳定包标识取摘要（排除抓取时间戳）。"""
    fields = (
        "package_name",
        "version_name",
        "version_code",
        "first_install_time",
        "last_update_time",
        "signing_digest",
        "installed",
    )
    return digest_json({key: snapshot.get(key) for key in fields})


def _parse_line_value(output: str, key: str) -> Optional[str]:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([^\s\r\n]+)", output)
    return match.group(1).strip() if match else None


def _parse_signing_digest(output: str) -> Optional[str]:
    explicit = re.search(
        r"(?i)(?:sha[- ]?256|certificate(?:Digest|Sha256))\s*[:=]\s*([0-9a-f:]{32,})",
        output,
    )
    if explicit:
        return explicit.group(1).replace(":", "").lower()
    signatures = re.search(r"(?im)^\s*(?:signatures|signingDetails)\s*[:=].*$", output)
    if not signatures:
        return None
    return hashlib.sha256(
        re.sub(r"\s+", " ", signatures.group(0)).strip().encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
#  设备安装包快照（本机 adb；T5：exec 参数数组，serial/包名经 shlex.quote）
# --------------------------------------------------------------------------- #

async def _default_adb_runner(cmd: str, timeout: float) -> str:
    import subprocess

    argv = shlex.split(str(cmd))
    if argv and argv[0] == "adb":
        # 全链路统一：adb 可执行路径经 engine.adb_scan 唯一入口解析
        argv[0] = resolve_adb_binary()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"adb timeout after {timeout}s: {cmd}")
    if proc.returncode != 0:
        message = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        raise RuntimeError(message or f"adb failed: {cmd}")
    return stdout.decode(errors="ignore")


async def read_installed_package(
    serial: str,
    package_name: str,
    *,
    adb_runner: Optional[AdbRunner] = None,
) -> Dict[str, Any]:
    """读取设备安装包元数据（不改变设备状态）。缺失包返回 installed=False 而非抛错。"""
    runner = adb_runner or _default_adb_runner
    quoted_serial = shlex.quote(str(serial))
    quoted_package = shlex.quote(str(package_name))
    try:
        path_output = str(
            await runner(f"adb -s {quoted_serial} shell pm path {quoted_package}", 20) or ""
        )
    except Exception as exc:
        return {
            "package_name": package_name,
            "installed": False,
            "known": False,
            "source": "device",
            "error": f"ADB_PACKAGE_QUERY_FAILED: {exc}",
            "captured_at": datetime.now().isoformat(),
        }
    if "package:" not in path_output:
        return {
            "package_name": package_name,
            "installed": False,
            "known": False,
            "source": "device",
            "captured_at": datetime.now().isoformat(),
        }
    try:
        output = str(
            await runner(f"adb -s {quoted_serial} shell dumpsys package {quoted_package}", 20) or ""
        )
    except Exception as exc:
        return {
            "package_name": package_name,
            "installed": True,
            "known": False,
            "source": "device",
            "error": f"ADB_PACKAGE_METADATA_FAILED: {exc}",
            "captured_at": datetime.now().isoformat(),
        }
    version_code = _parse_line_value(output, "versionCode")
    if version_code is None:
        version_code = _parse_line_value(output, "longVersionCode")
    snapshot = {
        "package_name": package_name,
        "version_name": _parse_line_value(output, "versionName"),
        "version_code": version_code,
        "first_install_time": _parse_line_value(output, "firstInstallTime"),
        "last_update_time": _parse_line_value(output, "lastUpdateTime"),
        "signing_digest": _parse_signing_digest(output),
        "installed": True,
        "known": bool(version_code or _parse_line_value(output, "versionName")),
        "source": "device",
        "captured_at": datetime.now().isoformat(),
    }
    snapshot["snapshot_digest"] = package_snapshot_digest(snapshot)
    return snapshot


# --------------------------------------------------------------------------- #
#  冻结链路辅助（vendor 自 api/compatibility.py / compatibility_replay.py 纯函数）
# --------------------------------------------------------------------------- #

def _compat_replay_scope(chain: Dict[str, Any]) -> str:
    value = str(
        chain.get("replay_scope") or chain.get("replay_eligibility") or "FULL"
    ).strip().upper()
    return {
        "FULL": "FULL_PATH",
        "FULL_PATH": "FULL_PATH",
        "SAFE_PREFIX": "PREFIX_TO_SAFETY_BOUNDARY",
        "PREFIX_TO_SAFETY_BOUNDARY": "PREFIX_TO_SAFETY_BOUNDARY",
        "DIAGNOSTIC_ONLY": "DIAGNOSTIC_ONLY",
    }.get(value, "NONE")


def _compat_terminal_outcome(chain: Dict[str, Any]) -> str:
    explicit = str(chain.get("terminal_outcome") or "").strip().upper()
    if explicit:
        return explicit
    outcomes = {
        str(item.get("terminal_outcome") or "NONE").strip().upper()
        for item in chain.get("terminal_boundaries") or []
        if isinstance(item, dict)
    }
    for outcome in (
        "APP_FAULT",
        "INFRA_FAULT",
        "AUTOMATION_FAILED",
        "EXTERNAL_NAVIGATION",
        "LOCATOR_FAILED",
        "SAFETY_BLOCKED",
        "BUDGET_STOP",
        "CANCELLED",
    ):
        if outcome in outcomes:
            return outcome
    return "NONE"


def _issue(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def entry_case_safety_issues(
    bridge: CloudBridge,
    branch_config: Dict[str, Any],
    *,
    package_name: Optional[str] = None,
) -> List[Dict[str, str]]:
    """entry 用例安全筛查（语义对齐 compatibility_replay.entry_case_safety_issues）。

    用例步骤走云端只读 API（GET /api/cases/{id}）；云端 404 等价「缺 entry 用例」。
    """
    case_id = branch_config.get("entry_case_id")
    case: Optional[Dict[str, Any]] = None
    if case_id:
        try:
            case = bridge.fetch_case(int(case_id))
        except Exception:
            case = None
    if not case:
        return [_issue("ENTRY_CASE_MISSING", "业务线缺少可回放的 entry 用例")]

    blocked: List[Dict[str, str]] = []
    dangerous_words = re.compile(
        r"登录|登出|密码|支付|付款|下单|删除|卸载|安装|确认购买|授权|外部"
        r"|\b(?:login|logout|password|pay|payment|purchase|place[ _-]?order|"
        r"delete|uninstall|install|authorize|external|checkout|cashier)\b",
        re.IGNORECASE,
    )
    allowed_actions = {
        "click",
        "wait_until_exists",
        "assert_text",
        "assert_image",
        "swipe",
        "back",
        "home",
        "sleep",
        "start_app",
        "stop_app",
    }
    target_package = str(package_name or "").strip()
    raw_steps = case.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = []
    for index, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            blocked.append(_issue("UNSAFE_ENTRY_CASE", f"entry 用例第 {index + 1} 步格式无效"))
            continue
        action = str(step.get("action") or "").strip().lower()
        options = step.get("options")
        options_text = (
            json.dumps(options, ensure_ascii=False, sort_keys=True, default=str)
            if isinstance(options, (dict, list))
            else str(options or "")
        )
        haystack = " ".join(
            [str(step.get(key) or "") for key in ("selector", "value", "description")]
            + [options_text]
        )
        unsafe_reason: Optional[str] = None
        if action not in allowed_actions:
            unsafe_reason = "不支持的动作"
        elif dangerous_words.search(haystack):
            unsafe_reason = "包含风险语义"
        elif action == "sleep":
            raw_seconds = (
                options.get("seconds")
                if isinstance(options, dict) and options.get("seconds") is not None
                else step.get("value", 1)
            )
            try:
                seconds = float(raw_seconds)
            except (TypeError, ValueError):
                seconds = -1
            if not 0 <= seconds <= 60:
                unsafe_reason = "sleep 必须在 0 到 60 秒之间"
        if unsafe_reason is None:
            try:
                step_timeout = float(step.get("timeout") or 10)
            except (TypeError, ValueError):
                step_timeout = -1
            if not 0 < step_timeout <= 60:
                unsafe_reason = "单步 timeout 必须在 1 到 60 秒之间"
        if unsafe_reason is None and action in {"start_app", "stop_app"} and target_package:
            app_id = str(
                (options.get("app_key") if isinstance(options, dict) else None)
                or step.get("selector")
                or step.get("value")
                or ""
            ).strip()
            if app_id != target_package:
                unsafe_reason = "只能启动或停止巡检目标包"
        if unsafe_reason:
            blocked.append(
                _issue("UNSAFE_ENTRY_CASE", f"entry 用例第 {index + 1} 步不安全: {unsafe_reason}")
            )
    return blocked


def source_package_snapshot(source_run: Dict[str, Any]) -> Dict[str, Any]:
    """来源包快照（优先巡检冻结快照 profile_snapshot.package_snapshot）。"""
    snapshot = (source_run.get("profile_snapshot") or {}).get("package_snapshot")
    package_name = str(source_run.get("package_name") or "")
    if isinstance(snapshot, dict):
        result = dict(snapshot)
        result.setdefault("package_name", package_name)
        result.setdefault("source", "inspection_snapshot")
        result.setdefault("known", bool(result.get("version_code")))
        return result
    return {
        "package_name": package_name,
        "installed": False,
        "known": False,
        "source": "inspection_snapshot",
    }


# --------------------------------------------------------------------------- #
#  预检（形状对齐 CompatibilityReplayPreflightRead）
# --------------------------------------------------------------------------- #

def build_local_replay_plan(
    bridge: CloudBridge,
    *,
    inspection_run_id: int,
    branch_key: str,
    max_chains: int = 20,
) -> Dict[str, Any]:
    """从云端只读 API 组装回放计划（chains=冻结链路，形状即 CompatibilityReplayChainRead）。"""
    paths = bridge.get_json(
        f"/api/inspections/runs/{int(inspection_run_id)}/replay-paths",
        params={"branch_key": branch_key, "page": 1, "page_size": max(1, min(100, int(max_chains)))},
    ) or {}
    items = [dict(item) for item in paths.get("items") or [] if isinstance(item, dict)]
    plan: Dict[str, Any] = {
        "plan_version": int(paths.get("schema_version") or PLAN_VERSION),
        "inspection_run_id": int(inspection_run_id),
        "branch_key": branch_key,
        "chains": items[: int(max_chains)],
        "summary": dict(paths.get("summary") or {}),
        "excluded": {"total": int(paths.get("total") or len(items))},
    }
    digest_payload = dict(plan)
    plan["digest"] = digest_json(digest_payload)
    plan["plan_digest"] = plan["digest"]
    return plan


def local_replay_preflight(
    bridge: CloudBridge,
    *,
    inspection_run_id: int,
    branch_key: str,
    device_serial: str,
    max_chains: int = 20,
    installed_package: Optional[Dict[str, Any]] = None,
    adb_runner: Optional[AdbRunner] = None,
) -> Dict[str, Any]:
    """本地预检：设备快照本机读 + 计划云端读，响应形状对齐 CompatibilityReplayPreflightRead。

    installed_package 可注入（同步入口/测试用）；缺省在本机 adb 读取。
    """
    source_run = bridge.get_json(f"/api/inspections/runs/{int(inspection_run_id)}") or {}
    source_status = str(source_run.get("status") or "").upper()
    package_name = str(source_run.get("package_name") or "")

    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    if not source_run.get("id"):
        raise CompatibilityPreflightError(404, "巡检来源任务不存在")
    if source_status not in {"PASS", "WARNING", "FAIL"}:
        raise CompatibilityPreflightError(400, "巡检来源任务尚未完成")
    if not package_name:
        raise CompatibilityPreflightError(400, "巡检来源缺少 package_name")

    profile_snapshot = source_run.get("profile_snapshot") or {}
    branches = profile_snapshot.get("branches") or {}
    branch_config = branches.get(branch_key)
    branch_config = dict(branch_config) if isinstance(branch_config, dict) else {}

    if installed_package is None:
        installed_package = asyncio.run(
            read_installed_package(device_serial, package_name, adb_runner=adb_runner)
        )
    installed_package = dict(installed_package)

    if not branch_config:
        blockers.append(
            {"code": "BRANCH_SNAPSHOT_MISSING", "message": "巡检报告缺少所选业务线的冻结配置"}
        )
    else:
        blockers.extend(
            entry_case_safety_issues(bridge, branch_config, package_name=package_name)
        )

    source_package = source_package_snapshot(source_run)
    if not bool(installed_package.get("installed")):
        blockers.append({"code": "PACKAGE_NOT_INSTALLED", "message": f"设备未安装 {package_name}"})
    elif str(installed_package.get("package_name") or "") != package_name:
        blockers.append({"code": "PACKAGE_NAME_MISMATCH", "message": "设备包名与巡检来源不一致"})
    if installed_package.get("error"):
        blockers.append(
            {"code": "PACKAGE_METADATA_UNAVAILABLE", "message": str(installed_package.get("error"))}
        )

    source_version = str(source_package.get("version_code") or "").strip()
    installed_version = str(installed_package.get("version_code") or "").strip()
    if not source_version:
        warnings.append(
            {"code": "SOURCE_VERSION_UNKNOWN", "message": "历史巡检未记录来源版本，人工确认后仍可回放"}
        )
    elif not installed_version:
        warnings.append(
            {"code": "TARGET_VERSION_UNKNOWN", "message": "无法读取设备 versionCode，人工确认后仍可回放"}
        )
    elif source_version == installed_version:
        warnings.append(
            {"code": "SAME_VERSION_REPLAY", "message": "设备版本与巡检来源相同，本次结果用于链路回放验收"}
        )

    plan: Dict[str, Any]
    try:
        plan = build_local_replay_plan(
            bridge,
            inspection_run_id=int(inspection_run_id),
            branch_key=branch_key,
            max_chains=max_chains,
        )
    except Exception as exc:
        detail = str(exc)
        # 业务线不存在（云端 replay-paths 404）对齐 _validate_replay_source 的 400 口径
        if "404" in detail:
            raise CompatibilityPreflightError(400, "巡检来源不包含所选业务线") from exc
        plan = {"plan_version": PLAN_VERSION, "digest": "", "summary": {}, "chains": [], "excluded": {}}
        blockers.append({"code": "REPLAY_PLAN_UNAVAILABLE", "message": detail})

    chains = [dict(item) for item in plan.get("chains") or [] if isinstance(item, dict)]
    for chain in chains:
        scope = _compat_replay_scope(chain)
        chain["replay_scope"] = scope
        chain["replay_eligibility"] = {
            "FULL_PATH": "FULL",
            "PREFIX_TO_SAFETY_BOUNDARY": "SAFE_PREFIX",
        }.get(scope, "NONE")
        chain["terminal_outcome"] = _compat_terminal_outcome(chain)
        chain.setdefault("boundary_evidence", "NOT_APPLICABLE")
    if not chains and not any(item["code"] == "REPLAY_PLAN_UNAVAILABLE" for item in blockers):
        blockers.append(
            {"code": "NO_REPLAYABLE_CHAINS", "message": "所选业务线没有安全、可定位的回放链路"}
        )

    device_digest = package_snapshot_digest(installed_package)
    installed_package["snapshot_digest"] = device_digest
    return {
        "execution_mode": "installed_replay",
        "inspection_run_id": int(inspection_run_id),
        "branch_key": branch_key,
        "package_name": package_name,
        "source_package": source_package,
        "installed_package": installed_package,
        "blockers": blockers,
        "warnings": warnings,
        "plan_digest": str(plan.get("digest") or plan.get("plan_digest") or ""),
        "device_snapshot_digest": device_digest,
        "plan_version": int(plan.get("plan_version") or PLAN_VERSION),
        "summary": dict(plan.get("summary") or {}),
        "chains": chains,
        "available_prefixes": [
            str(item.get("chain_id") or item.get("path_key") or "")
            for item in chains
            if _compat_replay_scope(item) == "PREFIX_TO_SAFETY_BOUNDARY"
        ],
        "excluded": plan.get("excluded") or {},
    }


class CompatibilityPreflightError(Exception):
    """预检前置校验失败（映射 HTTP 状态码 + detail）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --------------------------------------------------------------------------- #
#  冻结路径回放执行内核（= 批量步骤执行，复用 runner_service）
# --------------------------------------------------------------------------- #

def chain_steps_to_standard_rows(chain: Dict[str, Any]) -> List[Dict[str, Any]]:
    """冻结链路 first_path → 标准步骤行（runner_service 执行输入）。

    每个 first_path 取主定位候选（locator_candidates[0]）；无候选的步骤保留
    动作与描述（由 runner precheck 判定跳过/失败），不伪造定位。
    """
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(chain.get("first_path") or [], start=1):
        if not isinstance(item, dict):
            continue
        candidates = [c for c in (item.get("locator_candidates") or []) if isinstance(c, dict)]
        primary = candidates[0] if candidates else {}
        rows.append(
            {
                "order": index,
                "action": str(item.get("action_type") or "click"),
                "args": {
                    "selector": primary.get("selector"),
                    "by": primary.get("by"),
                    "value": item.get("input_value"),
                },
                "platform_overrides": {},
                "execute_on": [],
                "timeout": 10,
                "error_strategy": "ABORT",
                "description": str(item.get("action_key") or f"chain-step-{index}"),
                "retry_count": 0,
            }
        )
    return rows


def run_frozen_chain_locally(
    chain: Dict[str, Any],
    *,
    device_serial: str,
    platform: str = "android",
    emit=None,
    abort_event: Optional[threading.Event] = None,
    mapping: Optional[Dict[str, Any]] = None,
    variables_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """冻结路径回放执行内核：first_path 批量步骤执行（复用 runner_service）。

    返回 {status, reason, failure_type, trace, completed_checkpoints, checkpoint_count,
          duration}。status 口径对齐 _record_replay_result 消费面：PASS/WARNING/FAIL。
    checkpoint 完成度 = 成功步数（reachability 判定内核保留云端，见模块 docstring）。
    """
    from engine.core.execution_service import prepare_case_steps_for_platform
    from engine.core.runner_service import execute_prepared_steps

    checkpoints = [item for item in (chain.get("checkpoints") or []) if isinstance(item, dict)]
    rows = chain_steps_to_standard_rows(chain)
    if not rows:
        return {
            "status": "FAIL",
            "reason": "replay chain has no executable steps",
            "failure_type": "INVALID_REPLAY_PLAN",
            "trace": [],
            "completed_checkpoints": 0,
            "checkpoint_count": len(checkpoints),
            "duration": 0.0,
        }

    prepared, _ = prepare_case_steps_for_platform(
        rows,
        platform,
        dict(variables_map or {}),
        dict(mapping or {}),
        None,
    )
    outcome = asyncio.run(
        execute_prepared_steps(
            prepared,
            platform=platform,
            device_serial=device_serial,
            emit=_sync_emit(emit),
            abort_event=abort_event or threading.Event(),
        )
    )
    step_entries = outcome.get("step_entries") or []
    trace = [
        {
            "order": entry.get("order"),
            "action": entry.get("action"),
            "status": entry.get("report_status"),
            "duration": entry.get("duration"),
            "error": entry.get("error"),
        }
        for entry in step_entries
    ]
    completed = sum(1 for entry in step_entries if entry.get("report_status") in {"PASS", "WARNING", "SKIP"})
    failed = any(entry.get("report_status") == "FAIL" for entry in step_entries)
    aborted = bool(outcome.get("aborted"))
    if aborted:
        status, reason, failure_type = "FAIL", "replay aborted", "ABORTED"
    elif failed:
        status, reason, failure_type = "FAIL", "replay step failed", "STEP_FAILED"
    elif completed < len(rows):
        status, reason, failure_type = "WARNING", "replay partially completed", "PARTIAL"
    else:
        status, reason, failure_type = "PASS", "", None
    return {
        "status": status,
        "reason": reason,
        "failure_type": failure_type,
        "trace": trace,
        "completed_checkpoints": completed,
        "checkpoint_count": len(checkpoints),
        "duration": float(outcome.get("duration") or 0),
    }


def _sync_emit(emit):
    """把可选的同步 emit 适配为 runner 的 async emit（缺省丢弃）。"""
    if emit is None:
        async def _noop(_message):
            return None

        return _noop

    async def _adapted(message):
        result = emit(message)
        if asyncio.iscoroutine(result):
            await result

    return _adapted


# --------------------------------------------------------------------------- #
#  回放执行 + 结果上报（client-compatibility，P4-2 收尾）
# --------------------------------------------------------------------------- #

def build_client_installed_package(
    bridge: CloudBridge,
    *,
    inspection_run_id: int,
    device_serial: str,
    adb_runner: Optional[AdbRunner] = None,
) -> Dict[str, Any]:
    """创建 run 用安装包快照：包名取云端巡检来源，快照本机 adb 读取（云端无本机设备）。"""
    source_run = bridge.get_json(f"/api/inspections/runs/{int(inspection_run_id)}") or {}
    package_name = str(source_run.get("package_name") or "")
    if not package_name:
        raise CompatibilityPreflightError(400, "巡检来源缺少 package_name")
    return asyncio.run(
        read_installed_package(device_serial, package_name, adb_runner=adb_runner)
    )


def chain_report_from_result(chain: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """内核结果 → ClientReplayChainReport 载荷（chain_id 对齐 plan.chains 索引键）。"""
    failed_step_index: Optional[int] = None
    for entry in result.get("trace") or []:
        if str(entry.get("status") or "").upper() == "FAIL":
            order = entry.get("order")
            failed_step_index = max(int(order) - 1, 0) if isinstance(order, int) else None
            break
    status = str(result.get("status") or "FAIL").upper()
    failure_type = result.get("failure_type") or None
    warning_codes: List[str] = []
    if status == "WARNING" and failure_type:
        warning_codes = [str(failure_type)]
    return {
        "chain_id": str(
            chain.get("chain_id") or chain.get("path_key") or chain.get("id") or ""
        ),
        "status": status,
        "reason": result.get("reason") or "",
        "failure_type": failure_type,
        "failed_step_index": failed_step_index,
        "completed_checkpoints": int(result.get("completed_checkpoints") or 0),
        "duration_ms": float(result.get("duration") or 0.0) * 1000.0,
        "warning_codes": warning_codes,
        "boundary_evidence": str(chain.get("boundary_evidence") or "") or None,
        "trace": [dict(item) for item in result.get("trace") or []],
    }


def cell_report_from_chain_reports(
    device_serial: str,
    chain_reports: List[Dict[str, Any]],
    *,
    installed_package: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    current_stage: Optional[str] = None,
) -> Dict[str, Any]:
    """逐链路结果 → ClientReplayCellReport 载荷（status 缺省按链路汇总）。"""
    if status is None:
        statuses = {str(item.get("status") or "FAIL").upper() for item in chain_reports}
        if not statuses:
            status = "ERROR"
        elif "FAIL" in statuses:
            status = "FAIL"
        elif "ERROR" in statuses:
            status = "ERROR"
        elif statuses - {"PASS"}:
            status = "WARNING"
        else:
            status = "PASS"
    return {
        "device_serial": device_serial,
        "status": str(status).upper(),
        "error_message": error_message,
        "current_stage": current_stage,
        "installed_package_snapshot": dict(installed_package) if installed_package else None,
        "chains": [dict(item) for item in chain_reports],
    }


def report_replay_cells(
    bridge: CloudBridge,
    run_id: int,
    cells: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return bridge.report_client_compatibility_run(run_id, {"cells": cells})


def execute_client_replay_run(
    run: Dict[str, Any],
    *,
    auth_headers: Optional[Dict[str, str]] = None,
    kernel_runner: Optional[Any] = None,
    bridge: Optional[CloudBridge] = None,
    installed_package: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """云端已建档的客户端回放 run → 逐设备 × 冻结链路本地执行 + report 收尾。

    run 为 client-compatibility create 响应（CompatibilityRunRead）；page_set_snapshot
    即冻结链路（含 first_path/checkpoints）。每台设备先上报 RUNNING 中间态（前端轮询
    可见进度），逐链路执行后上报终态（PASS/WARNING/FAIL/ERROR），云端全 cell 终态后
    自动收尾 run。单链路异常不中断整批，捕获为该链路 FAIL。
    """
    bridge = bridge or CloudBridge(auth_headers=dict(auth_headers or {}))
    run_id = int(run.get("id") or 0)
    if not run_id:
        raise ValueError("回放 run 缺少 id")
    chains = [dict(item) for item in (run.get("page_set_snapshot") or []) if isinstance(item, dict)]
    serials = [
        str(item).strip()
        for item in (run.get("device_serials") or [])
        if str(item or "").strip()
    ]
    if not serials:
        raise ValueError("回放 run 缺少 device_serials")
    runner = kernel_runner or run_frozen_chain_locally

    cells: List[Dict[str, Any]] = []
    for serial in serials:
        chain_reports: List[Dict[str, Any]] = []
        cell_status: Optional[str] = None
        cell_error: Optional[str] = None
        try:
            report_replay_cells(
                bridge,
                run_id,
                [
                    cell_report_from_chain_reports(
                        serial, [], status="RUNNING", current_stage="回放执行中"
                    )
                ],
            )
            for chain in chains:
                try:
                    result = runner(chain, device_serial=serial)
                except Exception as exc:
                    logger.warning(
                        "回放链路执行异常: run=%s chain=%s serial=%s error=%s",
                        run_id,
                        chain.get("chain_id") or chain.get("path_key"),
                        serial,
                        exc,
                    )
                    result = {
                        "status": "FAIL",
                        "reason": f"replay chain raised: {exc}",
                        "failure_type": "EXECUTION_ERROR",
                        "trace": [],
                        "completed_checkpoints": 0,
                        "duration": 0.0,
                    }
                chain_reports.append(chain_report_from_result(chain, result))
        except Exception as exc:
            cell_status = "ERROR"
            cell_error = f"replay execution failed: {exc}"
            logger.exception("客户端回放执行失败: run=%s serial=%s", run_id, serial)
        cells.append(
            cell_report_from_chain_reports(
                serial,
                chain_reports,
                installed_package=installed_package,
                status=cell_status,
                error_message=cell_error,
                current_stage="执行失败" if cell_status else "已完成",
            )
        )
    return report_replay_cells(bridge, run_id, cells)


def start_background_replay_run(
    run: Dict[str, Any],
    *,
    auth_headers: Optional[Dict[str, str]] = None,
    kernel_runner: Optional[Any] = None,
) -> threading.Thread:
    """后台执行回放 run（daemon 线程，与 runner_service.start_background_case_run 同模式）。

    线程内异常兜底上报 ERROR cell，保证云端 run 不悬空 RUNNING。
    """

    def _thread_main() -> None:
        try:
            execute_client_replay_run(
                run, auth_headers=auth_headers, kernel_runner=kernel_runner
            )
        except Exception as exc:
            logger.exception("客户端回放执行线程失败: run=%s", run.get("id"))
            try:
                bridge = CloudBridge(auth_headers=dict(auth_headers or {}))
                run_id = int(run.get("id") or 0)
                serials = [
                    str(item).strip()
                    for item in (run.get("device_serials") or [])
                    if str(item or "").strip()
                ]
                if run_id and serials:
                    report_replay_cells(
                        bridge,
                        run_id,
                        [
                            cell_report_from_chain_reports(
                                serial, [], status="ERROR", error_message=str(exc)
                            )
                            for serial in serials
                        ],
                    )
            except Exception:
                logger.exception("回放失败收尾上报也失败: run=%s", run.get("id"))

    thread = threading.Thread(
        target=_thread_main,
        name=f"local-replay-{run.get('id')}",
        daemon=True,
    )
    thread.start()
    return thread
