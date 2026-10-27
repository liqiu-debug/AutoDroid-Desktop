"""Fastbot 本地轻量档（P3-2）测试。

覆盖：
- vendor monkey 资源目录/命令拼接（assets/fastbot 随客户端分发）
- run_fastbot_local 轻量档报告六列形状 + 汇总（crash 计数 / 降级标注）
- run_and_report 收尾上报（client-fastbot finish 载荷形状）
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
        crash_events.append(
            {"type": "CRASH", "time": "t", "package": package, "message": "boom"}
        )
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
    # T5：monkey 走 create_subprocess_exec 参数数组（serial 独立 argv，不经宿主 shell）
    argv = fake_collection["monkey_argv"]
    assert argv[:4] == ["adb", "-s", "USB1", "shell"]
    assert "com.android.commands.monkey.Monkey" in argv[4]


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


def test_start_fastbot_run_registers_cloud_task_and_spawns(monkeypatch):
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
    assert spawn["event_weights"]["enable_custom_event_weights"] is True
    assert spawn["event_weights"]["pct_touch"] == 55

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
    asyncio.run(fastbot_adb._adb_shell("USB1", "getprop ro.build.version.sdk"))
    assert captured["argv"][:4] == ["adb", "-s", "USB1", "shell"]

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
