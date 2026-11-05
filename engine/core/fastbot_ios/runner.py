"""iOS 稳定性主循环：事件批 → 守护（apps/state）→ 崩溃判定/取证/回放 → 拉起。

报告形状对齐 Android 轻量档（performance_data / crash_events / summary …），复用
client-fastbot 上报与平台报告页。
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from engine.core.fastbot.summary import _compute_summary
from engine.core.fastbot_ios import perf as ios_perf
from engine.core.fastbot_ios import crash as ios_crash
from engine.core.fastbot_ios.crash import BUG_TYPE_JETSAM, CrashCollector, format_full_log
from engine.core.fastbot_ios.monkey import EventGenerator, EventWeights
from engine.core.fastbot_ios.replay import MjpegReplayRecorder
from engine.core.fastbot_ios.tools import udid_from_serial
from engine.core.fastbot_ios.wda import (
    APP_NOT_RUNNING,
    APP_RUNNING_FOREGROUND,
    WdaError,
    WdaHost,
    WdaSession,
)

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_WDA_ERRORS = 3
MAX_WDA_RECOVERIES = 5
# throttle 语义对齐 Android（事件间隔 ms）；WDA 每事件自带 ≈200ms 开销，扣除后作为批内 pause
WDA_INHERENT_EVENT_MS = 200
CRASH_REPORT_WAIT_SECONDS = 20


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def run_ios_fastbot_local(
    *,
    device_serial: str,
    bundle_id: str,
    duration: int,
    throttle: int = 500,
    ignore_crashes: bool = False,
    capture_log: bool = True,
    enable_performance_monitor: bool = False,
    enable_local_replay: bool = True,
    weights: Optional[EventWeights] = None,
    report_dir: Optional[str] = None,
    stop_flag: Optional[threading.Event] = None,
    host_factory: Callable[[str], WdaHost] = WdaHost,
) -> Dict[str, Any]:
    udid = udid_from_serial(device_serial)
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id:
        raise ValueError("Bundle ID 不能为空")
    duration = max(1, int(duration or 0))
    pause_ms = max(50, int(throttle or 0) - WDA_INHERENT_EVENT_MS)
    stop_flag = stop_flag or threading.Event()

    host = host_factory(udid)
    session: Optional[WdaSession] = None
    recorder: Optional[MjpegReplayRecorder] = None
    sampler: Optional[ios_perf.PerfSampler] = None
    crash_events: List[Dict[str, Any]] = []
    perf_data: List[Dict[str, Any]] = []
    degraded: List[str] = ["jank_frame_monitor", "perfetto"]
    stats: Dict[str, Any] = {
        "events": 0, "batches": 0, "wda_errors": 0, "wda_recoveries": 0,
        "relaunches": 0, "escapes": 0, "external_kills": 0, "home_presses": 0,
    }
    work_dir = report_dir or tempfile.mkdtemp(prefix="ios-fastbot-")
    collector = CrashCollector(udid, os.path.join(work_dir, "ips_pull"))
    app_names: Set[str] = {bundle_id.rsplit(".", 1)[-1]}
    pmd3 = ios_perf.resolve_pymobiledevice3()
    if pmd3 and capture_log:
        exec_name = ios_perf.resolve_executable(pmd3, udid, bundle_id)
        if exec_name:
            app_names.add(exec_name)  # Jetsam 报告只有进程名
    aborted_reason = ""

    def open_session() -> WdaSession:
        nonlocal session
        if session is not None:
            session.close()
        session = WdaSession(host, bundle_id)
        session.open()
        return session

    try:
        host.ensure_ready()
        open_session()
        if capture_log and collector.available:
            collector.baseline()
        elif capture_log:
            degraded.append("crash_log")
        if enable_local_replay:
            recorder = MjpegReplayRecorder(host.mjpeg_port, os.path.join(work_dir, "replays"))
            recorder.start()
        if enable_performance_monitor:
            sampler = ios_perf.PerfSampler(udid, bundle_id, perf_data)  # 内部复用可执行名查询
            if sampler.available:
                sampler.start()
            else:
                degraded.append("performance_monitor")
                sampler = None

        generator = EventGenerator(session.window["width"], session.window["height"], weights or EventWeights())
        started = time.monotonic()
        consecutive_errors = 0

        def handle_exit(count_external: bool = True) -> bool:
            """App 不在运行：拉崩溃报告 → 按报告时间截回放 → 记事件 → 是否继续。

            批内崩溃时 /actions 会挂起，守护发现可能滞后十几秒；回放窗口以 .ips
            自带时间戳为准回溯截取（缓冲额外保留 90s）。
            """
            detected_mono, detected_wall = time.monotonic(), time.time()
            reports = collector.collect_new(bundle_id, app_names, CRASH_REPORT_WAIT_SECONDS) if (
                capture_log and collector.available) else []
            if not reports:
                if not count_external:
                    return True
                # 无崩溃报告：可能被系统回收/外部终止，记为退出事件不计崩溃
                stats["external_kills"] += 1
                logger.info("iOS App 退出但未见崩溃报告（不计崩溃）: %s", bundle_id)
                return True
            replay_attached = False
            for info in reports:
                crash_wall = ios_crash.report_epoch(info)
                lag = min(max(0.0, detected_wall - crash_wall), 85.0) if crash_wall else 0.0
                event_mono = detected_mono - lag
                event_time = datetime.fromtimestamp(detected_wall - lag).strftime("%H:%M:%S")
                term = info.get("termination") or {}
                event: Dict[str, Any] = {
                    "time": event_time,
                    "type": "OOM" if info.get("bug_type") == BUG_TYPE_JETSAM else "CRASH",
                    "platform": "ios",
                    "full_log": format_full_log(info) if capture_log else "",
                    "detection_lag_sec": round(lag, 1),
                    "ios_report": {k: info.get(k) for k in (
                        "file", "bug_type", "exception", "termination", "pid", "app_version", "os_version",
                        "faulting_frames", "jetsam_killed")},
                }
                by_proc = str(term.get("byProc") or "")
                if by_proc and by_proc not in app_names and by_proc != info.get("proc_name"):
                    event["killed_by"] = by_proc  # 外部发信号（如调试服务注入），区别于 App 自身异常
                if recorder is not None and not replay_attached:
                    event["replay"] = recorder.capture(event["type"], event_time, event_mono)
                    replay_attached = True  # 同一窗口只挂第一条
                crash_events.append(event)
            return bool(ignore_crashes)

        while not stop_flag.is_set() and time.monotonic() - started < duration:
            try:
                state = session.app_state()
                if state != APP_RUNNING_FOREGROUND:
                    if state <= APP_NOT_RUNNING:
                        if stats["batches"] > 0 and not handle_exit():
                            aborted_reason = "crash"
                            break
                        session.launch()
                        stats["relaunches"] += 1
                    else:
                        stats["escapes"] += 1
                        session.activate()
                    time.sleep(1.0)
                    continue
                payload, kinds = generator.batch(pause_ms)
                if payload is None:
                    session.press_button("home")
                    stats["home_presses"] += 1
                else:
                    session.perform(payload)
                stats["events"] += len(kinds)
                stats["batches"] += 1
                consecutive_errors = 0
            except (WdaError, OSError, Exception) as exc:  # noqa: B014 — httpx 错误也走看门狗
                stats["wda_errors"] += 1
                consecutive_errors += 1
                logger.warning("WDA 调用失败(%s/%s): %s", consecutive_errors, MAX_CONSECUTIVE_WDA_ERRORS, exc)
                if consecutive_errors < MAX_CONSECUTIVE_WDA_ERRORS:
                    time.sleep(1.0)
                    continue
                if stats["wda_recoveries"] >= MAX_WDA_RECOVERIES:
                    aborted_reason = "wda_unrecoverable"
                    break
                stats["wda_recoveries"] += 1
                if not host.status_ok() and not host.recover():
                    aborted_reason = "wda_unrecoverable"
                    break
                try:
                    open_session()  # 注意：建会话会顺带拉起 App，崩溃需另行核对
                    consecutive_errors = 0
                except Exception as reopen_exc:
                    logger.warning("重建 WDA 会话失败: %s", reopen_exc)
                # 连续失败常因 App 在批内崩溃（WDA 挂起）：恢复后核对新崩溃报告，避免被重新拉起掩盖
                if stats["batches"] > 0 and not handle_exit(count_external=False):
                    aborted_reason = "crash"
                    break
        stats["elapsed_sec"] = round(time.monotonic() - started, 1)
        stats["event_counts"] = dict(generator.counts)
    finally:
        if sampler is not None:
            sampler.stop()
        if recorder is not None:
            stats["mjpeg_frames"] = recorder.frames_received
            recorder.stop()
        if session is not None:
            session.close()
        host.close()

    summary = _compute_summary(
        perf_data, crash_events, jank_data=[], jank_events=[], trace_artifacts=[],
        enable_performance_monitor=enable_performance_monitor, enable_jank_frame_monitor=False,
    )
    total_oom = sum(1 for e in crash_events if e.get("type") == "OOM")
    # Jetsam 被杀同样是用户可感知的闪退：计入崩溃总数，另列 total_oom
    summary["total_crashes"] = int(summary.get("total_crashes") or 0) + total_oom
    summary.update({
        "platform": "ios",
        "bundle_id": bundle_id,
        "local_replay_enabled": recorder is not None,
        "degraded": degraded,
        "ios_stats": stats,
        "total_oom": total_oom,
    })
    if aborted_reason:
        summary["aborted_reason"] = aborted_reason
    return {
        "performance_data": perf_data, "jank_data": [], "jank_events": [], "trace_artifacts": [],
        "crash_events": crash_events, "summary": summary,
    }
