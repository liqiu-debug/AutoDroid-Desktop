# 来源：编排语义对齐 AutoDroid backend/fastbot/runner.run_fastbot_task（轻量档）+
#       backend/api/fastbot.py run_fastbot / _execute_fastbot_async（任务生命周期）。
"""Fastbot 本地轻量档执行 + client-fastbot 上报（P3-2 边界）。

轻量档 = 采集在本机设备执行、结果上报云端：
- 资源部署（framework/monkeyq/fastbot-thirdpart jar + libs）→ Monkey 智能探索；
- logcat 崩溃/ANR 监控（capture_log / ignore_crashes 语义同平台）；
- CPU/内存采样（enable_performance_monitor）；
- 汇总（summary 字段形状对齐 backend/fastbot/summary._compute_summary）；
- 上报云端 client-fastbot（POST /api/client-fastbot/ + /{id}/finish），复用平台
  读接口展示（GET /api/fastbot/tasks、/reports/{id} 零改动）。

未覆盖（重量级，如实后置）：jank 帧监控（gfxinfo/framestats）、perfetto 取证与
分析、本地回放录像、startup/fluency 专项。请求这些开关时降级并在 summary 标注
degraded，不伪造数据。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from engine.adb_scan import resolve_adb_binary
from engine.core.cloud_bridge import CloudBridge
from engine.core.fastbot import logcat as fastbot_logcat
from engine.core.fastbot import monkey as fastbot_monkey
from engine.core.fastbot import perf_monitor as fastbot_perf
from engine.core.fastbot.adb import (
    ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    _adb_shell,
    validate_device_serial,
    validate_package_name,
)
from engine.core.fastbot.summary import _compute_summary

logger = logging.getLogger(__name__)

MONITOR_SHUTDOWN_TIMEOUT_SECONDS = 12


def _now_iso() -> str:
    return datetime.now().isoformat()


async def run_fastbot_local(
    *,
    device_serial: str,
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    capture_log: bool = True,
    enable_performance_monitor: bool = True,
    enable_custom_event_weights: bool = False,
    pct_touch: int = 40,
    pct_motion: int = 30,
    pct_syskeys: int = 5,
    pct_majornav: int = 15,
) -> Dict[str, Any]:
    """轻量档本地采集（对齐 run_fastbot_task 的监控协程 + Monkey 主进程编排）。

    返回 client-fastbot report 六列：performance_data / jank_data / jank_events /
    trace_artifacts / crash_events / summary。
    """
    duration = max(1, int(duration or 0))
    throttle = max(0, int(throttle or 0))
    # T5：serial/包名严格白名单（非法直接拒绝，杜绝命令注入面）
    device_serial = validate_device_serial(device_serial)
    package_name = validate_package_name(package_name)

    await fastbot_monkey.push_fastbot_assets(device_serial)
    monkey_cmd = fastbot_monkey._build_monkey_command(
        package_name,
        duration,
        throttle,
        ignore_crashes,
        enable_custom_event_weights,
        pct_touch,
        pct_motion,
        pct_syskeys,
        pct_majornav,
    )

    perf_data: List[Dict[str, Any]] = []
    crash_events: List[Dict[str, Any]] = []
    stop_event = asyncio.Event()
    abort_event = asyncio.Event()
    should_abort = not ignore_crashes

    await _adb_shell(device_serial, "logcat -c", timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS)

    monitor_tasks: List[asyncio.Task] = []
    if enable_performance_monitor:
        monitor_tasks.append(
            asyncio.create_task(
                fastbot_perf._monitor_performance(device_serial, package_name, stop_event, perf_data)
            )
        )
    monitor_tasks.append(
        asyncio.create_task(
            fastbot_logcat._monitor_logcat(
                device_serial,
                package_name,
                stop_event,
                crash_events,
                capture_log,
                abort_on_crash=should_abort,
                abort_event=abort_event,
            )
        )
    )

    try:
        # T5：exec 参数数组（不经宿主 shell）；monkey_cmd 整串交设备侧 shell 解释
        monkey_proc = await asyncio.create_subprocess_exec(
            resolve_adb_binary(), "-s", device_serial, "shell", monkey_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        monkey_comm = asyncio.create_task(monkey_proc.communicate())
        abort_wait = asyncio.create_task(abort_event.wait())
        done, pending = await asyncio.wait(
            {monkey_comm, abort_wait},
            timeout=duration + 60,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_wait in done:
            logger.warning("检测到崩溃且容错策略为立即停止，正在终止 Monkey 进程")
            monkey_proc.terminate()
            try:
                await asyncio.wait_for(monkey_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                monkey_proc.kill()
            monkey_comm.cancel()
        elif monkey_comm not in done:
            monkey_proc.terminate()
            try:
                await monkey_proc.wait()
            except Exception:
                pass
            logger.warning("Monkey 进程超时，已强制终止")
            monkey_comm.cancel()
        for task in pending:
            task.cancel()
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(*monitor_tasks, return_exceptions=True),
                timeout=MONITOR_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            for task in monitor_tasks:
                task.cancel()
            await asyncio.gather(*monitor_tasks, return_exceptions=True)

    summary = _compute_summary(
        perf_data,
        crash_events,
        jank_data=[],
        jank_events=[],
        trace_artifacts=[],
        enable_performance_monitor=enable_performance_monitor,
        enable_jank_frame_monitor=False,
    )
    summary["local_replay_enabled"] = False
    summary["degraded"] = ["jank_frame_monitor", "local_replay", "perfetto"]

    return {
        "performance_data": perf_data,
        "jank_data": [],
        "jank_events": [],
        "trace_artifacts": [],
        "crash_events": crash_events,
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
#  任务生命周期（对齐 api/fastbot.py run_fastbot → _execute_fastbot_background）
# --------------------------------------------------------------------------- #

def create_cloud_task(bridge: CloudBridge, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/client-fastbot/ → FastbotTaskRead（run_id 幂等）。"""
    return bridge.create_client_fastbot(payload)


def run_and_report(
    *,
    task_id: int,
    auth_headers: Dict[str, str],
    device_serial: str,
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    capture_log: bool,
    enable_performance_monitor: bool,
    event_weights: Optional[Dict[str, Any]] = None,
) -> None:
    """后台线程入口：本地采集 → client-fastbot finish 收尾（异常也收尾 FAILED）。"""
    weights = dict(event_weights or {})

    def _thread_main() -> None:
        started = time.time()
        status = "COMPLETED"
        report: Dict[str, Any] = {
            "performance_data": [],
            "jank_data": [],
            "jank_events": [],
            "trace_artifacts": [],
            "crash_events": [],
            "summary": {},
        }
        try:
            report = asyncio.run(
                run_fastbot_local(
                    device_serial=device_serial,
                    package_name=package_name,
                    duration=duration,
                    throttle=throttle,
                    ignore_crashes=ignore_crashes,
                    capture_log=capture_log,
                    enable_performance_monitor=enable_performance_monitor,
                    enable_custom_event_weights=bool(weights.get("enable_custom_event_weights")),
                    pct_touch=int(weights.get("pct_touch", 40) or 40),
                    pct_motion=int(weights.get("pct_motion", 30) or 30),
                    pct_syskeys=int(weights.get("pct_syskeys", 5) or 5),
                    pct_majornav=int(weights.get("pct_majornav", 15) or 15),
                )
            )
        except Exception as exc:
            logger.exception("Fastbot 本地执行异常: task=%s device=%s", task_id, device_serial)
            status = "FAILED"
            report["summary"] = {
                "error": str(exc),
                "total_crashes": 0,
                "total_anrs": 0,
            }

        summary = report.get("summary") or {}
        bridge = CloudBridge(auth_headers=auth_headers)
        try:
            bridge.finish_client_fastbot(
                task_id,
                {
                    "status": status,
                    "total_crashes": int(summary.get("total_crashes") or 0),
                    "total_anrs": int(summary.get("total_anrs") or 0),
                    "finished_at": _now_iso(),
                    "report": report,
                },
            )
        except Exception as exc:
            logger.warning("client-fastbot 收尾上报失败（结果仅本地日志可见）: %s", exc)
        logger.info(
            "Fastbot 本地执行收尾: task=%s status=%s duration=%.1fs crashes=%s anrs=%s",
            task_id,
            status,
            time.time() - started,
            summary.get("total_crashes"),
            summary.get("total_anrs"),
        )

    threading.Thread(
        target=_thread_main, name=f"fastbot-local-{task_id}-{device_serial}", daemon=True
    ).start()


def start_fastbot_run(bridge: CloudBridge, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/fastbot/run 本地拦截：登记云端任务 → 后台本地采集 → 返回 FastbotTaskRead。

    轻量档：enable_jank_frame_monitor / enable_local_replay 请求降级（summary.degraded 标注）。
    """
    device_serial = str(payload.get("device_serial") or "").strip()
    package_name = str(payload.get("package_name") or "").strip()
    if not device_serial or not package_name:
        raise ValueError("package_name / device_serial 不能为空")
    if str(device_serial).startswith("ios:"):
        raise ValueError("Fastbot 仅支持 Android 设备")
    # T5：入口即白名单校验（HTTP 400 显式拒绝非法 serial/包名）
    validate_device_serial(device_serial)
    validate_package_name(package_name)

    duration = int(payload.get("duration") or 600)
    throttle = int(payload.get("throttle") or 500)
    ignore_crashes = bool(payload.get("ignore_crashes", False))
    capture_log = bool(payload.get("capture_log", True))
    enable_performance_monitor = bool(payload.get("enable_performance_monitor", False))

    run_id = str(uuid.uuid4())
    created = create_cloud_task(
        bridge,
        {
            "run_id": run_id,
            "kind": "fastbot",
            "package_name": package_name,
            "device_serial": device_serial,
            "duration": duration,
            "throttle": throttle,
            "ignore_crashes": ignore_crashes,
            "capture_log": capture_log,
            "started_at": _now_iso(),
        },
    )
    task_id = int((created or {}).get("id") or 0)
    if not task_id:
        raise RuntimeError("client-fastbot 创建响应缺少任务 id")

    run_and_report(
        task_id=task_id,
        auth_headers=dict(bridge.auth_headers),
        device_serial=device_serial,
        package_name=package_name,
        duration=duration,
        throttle=throttle,
        ignore_crashes=ignore_crashes,
        capture_log=capture_log,
        enable_performance_monitor=enable_performance_monitor,
        event_weights={
            "enable_custom_event_weights": bool(payload.get("enable_custom_event_weights", False)),
            "pct_touch": int(payload.get("pct_touch", 40) or 40),
            "pct_motion": int(payload.get("pct_motion", 30) or 30),
            "pct_syskeys": int(payload.get("pct_syskeys", 5) or 5),
            "pct_majornav": int(payload.get("pct_majornav", 15) or 15),
        },
    )
    return created
