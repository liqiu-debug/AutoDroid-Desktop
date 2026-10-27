"""本地 run registry 与执行旁路端点（R4）测试。

- registry 匹配/取消语义对齐 backend/run_control.py
- GET  /api/runs/active、POST /api/runs/cancel 本地拦截（不再反代云端）
- cancel 置 abort_event 并等 runner 退出
- GET /api/cases/{id}/precheck 本地预检（不触达云端 precheck）
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app
from engine.core import run_registry, runner_service
from engine.core.run_registry import LocalRunRecord, LocalRunRegistry


@pytest.fixture(autouse=True)
def _clean_registry():
    run_registry.registry.reset_for_tests()
    yield
    run_registry.registry.reset_for_tests()


# --------------------------- registry 单元 --------------------------- #

def test_registry_active_filters_by_kind_and_target():
    registry = LocalRunRegistry()
    registry.register(LocalRunRecord(kind="case", target_id=1, run_id="r1", device_serial="A"))
    registry.register(LocalRunRecord(kind="scenario", target_id=1, run_id="r2", device_serial="B"))
    assert {r.run_id for r in registry.active(kind="case", target_id=1)} == {"r1"}
    assert {r.run_id for r in registry.active(kind="scenario", target_id=1)} == {"r2"}
    assert registry.active(kind="case", target_id=2) == []


def test_registry_cancel_sets_abort_and_hides_from_active():
    registry = LocalRunRegistry()
    record = registry.register(
        LocalRunRecord(kind="case", target_id=5, run_id="r1", batch_id="b1", device_serial="USB1")
    )
    cancelled = registry.cancel(kind="case", target_id=5, batch_id="b1", run_ids=["r1"], device_serials=["USB1"])
    assert [r.run_id for r in cancelled] == ["r1"]
    assert record.abort_event.is_set()
    assert record.status == "ABORTED"
    assert registry.active(kind="case", target_id=5) == []


def test_registry_cancel_match_semantics():
    """对齐 run_control.RunRegistry._match：显式 run_ids/serials 不命中则不取消。"""
    registry = LocalRunRegistry()
    record = registry.register(LocalRunRecord(kind="case", target_id=5, run_id="r1", device_serial="USB1"))
    assert registry.cancel(kind="case", target_id=5, run_ids=["other"]) == []
    assert registry.cancel(kind="case", target_id=5, device_serials=["NOPE"]) == []
    assert not record.abort_event.is_set()
    assert registry.cancel(kind="case", target_id=5, device_serials=["USB1"]) == [record]


def test_registry_wait_for_exit_times_out_then_completes():
    registry = LocalRunRegistry()
    record = registry.register(LocalRunRecord(kind="case", target_id=5, run_id="r1"))
    assert registry.wait_for_exit([record], timeout=0.05) is False
    registry.complete("r1")
    assert registry.wait_for_exit([record], timeout=0.05) is True


def test_registry_update_execution_id_backfills_record():
    """S1：execution_id 生命周期——create_cloud_run/preset_run_key 后回填。"""
    registry = LocalRunRegistry()
    record = registry.register(
        LocalRunRecord(kind="scenario", target_id=7, run_id="r1", batch_id="b1", device_serial="USB1")
    )
    assert record.execution_id is None
    registry.update_execution_id("r1", 77)
    assert record.execution_id == 77
    # 非法值/未知 run_id 忽略不抛
    registry.update_execution_id("r1", "not-int")
    registry.update_execution_id("missing", 88)
    assert record.execution_id == 77
    assert registry.active(kind="scenario", target_id=7)[0].to_dict()["execution_id"] == 77


def test_registry_cancel_matches_scenario_editor_payload():
    """S1 回归：前端 ScenarioEditor.terminateScenarioRun 真实载荷 AND 匹配。

    {kind:'scenario', target_id, batch_id, execution_ids, device_serials}
    （batch_id+execution_ids+device_serials 三键同发，缺 execution_id 回填时曾 matched=0）。
    """
    registry = LocalRunRegistry()
    hit = registry.register(
        LocalRunRecord(kind="scenario", target_id=7, run_id="r1", batch_id="b1", device_serial="USB1", execution_id=77)
    )
    other = registry.register(
        LocalRunRecord(kind="scenario", target_id=7, run_id="r2", batch_id="b1", device_serial="USB2", execution_id=88)
    )
    cancelled = registry.cancel(
        kind="scenario",
        target_id=7,
        batch_id="b1",
        execution_ids=[77],
        device_serials=["USB1"],
    )
    assert [r.run_id for r in cancelled] == ["r1"]
    assert hit.abort_event.is_set()
    assert not other.abort_event.is_set()


def test_registry_cancel_frontend_payload_matches_unbackfilled_execution_id():
    """execution_id 尚未回填/预创建失败（None）时，execution_ids 过滤器不误杀——其余键仍需命中。"""
    registry = LocalRunRegistry()
    record = registry.register(
        LocalRunRecord(kind="scenario", target_id=7, run_id="r1", batch_id="b1", device_serial="USB1")
    )
    cancelled = registry.cancel(
        kind="scenario",
        target_id=7,
        batch_id="b1",
        execution_ids=[77],
        device_serials=["USB1"],
    )
    assert [r.run_id for r in cancelled] == ["r1"]
    assert record.abort_event.is_set()


# --------------------------- 端点（TestClient） --------------------------- #

class _FakeDevice:
    info = {"displayWidth": 10, "displayHeight": 20}

    def dump_hierarchy(self):
        return "<hierarchy/>"

    def screenshot(self):
        from PIL import Image

        return Image.new("RGB", (4, 4))


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


def test_runs_active_is_local_and_returns_registry_items(client, mock_cloud):
    run_registry.registry.register(
        LocalRunRecord(kind="case", target_id=5, run_id="r-live", batch_id="b1", device_serial="USB1")
    )
    resp = client.get("/api/runs/active", params={"kind": "case", "target_id": 5})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["run_id"] == "r-live"
    assert item["kind"] == "case"
    assert item["target_id"] == 5
    assert item["batch_id"] == "b1"
    assert item["device_serial"] == "USB1"
    assert item["status"] == "RUNNING"
    assert item["started_at"] and item["cancel_requested_at"] is None
    assert not [r for r in mock_cloud.requests if "/runs/" in r["path"]], "active 轮询不应反代云端"


def test_runs_cancel_is_local_sets_abort(client, mock_cloud):
    record = run_registry.registry.register(
        LocalRunRecord(kind="case", target_id=5, run_id="r-live", batch_id="b1", device_serial="USB1")
    )
    resp = client.post(
        "/api/runs/cancel",
        json={"kind": "case", "target_id": 5, "batch_id": "b1", "run_ids": [], "device_serials": ["USB1"]},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ABORTED"
    assert payload["cancelled_count"] == 1
    assert payload["db_updates"] == 0
    assert payload["runs"][0]["run_id"] == "r-live"
    assert record.abort_event.is_set()
    assert not [r for r in mock_cloud.requests if "/runs/" in r["path"]], "cancel 不应反代云端"


def test_cases_precheck_is_local_with_platform_shape(client, mock_cloud):
    resp = client.get(
        "/api/cases/5/precheck", params={"device_serial": "USB1", "env_id": 3}
    )
    assert resp.status_code == 200
    payload = resp.json()
    # 形状对齐 cross_platform_execution.precheck_case_execution
    assert payload["case_id"] == 5
    assert payload["device_serial"] == "USB1"
    assert payload["platform"] == "android"
    assert payload["ok"] is True
    assert payload["has_runnable_steps"] is True
    assert set(payload["summary"]) == {"total", "pass", "skip", "fail", "global_fail"}
    assert payload["steps"] and payload["steps"][0]["status"] == "PASS"
    assert "global_checks" in payload and "exported_variables" in payload
    assert not [r for r in mock_cloud.requests if "precheck" in r["path"]], "precheck 应本地拦截"


def test_background_run_registers_and_cancel_aborts_it(client, monkeypatch):
    """run_case_local 生命周期登记 + cancel 中止（等 runner 退出）。"""
    abort_seen = {"value": False}
    started = threading.Event()

    class SlowRunner:
        def __init__(self, platform=None, device_id=None, abort_event=None, **kwargs):
            self.abort_event = abort_event
            self.runtime_variables = {}
            self.driver = _FakeDevice()

        def run_step(self, step):
            started.set()
            # 阻塞直到 cancel 置 abort_event（或兜底超时）
            if self.abort_event is not None:
                self.abort_event.wait(timeout=5)
                abort_seen["value"] = self.abort_event.is_set()
            return {"status": "PASS", "duration": 0.01, "attempts": 1, "step": step}

        def disconnect(self):
            pass

    monkeypatch.setattr(runner_service, "CrossPlatformRunner", SlowRunner)
    monkeypatch.setattr(
        runner_service.recording_service, "localize_step_images", lambda step, ensure: step
    )

    resp = client.post("/api/cases/5/run?device_serial=USB1")
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    batch_id = resp.json()["batch_id"]

    # 等后台线程登记
    for _ in range(100):
        active = client.get("/api/runs/active", params={"kind": "case", "target_id": 5}).json()["items"]
        if active:
            break
        time.sleep(0.05)
    assert active, "后台执行应登记到本地 registry"
    assert active[0]["run_id"] == run_id
    # S1：create_cloud_run 后 execution_id 回填（mock 云端返回 id=42）
    for _ in range(100):
        active = client.get("/api/runs/active", params={"kind": "case", "target_id": 5}).json()["items"]
        if active and active[0]["execution_id"] is not None:
            break
        time.sleep(0.05)
    assert active[0]["execution_id"] == 42, "active.items[].execution_id 不得为 null"

    # 等 runner 真正进入步骤（run_step 阻塞等 abort），再取消
    assert started.wait(timeout=5), "runner 未进入步骤执行"

    # CaseEditor.terminateActiveRun 真实载荷形状（batch_id+run_ids+device_serials）
    cancel = client.post(
        "/api/runs/cancel",
        json={
            "kind": "case",
            "target_id": 5,
            "batch_id": batch_id,
            "run_ids": [run_id],
            "device_serials": ["USB1"],
        },
    )
    assert cancel.status_code == 200
    assert cancel.json()["cancelled_count"] == 1

    # runner 已退出（cancel 内等待 done_event）
    for _ in range(100):
        active = client.get("/api/runs/active", params={"kind": "case", "target_id": 5}).json()["items"]
        if not active:
            break
        time.sleep(0.05)
    assert not active, "取消后 run 应从 active 消失"
    assert abort_seen["value"] is True
