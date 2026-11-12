# 来源：编排语义对齐 AutoDroid backend/fastbot/runner.run_fastbot_task（轻量档）+
#       backend/api/fastbot.py run_fastbot / _execute_fastbot_async（任务生命周期）。
"""Fastbot 本地轻量档执行 + client-fastbot 上报（P3-2 边界）。

轻量档 = 采集在本机设备执行、结果上报云端：
- 资源部署（framework/monkeyq/fastbot-thirdpart jar + libs）→ Monkey 智能探索；
- logcat 崩溃/ANR 监控（capture_log / ignore_crashes 语义同平台）；
- CPU/内存采样（enable_performance_monitor）；
- 崩溃异常回放（enable_local_replay）：本机滚动录制 scrcpy 码流，Crash / ANR 时
  截取前 30 秒 + 事件后补录并按产物上传云端（replays/<name>.mp4，平台读接口服务）；
- 汇总（summary 字段形状对齐 backend/fastbot/summary._compute_summary）；
- 上报云端 client-fastbot（POST /api/client-fastbot/ + /{id}/finish），复用平台
  读接口展示（GET /api/fastbot/tasks、/reports/{id} 零改动）。

未覆盖（重量级，如实后置）：jank 帧监控（gfxinfo/framestats）、perfetto 取证与
分析、startup/fluency 专项。请求这些开关时降级并在 summary 标注 degraded，
不伪造数据。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine import config_store
from engine.adb_scan import resolve_adb_binary
from engine.core import device_busy
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
# 异常回放窗口（对齐 backend/fastbot/runner.py 的 LOCAL_REPLAY_* 常量）
LOCAL_REPLAY_PRE_ROLL_SEC = 30
LOCAL_REPLAY_POST_ROLL_SEC = 5
LOCAL_REPLAY_SEGMENT_SEC = 5
# 启动录制前等设备流就绪的有界预算：两个任务可能同一时刻起跑（实测 80ms 内先后进入），
# 后到的设备流几百毫秒后才 ready，等一小段再降级，避免把「晚到」记成「能力缺失」
LOCAL_REPLAY_READY_WAIT_SEC = 8.0
LOCAL_REPLAY_READY_POLL_SEC = 0.2
# 本机回放产物目录：userData/fastbot_reports/<task_id>/replays/
LOCAL_REPORTS_DIR_NAME = "fastbot_reports"
LOCAL_REPORTS_RETAIN_DAYS = 7


def _pct(source: Dict[str, Any], key: str, default: int) -> int:
    """事件配比百分比：缺省/非法取默认，0 就是 0（旧写法 `or 默认值` 会把 0 变回默认）。"""
    value = source.get(key, default)
    if value is None or value == "":
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now().isoformat()


# --------------------------------------------------------------------------- #
#  异常回放：本机滚动录制 → Crash/ANR 截取 → 作为产物上传云端
# --------------------------------------------------------------------------- #

def local_report_dir(task_id: int) -> Path:
    """本机回放产物目录（userData/fastbot_reports/<task_id>）。"""
    return config_store.user_data_dir() / LOCAL_REPORTS_DIR_NAME / str(int(task_id))


def local_replay_path(task_id: int, filename: str) -> Optional[Path]:
    """回放 mp4 的本机路径；filename 只取 basename（拒绝路径穿越）。"""
    name = os.path.basename(str(filename or "").strip())
    if not name or name in (".", ".."):
        return None
    return local_report_dir(task_id) / "replays" / name


def prune_local_reports(keep_days: int = LOCAL_REPORTS_RETAIN_DAYS) -> None:
    """清理过期的本机回放产物目录（清理失败只记日志，绝不挡住任务启动）。"""
    root = config_store.user_data_dir() / LOCAL_REPORTS_DIR_NAME
    if not root.is_dir():
        return
    deadline = time.time() - max(1, int(keep_days)) * 86400
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        logger.warning("读取本机回放目录失败（跳过清理）: %s", exc)
        return
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < deadline:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _normalize_replay_payload(payload: Dict[str, Any], task_id: int) -> Dict[str, Any]:
    """回放 path 归一为平台约定（reports/fastbot/<id>/replays/<name>），不外泄本机路径。"""
    filename = str(payload.get("filename") or "")
    payload["path"] = (
        f"reports/fastbot/{int(task_id)}/replays/{filename}" if filename else ""
    )
    return payload


def _mark_replay_upload_failed(replay: Dict[str, Any], error: str) -> None:
    """上传失败的如实标注（前端展示原因，而不是留一个打不开的播放器）。"""
    replay["status"] = "FAILED"
    replay["error"] = error
    replay["filename"] = ""
    replay["path"] = ""


def publish_local_replays(bridge: CloudBridge, task_id: int, report: Dict[str, Any]) -> None:
    """把本机回放 mp4 作为 client-fastbot 产物上传云端，供报告侧直接播放。

    与平台自跑同约定：落 reports/fastbot/<id>/replays/<name>.mp4，
    由 GET /api/fastbot/replays/{id}/{filename} 服务。
    """
    for event in report.get("crash_events") or []:
        replay = event.get("replay") if isinstance(event, dict) else None
        if not isinstance(replay, dict) or replay.get("status") != "READY":
            continue
        path = local_replay_path(task_id, replay.get("filename"))
        if path is None or not path.is_file():
            _mark_replay_upload_failed(replay, "回放视频未生成，无法上传云端")
            continue
        try:
            bridge.upload_client_fastbot_artifact(task_id, str(path), f"replays/{path.name}")
        except Exception as exc:
            logger.warning(
                "回放视频上传失败: task=%s file=%s error=%s", task_id, path.name, exc
            )
            _mark_replay_upload_failed(replay, f"回放视频上传云端失败：{exc}")
            continue
        replay["path"] = f"reports/fastbot/{int(task_id)}/replays/{path.name}"


def _device_not_ready_type() -> Optional[type]:
    """DeviceNotReadyError 的类型；取不到就 None。

    这里必须自己吞掉 import 异常：本函数是在「启动录制失败」的 except 分支里被调用的，
    而那次失败本身就可能是这个 import 挂了（adbutils 缺失/损坏等）。若再炸一次，
    异常会逃出 run_fastbot_local，整轮稳定性测试陪葬 —— 降级继续跑才是原语义。
    """
    try:
        from engine.core.device_stream.manager import DeviceNotReadyError
    except Exception:
        return None
    return DeviceNotReadyError


def _local_replay_failure_status(exc: BaseException) -> str:
    """启动录制失败归类：设备流没就绪是并发起跑的可预期竞态（not_ready），其余是 error。

    调用点在 except 分支里，本函数自身不能再抛，所以归类逻辑整体再兜一层。
    """
    try:
        not_ready = _device_not_ready_type()
        if not_ready is not None and isinstance(exc, not_ready):
            return "not_ready"
        if isinstance(exc, ValueError) and "未就绪" in str(exc):
            return "not_ready"  # 兼容 manager 旧 ValueError 契约（测试替身/历史调用方）
    except Exception:
        return "error"
    return "error"


async def _wait_device_stream_ready(device_manager: Any, serial: str) -> bool:
    """有界等待设备流就绪；到点未就绪返回 False（不抛，避免把主流程一起拖垮）。

    走 to_thread 轮询：monkey / logcat 协程同时在跑，不能阻塞事件循环。
    """
    try:
        return bool(
            await asyncio.to_thread(
                device_manager.wait_until_ready,
                serial,
                LOCAL_REPLAY_READY_WAIT_SEC,
                LOCAL_REPLAY_READY_POLL_SEC,
            )
        )
    except Exception as exc:  # 只读查询失败按未就绪处理
        logger.warning("查询设备流就绪状态失败，按未就绪处理: %s", exc)
        return False


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
    task_id: int = 0,
    enable_local_replay: bool = True,
    report_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """轻量档本地采集（对齐 run_fastbot_task 的监控协程 + Monkey 主进程编排）。

    返回 client-fastbot report 六列：performance_data / jank_data / jank_events /
    trace_artifacts / crash_events / summary。
    enable_local_replay：本机滚动录制 scrcpy 码流，Crash/ANR 时截取回放；
    录制不可用（设备流未就绪/未给产物目录）时按降级处理并在 summary 标注，
    精确原因记在 summary.local_replay_status / local_replay_error。
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

    replay_started = False
    replay_dir = str(report_dir or "")
    # 精确状态：on / disabled / not_ready / error（summary.local_replay_status）。
    # 注意 local_replay_enabled 与 summary.degraded 的语义与格式一个字都不能动：
    # 前端按这两个字段渲染「未开启（客户端降级）」。
    replay_status = "disabled"
    replay_error: Optional[str] = None
    if enable_local_replay:
        if not replay_dir:
            # run_and_report 恒传 local_report_dir(task_id)，这里只兜外部直调
            replay_status = "error"
            replay_error = "未提供回放产物目录"
            logger.warning("未提供回放产物目录，本次不录制异常回放")
        else:
            try:
                from engine.core.device_stream.manager import device_manager

                os.makedirs(replay_dir, exist_ok=True)
                # 设备流常常晚几百毫秒才 ready（两任务同毫秒起跑），等一小段再降级
                if not await _wait_device_stream_ready(device_manager, device_serial):
                    replay_status = "not_ready"
                    replay_error = (
                        f"设备流 {LOCAL_REPLAY_READY_WAIT_SEC:g}s 内未就绪: {device_serial}"
                    )
                    logger.warning("启动异常回放录制失败，本次报告无崩溃回放: %s", replay_error)
                else:
                    device_manager.start_recording(
                        device_serial,
                        int(task_id or 0),
                        replay_dir,
                        LOCAL_REPLAY_PRE_ROLL_SEC,
                        LOCAL_REPLAY_POST_ROLL_SEC,
                        LOCAL_REPLAY_SEGMENT_SEC,
                    )
                    replay_started = True
                    replay_status = "on"
            except Exception as exc:
                replay_status = _local_replay_failure_status(exc)
                replay_error = f"{type(exc).__name__}: {exc}"
                logger.warning("启动异常回放录制失败，本次报告无崩溃回放: %s", exc)

    async def _capture_local_replay(event_type: str, event_time: str) -> Optional[Dict[str, Any]]:
        """Crash/ANR 触发：截取回放窗口（含事件后补录）并返回事件 replay 载荷。"""
        if not replay_started:
            return None
        from engine.core.device_stream.manager import device_manager

        try:
            result = await asyncio.to_thread(
                device_manager.capture_replay, device_serial, event_type, event_time
            )
        except Exception as exc:
            logger.warning("截取异常回放失败: %s", exc)
            return {"status": "FAILED", "error": f"截取回放失败：{exc}"}
        return _normalize_replay_payload(result.to_dict(), task_id) if result else None

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
                replay_callback=_capture_local_replay if replay_started else None,
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
        if replay_started:
            try:
                from engine.core.device_stream.manager import device_manager

                device_manager.stop_recording(device_serial)
            except Exception as exc:
                logger.warning("停止异常回放录制失败（忽略）: %s", exc)

    summary = _compute_summary(
        perf_data,
        crash_events,
        jank_data=[],
        jank_events=[],
        trace_artifacts=[],
        enable_performance_monitor=enable_performance_monitor,
        enable_jank_frame_monitor=False,
    )
    summary["local_replay_enabled"] = bool(replay_started)
    summary["local_replay_status"] = replay_status
    if replay_error:  # 健康时不留噪音字段
        summary["local_replay_error"] = replay_error
    degraded = ["jank_frame_monitor"]
    if enable_local_replay and not replay_started:
        degraded.append("local_replay")
    degraded.append("perfetto")
    summary["degraded"] = degraded

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


def _run_ios(*, weights: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """iOS 分支：主机驱动 WDA（engine/core/fastbot_ios）；事件权重映射 Android 四档。"""
    from engine.core.fastbot_ios.monkey import EventWeights
    from engine.core.fastbot_ios.runner import run_ios_fastbot_local

    event_weights = EventWeights()  # 智能默认：点击为主（见 EventWeights 说明）
    if weights.get("enable_custom_event_weights"):
        touch = _pct(weights, "pct_touch", 40)
        motion = _pct(weights, "pct_motion", 30)
        back = _pct(weights, "pct_majornav", 15)
        syskeys = _pct(weights, "pct_syskeys", 5)
        # 页面上「其他随机事件」= 100 - 已分配，iOS 侧落到长按
        event_weights = EventWeights(
            touch=touch, motion=motion, back=back, syskeys=syskeys,
            long_press=max(0, 100 - (touch + motion + back + syskeys)),
        )
    return run_ios_fastbot_local(
        bundle_id=kwargs.pop("package_name"), weights=event_weights, **kwargs
    )


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
    enable_local_replay: bool = True,
) -> None:
    """后台线程入口：本地采集 → 回放产物上传 → client-fastbot finish 收尾（异常也收尾 FAILED）。"""
    weights = dict(event_weights or {})

    def _thread_main() -> None:
        try:
            _run_and_finish()
        finally:
            device_busy.release(device_serial)  # 收尾出任何异常都不能把设备一直占着

    def _run_and_finish() -> None:
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
            from engine.core.fastbot_ios.tools import is_ios_serial

            if is_ios_serial(device_serial):
                report = _run_ios(
                    device_serial=device_serial,
                    package_name=package_name,
                    duration=duration,
                    throttle=throttle,
                    ignore_crashes=ignore_crashes,
                    capture_log=capture_log,
                    enable_performance_monitor=enable_performance_monitor,
                    enable_local_replay=enable_local_replay,
                    weights=weights,
                    report_dir=str(local_report_dir(task_id)),
                )
            else:
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
                        pct_touch=_pct(weights, "pct_touch", 40),
                        pct_motion=_pct(weights, "pct_motion", 30),
                        pct_syskeys=_pct(weights, "pct_syskeys", 5),
                        pct_majornav=_pct(weights, "pct_majornav", 15),
                        task_id=task_id,
                        enable_local_replay=enable_local_replay,
                        report_dir=str(local_report_dir(task_id)),
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
        if report.get("crash_events"):
            # 回放视频随报告一并云端化：报告在任何入口都能直接播放
            try:
                publish_local_replays(bridge, task_id, report)
            except Exception as exc:
                logger.warning("回放产物上传阶段异常（已忽略）: %s", exc)
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
            "Fastbot 本地执行收尾: task=%s status=%s duration=%.1fs crashes=%s anrs=%s replay=%s replay_status=%s",
            task_id,
            status,
            time.time() - started,
            summary.get("total_crashes"),
            summary.get("total_anrs"),
            summary.get("local_replay_enabled"),
            summary.get("local_replay_status"),
        )

    threading.Thread(
        target=_thread_main, name=f"fastbot-local-{task_id}-{device_serial}", daemon=True
    ).start()


def start_fastbot_run(bridge: CloudBridge, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/fastbot/run 本地拦截：登记云端任务 → 后台本地采集 → 返回 FastbotTaskRead。

    本地采集覆盖异常回放（enable_local_replay，默认开）；jank 帧监控请求降级
    （summary.degraded 标注）。
    """
    device_serial = str(payload.get("device_serial") or "").strip()
    package_name = str(payload.get("package_name") or "").strip()
    if not device_serial or not package_name:
        raise ValueError("package_name / device_serial 不能为空")
    from engine.core.fastbot_ios.tools import is_ios_serial, udid_from_serial

    if is_ios_serial(device_serial):
        udid_from_serial(device_serial)  # 非法 UDID → ValueError（HTTP 400）
        validate_package_name(package_name)  # Bundle ID 与包名同字符集
    else:
        # T5：入口即白名单校验（HTTP 400 显式拒绝非法 serial/包名）
        validate_device_serial(device_serial)
        validate_package_name(package_name)

    duration = int(payload.get("duration") or 600)
    throttle = int(payload.get("throttle") or 500)
    ignore_crashes = bool(payload.get("ignore_crashes", False))
    capture_log = bool(payload.get("capture_log", True))
    enable_performance_monitor = bool(payload.get("enable_performance_monitor", False))
    enable_local_replay = bool(payload.get("enable_local_replay", True))
    prune_local_reports()

    if device_busy.has_local_run(device_serial):
        raise ValueError("该设备正在执行用例，请等执行结束后再试")
    if not device_busy.try_acquire(device_serial):
        raise ValueError("该设备正在跑稳定性测试，请等本次结束后再试")
    run_id = str(uuid.uuid4())
    try:
        created = _create_and_start(
            bridge,
            run_id=run_id,
            device_serial=device_serial,
            package_name=package_name,
            duration=duration,
            throttle=throttle,
            ignore_crashes=ignore_crashes,
            capture_log=capture_log,
            enable_performance_monitor=enable_performance_monitor,
            enable_local_replay=enable_local_replay,
            payload=payload,
        )
    except BaseException:
        device_busy.release(device_serial)  # 没起来就别占着设备
        raise
    return created


def _create_and_start(
    bridge: CloudBridge,
    *,
    run_id: str,
    device_serial: str,
    package_name: str,
    duration: int,
    throttle: int,
    ignore_crashes: bool,
    capture_log: bool,
    enable_performance_monitor: bool,
    enable_local_replay: bool,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
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
        enable_local_replay=enable_local_replay,
        event_weights={
            "enable_custom_event_weights": bool(payload.get("enable_custom_event_weights", False)),
            "pct_touch": _pct(payload, "pct_touch", 40),
            "pct_motion": _pct(payload, "pct_motion", 30),
            "pct_syskeys": _pct(payload, "pct_syskeys", 5),
            "pct_majornav": _pct(payload, "pct_majornav", 15),
        },
    )
    return created
