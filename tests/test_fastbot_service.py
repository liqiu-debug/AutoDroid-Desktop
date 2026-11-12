"""Fastbot 本地轻量档（P3-2）测试。

覆盖：
- vendor monkey 资源目录/命令拼接（assets/fastbot 随客户端分发）
- run_fastbot_local 轻量档报告六列形状 + 汇总（crash 计数 / 降级标注）
- 异常回放：滚动录制编排（start/capture/stop）、事件 replay 载荷、产物上传
- run_and_report 收尾上报（client-fastbot finish 载荷形状 + 回放先上传后收尾）
- start_fastbot_run：云端登记 + 后台执行入参
- POST /api/fastbot/run 本地拦截（不反代云端 /api/fastbot/run）+ 未在线设备 400
- 平台 client-fastbot 契约字段漂移锁（AST，无需导入平台依赖栈）
"""
from __future__ import annotations

import asyncio
import ast
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

from engine import config_store, presence
from engine.core import cloud_bridge, fastbot_service
from engine.core.device_stream import recorder as device_recorder
from engine.core.fastbot import logcat as fastbot_logcat
from engine.core.fastbot import monkey as fastbot_monkey
from engine.core.fastbot import perf_monitor as fastbot_perf

PLATFORM_CLIENT_FASTBOT = (
    Path(__file__).resolve().parents[2] / "AutoDroid" / "backend" / "api" / "client_fastbot.py"
)


# --------------------------- vendor 边界 --------------------------- #

def test_monkey_assets_ship_with_client():
    assert Path(fastbot_monkey.FASTBOT_ASSETS_DIR).is_dir()
    for jar in fastbot_monkey.DEVICE_JARS:
        assert (Path(fastbot_monkey.FASTBOT_ASSETS_DIR) / jar).is_file(), f"缺少 {jar}"
    cmd = fastbot_monkey._build_monkey_command("com.x", 120, 500, True, True, 50, 20, 5, 10)
    assert "com.android.commands.monkey.Monkey" in cmd
    assert "--running-minutes 2" in cmd
    assert "--ignore-crashes" in cmd
    assert "--pct-touch 50" in cmd


# --------------------------- 本地采集 --------------------------- #

class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"", b""

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


@pytest.fixture()
def fake_collection(monkeypatch):
    """轻量档采集替身：monkey 进程瞬时结束 + logcat 抓到一次崩溃。"""
    calls: Dict[str, Any] = {}

    async def _push(serial):
        calls["pushed"] = serial

    async def _adb_shell(serial, cmd, timeout=None):
        calls.setdefault("shell", []).append(cmd)
        return ""

    async def _monitor_logcat(serial, package, stop_event, crash_events, capture_log, **kwargs):
        calls["logcat_kwargs"] = dict(kwargs)
        event = {"type": "CRASH", "time": "t", "package": package, "message": "boom"}
        replay_callback = kwargs.get("replay_callback")
        if replay_callback:
            replay = await replay_callback("CRASH", "t")
            if replay:
                event["replay"] = replay
        crash_events.append(event)
        await stop_event.wait()

    async def _monitor_performance(serial, package, stop_event, perf_data, interval=10):
        perf_data.append({"time": "t", "cpu": 10.0, "mem": 100.0})
        await stop_event.wait()

    async def _create_subprocess_exec(*args, **kwargs):
        calls["monkey_argv"] = list(args)
        return _FakeProc()

    monkeypatch.setattr(fastbot_monkey, "push_fastbot_assets", _push)
    monkeypatch.setattr(fastbot_service, "_adb_shell", _adb_shell)
    monkeypatch.setattr(fastbot_logcat, "_monitor_logcat", _monitor_logcat)
    monkeypatch.setattr(fastbot_perf, "_monitor_performance", _monitor_performance)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)
    # adb 可执行路径走 adb_scan 唯一入口解析（此处固定假路径，断言 argv[0] 贯通）
    monkeypatch.setattr("engine.adb_scan.find_adb_path", lambda explicit=None: "/resolved/adb")
    return calls


def test_run_fastbot_local_light_profile_report_shape(fake_collection):
    result = asyncio.run(
        fastbot_service.run_fastbot_local(
            device_serial="USB1",
            package_name="com.x",
            duration=1,
            throttle=500,
            ignore_crashes=True,
            capture_log=True,
            enable_performance_monitor=True,
        )
    )
    # client-fastbot report 六列（backend/api/client_fastbot.ClientFastbotReportPayload）
    assert set(result) == {
        "performance_data", "jank_data", "jank_events",
        "trace_artifacts", "crash_events", "summary",
    }
    summary = result["summary"]
    assert summary["total_crashes"] == 1 and summary["total_anrs"] == 0
    assert summary["avg_cpu"] == 10.0 and summary["avg_mem"] == 100.0
    assert summary["performance_monitor_enabled"] is True
    # 未覆盖能力如实标注降级，不伪造数据
    assert set(summary["degraded"]) == {"jank_frame_monitor", "local_replay", "perfetto"}
    # T5：monkey 走 create_subprocess_exec 参数数组（serial 独立 argv，不经宿主 shell）；
    # adb 可执行路径经 engine.adb_scan 唯一入口解析
    argv = fake_collection["monkey_argv"]
    assert argv[:4] == ["/resolved/adb", "-s", "USB1", "shell"]
    assert "com.android.commands.monkey.Monkey" in argv[4]


# --------------------------- 异常回放（本地滚动录制） --------------------------- #

@pytest.fixture(autouse=True)
def _fast_replay_ready_budget(monkeypatch):
    """设备流就绪等待压到毫秒级：走完整等待/超时路径，但测试不真等 8 秒。"""
    monkeypatch.setattr(fastbot_service, "LOCAL_REPLAY_READY_WAIT_SEC", 0.6)
    monkeypatch.setattr(fastbot_service, "LOCAL_REPLAY_READY_POLL_SEC", 0.01)


def _is_ready_script(monkeypatch, results: List[bool]) -> List[str]:
    """device_manager.is_ready 替身：按脚本返回（末值重复），返回探测记录。"""
    from engine.core.device_stream.manager import device_manager

    pending = list(results)
    probes: List[str] = []

    def _probe(serial: str) -> bool:
        probes.append(serial)
        return pending.pop(0) if pending else results[-1]

    monkeypatch.setattr(device_manager, "is_ready", _probe)
    return probes


@pytest.fixture()
def fake_recorder(monkeypatch):
    """device_manager 录制替身：设备流已就绪，记录 start/capture/stop 入参。

    就绪探测也替身掉：录制可用就意味着设备流就绪，用例要考「晚到」时自己再覆盖 is_ready。
    """
    from engine.core.device_stream.manager import device_manager

    calls: Dict[str, Any] = {}

    def _is_ready(serial: str) -> bool:
        return True

    def _start(serial, task_id, report_dir, pre_roll_sec=30, post_roll_sec=5, segment_sec=5):
        calls["start"] = {
            "serial": serial,
            "task_id": task_id,
            "report_dir": report_dir,
            "pre_roll_sec": pre_roll_sec,
            "post_roll_sec": post_roll_sec,
            "segment_sec": segment_sec,
        }
        return object()

    def _capture(serial, event_type, event_time):
        calls.setdefault("capture", []).append((serial, event_type, event_time))
        return device_recorder.ReplayCaptureResult(
            status="READY",
            path="../../relative/local/path/crash_t_01.mp4",
            filename="crash_t_01.mp4",
            pre_roll_sec=30,
            post_roll_sec=5,
            duration_sec=35,
        )

    def _stop(serial):
        calls.setdefault("stop", []).append(serial)

    monkeypatch.setattr(device_manager, "is_ready", _is_ready)
    monkeypatch.setattr(device_manager, "start_recording", _start)
    monkeypatch.setattr(device_manager, "capture_replay", _capture)
    monkeypatch.setattr(device_manager, "stop_recording", _stop)
    return calls


def _run_local(monkeypatch, tmp_path, **overrides):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    kwargs: Dict[str, Any] = {
        "device_serial": "USB1",
        "package_name": "com.x",
        "duration": 1,
        "throttle": 500,
        "ignore_crashes": True,
        "capture_log": True,
        "enable_performance_monitor": True,
        "task_id": 9,
        "report_dir": str(fastbot_service.local_report_dir(9)),
    }
    kwargs.update(overrides)
    return asyncio.run(fastbot_service.run_fastbot_local(**kwargs))


def test_run_fastbot_local_records_replay_and_marks_enabled(
    fake_collection, fake_recorder, tmp_path, monkeypatch
):
    result = _run_local(monkeypatch, tmp_path)
    summary = result["summary"]
    assert summary["local_replay_enabled"] is True
    assert summary["local_replay_status"] == "on"
    # 已实现的回放不再计入降级项（jank 帧监控 / perfetto 仍未覆盖）
    assert summary["degraded"] == ["jank_frame_monitor", "perfetto"]

    start = fake_recorder["start"]
    assert start["serial"] == "USB1" and start["task_id"] == 9
    assert Path(start["report_dir"]).is_dir(), "回放产物目录应已建好"
    assert (start["pre_roll_sec"], start["post_roll_sec"]) == (30, 5)

    event = result["crash_events"][0]
    assert fake_recorder["capture"] == [("USB1", "CRASH", "t")]
    # path 归一为平台约定，不外泄本机路径（文件名不变，前端按 filename 取）
    assert event["replay"]["status"] == "READY"
    assert event["replay"]["filename"] == "crash_t_01.mp4"
    assert event["replay"]["path"] == "reports/fastbot/9/replays/crash_t_01.mp4"
    assert fake_recorder["stop"] == ["USB1"]


def test_run_fastbot_local_replay_waits_for_stream_then_starts(
    fake_collection, fake_recorder, tmp_path, monkeypatch
):
    """设备流晚到几百毫秒是常态（实测两任务 80ms 内同时起跑）：等就绪再启动，不误降级。"""
    probes = _is_ready_script(monkeypatch, [False, False, True])
    result = _run_local(monkeypatch, tmp_path)
    summary = result["summary"]
    assert summary["local_replay_enabled"] is True
    assert summary["local_replay_status"] == "on"
    assert "local_replay_error" not in summary
    assert summary["degraded"] == ["jank_frame_monitor", "perfetto"]
    assert len(probes) == 3, "应按轮询等到就绪，而不是第一次没 ready 就直接降级"
    assert fake_recorder["start"]["serial"] == "USB1"
    assert "replay" in result["crash_events"][0]


def test_run_fastbot_local_replay_stream_never_ready_degrades(
    fake_collection, fake_recorder, tmp_path, monkeypatch
):
    """设备流一直没就绪：有界等待到点如实降级（not_ready），绝不半路启动录制。"""
    probes = _is_ready_script(monkeypatch, [False])
    result = _run_local(monkeypatch, tmp_path)
    summary = result["summary"]
    assert summary["local_replay_enabled"] is False
    assert summary["local_replay_status"] == "not_ready"
    assert summary["local_replay_error"], "降级原因必须落进 local_replay_error"
    assert "USB1" in summary["local_replay_error"]
    assert summary["degraded"] == [
        "jank_frame_monitor", "local_replay", "perfetto",
    ]
    assert len(probes) >= 2, "应在等待预算内轮询，而不是只探测一次"
    assert "start" not in fake_recorder, "设备流未就绪时不得启动录制"
    assert fake_collection["logcat_kwargs"]["replay_callback"] is None
    assert "replay" not in result["crash_events"][0]


def test_run_fastbot_local_replay_start_failure_marks_error(
    fake_collection, fake_recorder, tmp_path, monkeypatch
):
    """录制启动抛出非「未就绪」异常：归 error，原因进 local_replay_error。"""
    from engine.core.device_stream.manager import device_manager

    def _boom(*args, **kwargs):
        raise RuntimeError("scrcpy 码流通道打不开")

    monkeypatch.setattr(device_manager, "start_recording", _boom)
    result = _run_local(monkeypatch, tmp_path)
    summary = result["summary"]
    assert summary["local_replay_enabled"] is False
    assert summary["local_replay_status"] == "error"
    assert "scrcpy 码流通道打不开" in summary["local_replay_error"]
    assert summary["degraded"] == [
        "jank_frame_monitor", "local_replay", "perfetto",
    ]


def test_run_fastbot_local_replay_late_not_ready_stays_not_ready(
    fake_collection, fake_recorder, tmp_path, monkeypatch
):
    """就绪检查后的竞态（DeviceNotReadyError）仍算 not_ready，不算故障。"""
    from engine.core.device_stream.manager import DeviceNotReadyError, device_manager

    def _late(serial, *args, **kwargs):
        raise DeviceNotReadyError(f"设备 {serial} 未就绪")

    monkeypatch.setattr(device_manager, "start_recording", _late)
    result = _run_local(monkeypatch, tmp_path)
    summary = result["summary"]
    assert summary["local_replay_enabled"] is False
    assert summary["local_replay_status"] == "not_ready"
    assert "未就绪" in summary["local_replay_error"]


def test_run_fastbot_local_replay_opt_out_stays_quiet(fake_collection, tmp_path, monkeypatch):
    """用户关掉开关：不启动录制也不查设备流，且不标注降级（不是"请求了但没做"）。"""
    from engine.core.device_stream.manager import device_manager

    def _unexpected(*args, **kwargs):
        raise AssertionError("关闭异常回放时不应启动录制/查询设备流")

    monkeypatch.setattr(device_manager, "start_recording", _unexpected)
    monkeypatch.setattr(device_manager, "is_ready", _unexpected)
    result = _run_local(monkeypatch, tmp_path, enable_local_replay=False)
    assert result["summary"]["local_replay_enabled"] is False
    assert result["summary"]["local_replay_status"] == "disabled"
    assert "local_replay_error" not in result["summary"]
    assert result["summary"]["degraded"] == ["jank_frame_monitor", "perfetto"]


def test_run_fastbot_local_summary_replay_fields_keep_legacy_format(
    fake_collection, fake_recorder, tmp_path, monkeypatch
):
    """local_replay_enabled / degraded 的语义与格式一个字都不能变：前端按这两个字段渲染。"""
    started = _run_local(monkeypatch, tmp_path)["summary"]
    assert started["local_replay_enabled"] is True
    assert isinstance(started["local_replay_enabled"], bool)
    assert started["degraded"] == ["jank_frame_monitor", "perfetto"]

    opted_out = _run_local(monkeypatch, tmp_path, enable_local_replay=False)["summary"]
    assert opted_out["local_replay_enabled"] is False
    assert opted_out["degraded"] == ["jank_frame_monitor", "perfetto"]

    _is_ready_script(monkeypatch, [False])
    degraded = _run_local(monkeypatch, tmp_path)["summary"]
    assert degraded["local_replay_enabled"] is False
    assert degraded["degraded"] == ["jank_frame_monitor", "local_replay", "perfetto"]
    assert all(isinstance(name, str) for name in degraded["degraded"])


# --------------------------- 回放产物上传 --------------------------- #

def test_publish_local_replays_uploads_ready_and_flags_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    replay_dir = fastbot_service.local_report_dir(9) / "replays"
    replay_dir.mkdir(parents=True)
    (replay_dir / "crash_1.mp4").write_bytes(b"mp4-bytes")

    uploads: List[Any] = []

    def _upload(self, task_id, file_path, rel_path):
        uploads.append((task_id, file_path, rel_path))
        return {"path": f"reports/fastbot/{task_id}/{rel_path}"}

    monkeypatch.setattr(cloud_bridge.CloudBridge, "upload_client_fastbot_artifact", _upload)
    report = {
        "crash_events": [
            {"replay": {"status": "READY", "filename": "crash_1.mp4", "path": "../../x"}},
            {"replay": {"status": "READY", "filename": "crash_2.mp4"}},
            {"replay": {"status": "UNAVAILABLE", "error": "no buffered video data"}},
        ]
    }
    fastbot_service.publish_local_replays(
        cloud_bridge.CloudBridge(upstream="http://127.0.0.1:9"), 9, report
    )

    assert uploads == [(9, str(replay_dir / "crash_1.mp4"), "replays/crash_1.mp4")]
    ready = report["crash_events"][0]["replay"]
    assert ready["status"] == "READY"
    assert ready["path"] == "reports/fastbot/9/replays/crash_1.mp4"
    missing = report["crash_events"][1]["replay"]
    assert missing["status"] == "FAILED" and missing["filename"] == ""
    assert "未生成" in missing["error"]
    # 未就绪的事件不动
    assert report["crash_events"][2]["replay"]["status"] == "UNAVAILABLE"


def test_publish_local_replays_marks_upload_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    replay_dir = fastbot_service.local_report_dir(9) / "replays"
    replay_dir.mkdir(parents=True)
    (replay_dir / "crash_1.mp4").write_bytes(b"mp4")
    # 文件名带路径分隔符时只取 basename，防止越界写入/读取
    assert fastbot_service.local_replay_path(9, "../../etc/passwd") == (
        replay_dir / "passwd"
    )

    def _fail(self, task_id, file_path, rel_path):
        raise RuntimeError("隧道中断")

    monkeypatch.setattr(cloud_bridge.CloudBridge, "upload_client_fastbot_artifact", _fail)
    report = {"crash_events": [{"replay": {"status": "READY", "filename": "crash_1.mp4"}}]}
    fastbot_service.publish_local_replays(
        cloud_bridge.CloudBridge(upstream="http://127.0.0.1:9"), 9, report
    )
    replay = report["crash_events"][0]["replay"]
    assert replay["status"] == "FAILED"
    assert "上传云端失败" in replay["error"]
    assert replay["filename"] == ""


def test_prune_local_reports_removes_expired_only(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    fresh = fastbot_service.local_report_dir(11)
    stale = fastbot_service.local_report_dir(10)
    (fresh / "replays").mkdir(parents=True)
    (stale / "replays").mkdir(parents=True)
    expired = time.time() - 8 * 86400
    os.utime(stale, (expired, expired))

    fastbot_service.prune_local_reports()

    assert fresh.is_dir() and not stale.exists()


# --------------------------- 收尾上报 --------------------------- #

def test_run_and_report_finishes_with_platform_payload(fake_collection, monkeypatch):
    finished: List[Dict[str, Any]] = []
    done = threading.Event()

    def _finish(self, task_id, payload):
        finished.append((task_id, payload))
        done.set()
        return {"task_id": task_id, "report_id": 1, "status": payload["status"]}

    monkeypatch.setattr(cloud_bridge.CloudBridge, "finish_client_fastbot", _finish)
    fastbot_service.run_and_report(
        task_id=9,
        auth_headers={},
        device_serial="USB1",
        package_name="com.x",
        duration=1,
        throttle=500,
        ignore_crashes=True,
        capture_log=True,
        enable_performance_monitor=True,
    )
    assert done.wait(timeout=10), "finish 收尾未在限时内完成"
    task_id, payload = finished[0]
    assert task_id == 9
    assert payload["status"] == "COMPLETED"
    assert payload["total_crashes"] == 1
    assert payload["finished_at"]
    assert set(payload["report"]) == {
        "performance_data", "jank_data", "jank_events",
        "trace_artifacts", "crash_events", "summary",
    }


def test_run_and_report_uploads_replays_before_finish(monkeypatch, tmp_path):
    """回放视频先落云端再收尾——报告一出现就能直接播放。"""
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    replay_dir = fastbot_service.local_report_dir(9) / "replays"
    replay_dir.mkdir(parents=True)
    (replay_dir / "crash_1.mp4").write_bytes(b"mp4")

    async def _local(**kwargs):
        return {
            "performance_data": [], "jank_data": [], "jank_events": [], "trace_artifacts": [],
            "crash_events": [
                {"type": "CRASH", "time": "t", "replay": {
                    "status": "READY", "filename": "crash_1.mp4", "duration_sec": 35,
                }},
            ],
            "summary": {"total_crashes": 1, "total_anrs": 0, "local_replay_enabled": True},
        }

    order: List[Any] = []
    done = threading.Event()

    def _upload(self, task_id, file_path, rel_path):
        order.append(("upload", task_id, rel_path, Path(file_path).read_bytes()))
        return {}

    def _finish(self, task_id, payload):
        order.append(("finish", task_id, payload))
        done.set()
        return {}

    monkeypatch.setattr(fastbot_service, "run_fastbot_local", _local)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "upload_client_fastbot_artifact", _upload)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "finish_client_fastbot", _finish)
    fastbot_service.run_and_report(
        task_id=9, auth_headers={}, device_serial="USB1", package_name="com.x",
        duration=1, throttle=500, ignore_crashes=True, capture_log=True,
        enable_performance_monitor=True,
    )
    assert done.wait(timeout=10), "收尾未在限时内完成"
    assert [item[0] for item in order] == ["upload", "finish"]
    assert order[0][1] == 9 and order[0][2] == "replays/crash_1.mp4"
    assert order[0][3] == b"mp4"
    replay = order[1][2]["report"]["crash_events"][0]["replay"]
    assert replay["path"] == "reports/fastbot/9/replays/crash_1.mp4"


def test_run_and_report_skips_upload_without_crash_events(monkeypatch):
    async def _local(**kwargs):
        return {
            "performance_data": [], "jank_data": [], "jank_events": [], "trace_artifacts": [],
            "crash_events": [],
            "summary": {"total_crashes": 0, "total_anrs": 0, "local_replay_enabled": True},
        }

    def _unexpected(*args, **kwargs):
        raise AssertionError("无崩溃事件时不应发起产物上传")

    done = threading.Event()
    monkeypatch.setattr(fastbot_service, "run_fastbot_local", _local)
    monkeypatch.setattr(
        cloud_bridge.CloudBridge, "upload_client_fastbot_artifact", _unexpected
    )
    monkeypatch.setattr(
        cloud_bridge.CloudBridge, "finish_client_fastbot",
        lambda self, task_id, payload: done.set() or {},
    )
    fastbot_service.run_and_report(
        task_id=9, auth_headers={}, device_serial="USB1", package_name="com.x",
        duration=1, throttle=500, ignore_crashes=True, capture_log=True,
        enable_performance_monitor=True,
    )
    assert done.wait(timeout=10)


def test_run_and_report_failed_run_still_finishes(monkeypatch):
    """采集异常也必须 finish(FAILED)——云端任务不得停留 RUNNING。"""
    finished: List[Dict[str, Any]] = []
    done = threading.Event()

    async def _boom(**kwargs):
        raise RuntimeError("device lost")

    def _finish(self, task_id, payload):
        finished.append(payload)
        done.set()
        return {}

    monkeypatch.setattr(fastbot_service, "run_fastbot_local", _boom)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "finish_client_fastbot", _finish)
    fastbot_service.run_and_report(
        task_id=9, auth_headers={}, device_serial="USB1", package_name="com.x",
        duration=1, throttle=500, ignore_crashes=False, capture_log=True,
        enable_performance_monitor=False,
    )
    assert done.wait(timeout=10)
    assert finished[0]["status"] == "FAILED"
    assert "device lost" in (finished[0]["report"]["summary"].get("error") or "")


def test_start_fastbot_run_registers_cloud_task_and_spawns(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    captured = {}

    def _create(self, payload):
        captured["create"] = payload
        return {"id": 9, "status": "RUNNING"}

    def _spawn(**kwargs):
        captured["spawn"] = kwargs

    monkeypatch.setattr(cloud_bridge.CloudBridge, "create_client_fastbot", _create)
    monkeypatch.setattr(fastbot_service, "run_and_report", _spawn)
    bridge = cloud_bridge.CloudBridge(upstream="http://127.0.0.1:9")
    created = fastbot_service.start_fastbot_run(
        bridge,
        {
            "package_name": "com.x",
            "device_serial": "USB1",
            "duration": 60,
            "throttle": 300,
            "ignore_crashes": True,
            "capture_log": False,
            "enable_performance_monitor": True,
            "enable_custom_event_weights": True,
            "pct_touch": 55,
        },
    )
    assert created["id"] == 9
    create_payload = captured["create"]
    assert create_payload["kind"] == "fastbot"
    assert create_payload["package_name"] == "com.x"
    assert create_payload["device_serial"] == "USB1"
    assert create_payload["duration"] == 60 and create_payload["throttle"] == 300
    assert create_payload["ignore_crashes"] is True and create_payload["capture_log"] is False
    assert create_payload["started_at"]
    spawn = captured["spawn"]
    assert spawn["task_id"] == 9
    assert spawn["enable_performance_monitor"] is True
    # 异常回放默认开启（对齐 FastbotDashboard 表单默认值），显式关闭时如实透传
    assert spawn["enable_local_replay"] is True
    assert spawn["event_weights"]["enable_custom_event_weights"] is True
    assert spawn["event_weights"]["pct_touch"] == 55

    # 执行线程被 mock 掉不会走到收尾释放：模拟上一轮结束（同一设备不能并发跑测）
    from engine.core import device_busy

    device_busy.release("USB1")
    fastbot_service.start_fastbot_run(
        bridge,
        {"package_name": "com.x", "device_serial": "USB1", "enable_local_replay": False},
    )
    assert captured["spawn"]["enable_local_replay"] is False

    with pytest.raises(ValueError):
        fastbot_service.start_fastbot_run(bridge, {"package_name": "", "device_serial": "USB1"})
    with pytest.raises(ValueError):
        fastbot_service.start_fastbot_run(
            bridge, {"package_name": "com.x", "device_serial": "ios:u1"}
        )


# --------------------------- T5：命令注入面收敛 --------------------------- #

def test_validate_serial_and_package_whitelist():
    from engine.core.fastbot.adb import validate_device_serial, validate_package_name

    assert validate_device_serial("USB1") == "USB1"
    assert validate_device_serial("emulator-5554") == "emulator-5554"
    assert validate_device_serial("127.0.0.1:5555") == "127.0.0.1:5555"
    for bad in ("", "a b", "a;reboot", "a$(id)", "a`id`", "a|b", "a&&b", None):
        with pytest.raises(ValueError):
            validate_device_serial(bad)

    assert validate_package_name("com.x") == "com.x"
    assert validate_package_name("com.example.app_2") == "com.example.app_2"
    for bad in ("", "com.x;reboot", "com.x && rm -rf /", "com.x$(id)", "com x", None):
        with pytest.raises(ValueError):
            validate_package_name(bad)


def test_start_fastbot_run_rejects_injection_payload(monkeypatch):
    """T5：注入形态的 serial/包名在入口即被拒（不创建云端任务、不拉起进程）。"""
    created = []
    monkeypatch.setattr(
        cloud_bridge.CloudBridge,
        "create_client_fastbot",
        lambda self, payload: created.append(payload) or {"id": 1},
    )
    bridge = cloud_bridge.CloudBridge(upstream="http://127.0.0.1:9")
    with pytest.raises(ValueError):
        fastbot_service.start_fastbot_run(
            bridge, {"package_name": "com.x; rm -rf /", "device_serial": "USB1"}
        )
    with pytest.raises(ValueError):
        fastbot_service.start_fastbot_run(
            bridge, {"package_name": "com.x", "device_serial": "USB1; reboot"}
        )
    assert created == []


def test_adb_shell_exec_args_no_shell(monkeypatch):
    """T5：vendor adb 走 create_subprocess_exec 参数数组（serial 独立 argv）。"""
    import asyncio

    from engine.core.fastbot import adb as fastbot_adb

    captured = {}

    class _P:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def _exec(*args, **kwargs):
        captured["argv"] = list(args)
        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    # adb 可执行路径经 engine.adb_scan 唯一入口解析
    monkeypatch.setattr("engine.adb_scan.find_adb_path", lambda explicit=None: "/resolved/adb")
    asyncio.run(fastbot_adb._adb_shell("USB1", "getprop ro.build.version.sdk"))
    assert captured["argv"][:4] == ["/resolved/adb", "-s", "USB1", "shell"]

    with pytest.raises(ValueError):
        asyncio.run(fastbot_adb._adb_shell("USB1; reboot", "echo hi"))


# --------------------------- HTTP 拦截 --------------------------- #

@pytest.fixture()
def mock_cloud():
    from test_local_api import _MockCloud

    server = _MockCloud()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch, mock_cloud):
    from fastapi.testclient import TestClient

    from engine.app import create_app

    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    config_store.reset_for_tests()
    presence.reset_for_tests()
    config_store.set_upstream(mock_cloud.base_url)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    presence.reset_for_tests()
    config_store.reset_for_tests()


@pytest.fixture()
def local_device(monkeypatch):
    from engine.core.device_stream.manager import device_manager

    monkeypatch.setattr(
        device_manager, "get_device", lambda serial: {"serial": serial, "ready": True} if serial == "USB1" else None
    )
    return device_manager


def test_fastbot_run_intercept_registers_and_returns_task_read(client, mock_cloud, local_device, monkeypatch):
    spawned = {}
    monkeypatch.setattr(
        fastbot_service, "run_and_report", lambda **kwargs: spawned.update(kwargs)
    )
    resp = client.post(
        "/api/fastbot/run",
        json={
            "package_name": "com.x",
            "device_serial": "USB1",
            "duration": 60,
            "throttle": 500,
            "enable_performance_monitor": True,
            "enable_jank_frame_monitor": True,
            "enable_local_replay": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # 形状对齐 FastbotTaskRead（前端 runFastbot 调用面零改动）
    assert set(data) >= {
        "id", "package_name", "duration", "throttle", "device_serial", "status",
        "total_crashes", "total_anrs", "created_at",
    }
    assert data["id"] == 9 and data["status"] == "RUNNING"
    # 本地执行已启动
    assert spawned["task_id"] == 9 and spawned["device_serial"] == "USB1"
    assert spawned["enable_local_replay"] is True
    # 云端登记走 client-fastbot（不是反代 /api/fastbot/run）
    posted = [r["path"] for r in mock_cloud.requests if r["method"] == "POST"]
    assert "/api/client-fastbot/" in posted
    assert not [p for p in posted if p.endswith("/api/fastbot/run")]


def test_fastbot_run_requires_local_device(client, mock_cloud, local_device):
    resp = client.post(
        "/api/fastbot/run",
        json={"package_name": "com.x", "device_serial": "NOPE"},
    )
    assert resp.status_code == 400
    assert "设备不在线或未就绪" in resp.json()["detail"]


# --------------------------- 引擎日志落盘 --------------------------- #

def test_engine_file_logging_writes_rotating_log(tmp_path, monkeypatch):
    """引擎日志落盘 userData/logs/engine.log：stderr 在 Electron 侧被丢掉，事后要能查现场。"""
    import logging as logging_mod

    from engine import __main__ as engine_main

    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    handler = engine_main._setup_file_logging()
    assert handler is not None
    try:
        logging_mod.getLogger("engine.test").warning("启动异常回放录制失败，本次报告无崩溃回放: boom")
        for item in list(logging_mod.getLogger().handlers):
            item.flush()
        log_file = tmp_path / "userdata" / "logs" / "engine.log"
        assert log_file.is_file(), "日志目录不存在时应自建"
        assert "启动异常回放录制失败" in log_file.read_text(encoding="utf-8")
    finally:
        logging_mod.getLogger().removeHandler(handler)
        handler.close()


def test_engine_file_logging_degrades_to_stderr_only(tmp_path, monkeypatch, capsys):
    """日志目录建不出来只告警，绝不挡住引擎启动。"""
    from engine import __main__ as engine_main

    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AUTODROID_USER_DATA", str(blocked))
    handler = engine_main._setup_file_logging()
    assert handler is None
    assert "引擎日志落盘不可用" in capsys.readouterr().err


# --------------------------- 契约漂移锁 --------------------------- #

_MIRRORS = {
    "ClientFastbotTaskCreate": {
        "run_id", "kind", "package_name", "device_serial", "duration", "throttle",
        "ignore_crashes", "capture_log", "started_at", "executor_name",
    },
    "ClientFastbotReportPayload": {
        "performance_data", "jank_data", "jank_events",
        "trace_artifacts", "crash_events", "summary",
    },
    "ClientFastbotFinish": {
        "status", "total_crashes", "total_anrs", "finished_at", "report",
    },
}


def _ast_model_fields(class_name: str) -> Set[str]:
    tree = ast.parse(PLATFORM_CLIENT_FASTBOT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: Set[str] = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            fields.add(target.id)
            return fields
    raise AssertionError(f"平台源码未找到模型: {class_name}")


@pytest.mark.parametrize("model_name", sorted(_MIRRORS))
def test_client_fastbot_payload_fields_match_platform(model_name):
    if not PLATFORM_CLIENT_FASTBOT.is_file():
        pytest.skip("平台仓库源码不在本机（跳过跨仓库漂移锁）")
    assert _MIRRORS[model_name] == _ast_model_fields(model_name)
