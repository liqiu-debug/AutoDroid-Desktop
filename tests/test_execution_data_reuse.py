"""聚合输入只读取一次，预检与运行保持独立的变量/步骤状态。"""
from __future__ import annotations

import asyncio
import threading
from copy import deepcopy

import httpx
import pytest

from engine import config_store
from engine.core import cloud_bridge, fastbot_service, run_registry, runner_service, scenario_service, task_scheduler
from test_runner_service import FakeBridge, FakeRunner
from test_scenario_service import FakeScenarioBridge


@pytest.fixture()
def fake_runner(monkeypatch):
    FakeRunner.results = {}
    FakeRunner.instances = []
    monkeypatch.setattr(runner_service, "CrossPlatformRunner", FakeRunner)
    monkeypatch.setattr(runner_service.recording_service, "localize_step_images", lambda step, ensure, platform: step)


def _http_bridge(monkeypatch, snapshot, path):
    requests = []
    original_client = httpx.Client

    def respond(request):
        requests.append(request)
        assert request.url.path == path
        return httpx.Response(200, json=snapshot)

    monkeypatch.setattr(cloud_bridge.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs
    ))
    return cloud_bridge.CloudBridge(upstream="https://first.example", auth_headers={"Authorization": "Bearer first"}), requests


@pytest.mark.parametrize("case_count", [1, 12, 100])
def test_scenario_one_data_request_for_precheck_and_two_device_runs(case_count, fake_runner, monkeypatch):
    nodes = [
        {"id": i + 1, "case_id": 22, "order": i + 1, "alias": f"repeat-{i + 1}"}
        for i in range(case_count)
    ]
    source = FakeScenarioBridge(scenario_steps=nodes)
    snapshot = source.fetch_scenario_execution_data(7, 3)
    original = deepcopy(snapshot)
    bridge, requests = _http_bridge(monkeypatch, snapshot, "/api/scenarios/7/execution-data")
    monkeypatch.setattr(bridge, "create_run", source.create_run)
    monkeypatch.setattr(bridge, "report_steps", source.report_steps)
    monkeypatch.setattr(bridge, "finish_run", source.finish_run)

    async def emit(message):
        pass

    async def run():
        data = await asyncio.to_thread(bridge.fetch_scenario_execution_data, 7, 3)
        for serial in ("USB1", "USB2"):
            check = scenario_service.precheck_scenario_for_device(bridge, 7, serial, 3, execution_data=data)
            assert check["ok"]
            assert check["summary"]["total_cases"] == case_count
            result = await scenario_service.run_scenario_local(
                scenario_id=7, env_id=3, device_serial=serial, emit=emit, bridge=bridge, execution_data=data,
            )
            assert result["success"]
        assert data == original

    with bridge:
        asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].url.params["env_id"] == "3"
    assert len(source.step_payloads) == case_count * 2
    assert [row[1]["alias"] for row in source.step_payloads[:case_count]] == [node["alias"] for node in nodes]


def test_case_one_data_request_preserves_snapshot_for_both_devices(fake_runner, monkeypatch):
    source = FakeBridge()
    snapshot = source.fetch_case_execution_data(5, 3)
    original = deepcopy(snapshot)
    bridge, requests = _http_bridge(monkeypatch, snapshot, "/api/cases/5/execution-data")
    for name in ("create_run", "report_steps", "finish_run"):
        monkeypatch.setattr(bridge, name, getattr(source, name))

    async def emit(message):
        pass

    async def run():
        data = bridge.fetch_case_execution_data(5, 3)
        for serial in ("USB1", "USB2"):
            assert runner_service.precheck_for_device(bridge, 5, serial, 3, execution_data=data)["ok"]
            assert (await runner_service.run_case_local(
                case_id=5, env_id=3, device_serial=serial, emit=emit, bridge=bridge, execution_data=data
            ))["success"]
        assert data == original

    with bridge:
        asyncio.run(run())
    assert len(requests) == 1


def test_scenario_runtime_export_is_rendered_per_device_without_mutating_snapshot(fake_runner, monkeypatch):
    source = FakeScenarioBridge()
    snapshot = source.fetch_scenario_execution_data(7, 3)
    snapshot["scenario_steps"] = [
        {"id": 1, "case_id": 11, "order": 1, "alias": "produce"},
        {"id": 2, "case_id": 22, "order": 2, "alias": "consume"},
    ]
    snapshot["cases"]["11"]["standard_steps"] = [
        {"order": 1, "action": "extract_by_ocr", "args": {"output_var": "TOKEN", "region": [0, 0, 100, 100]},
         "execute_on": ["android"]},
    ]
    snapshot["cases"]["22"]["standard_steps"] = [
        {"order": 1, "action": "input", "args": {"text": "{{TOKEN}}"},
         "platform_overrides": {"android": {"selector": "#token", "by": "id"}}, "execute_on": ["android"]},
    ]
    original = deepcopy(snapshot)
    captured = []

    class ExportRunner(FakeRunner):
        def __init__(self, platform=None, device_id=None, **kwargs):
            super().__init__(platform=platform, device_id=device_id, **kwargs)
            self.serial = device_id

        def run_step(self, step):
            captured.append((self.serial, deepcopy(step)))
            if step["action"] == "extract_by_ocr":
                self.runtime_variables["TOKEN"] = f"token-{self.serial}"
            return super().run_step(step)

    monkeypatch.setattr(runner_service, "CrossPlatformRunner", ExportRunner)

    async def emit(message):
        pass

    async def run():
        for serial in ("USB1", "USB2"):
            assert scenario_service.precheck_scenario_for_device(source, 7, serial, 3, execution_data=snapshot)["ok"]
            assert (await scenario_service.run_scenario_local(
                scenario_id=7, env_id=3, device_serial=serial, emit=emit, bridge=source, execution_data=snapshot,
            ))["success"]

    asyncio.run(run())
    inputs = [(serial, step["args"]["text"]) for serial, step in captured if step["action"] == "input"]
    assert inputs == [("USB1", "token-USB1"), ("USB2", "token-USB2")]
    assert snapshot == original


def test_legacy_only_case_snapshot_still_prechecks_and_runs(fake_runner):
    source = FakeBridge()
    snapshot = source.fetch_case_execution_data(5, 3)
    snapshot["standard_steps"] = []
    snapshot["case"]["steps"] = [{"action": "home"}]
    assert runner_service.precheck_for_device(source, 5, "USB1", 3, execution_data=snapshot)["ok"]

    async def emit(message):
        pass

    result = asyncio.run(runner_service.run_case_local(
        case_id=5, env_id=3, device_serial="USB1", emit=emit, bridge=source, execution_data=snapshot,
    ))
    assert result["success"]
    assert source.step_payloads[0][1]["steps"][0]["action"] == "home"


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.closed = False

    async def accept(self):
        pass

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("kind", ["case", "scenario"])
@pytest.mark.parametrize("broken", [False, True])
def test_ws_precheck_blocks_and_closes_owned_bridge(kind, broken, monkeypatch):
    ws = FakeWebSocket()
    closed = []
    original_close = cloud_bridge.CloudBridge.close

    def close(bridge):
        closed.append(bridge)
        original_close(bridge)

    monkeypatch.setattr(cloud_bridge.CloudBridge, "close", close)
    if kind == "case":
        snapshot = FakeBridge().fetch_case_execution_data(5)
        snapshot["standard_steps"] = [{"order": 1, "action": "home", "args": {}, "execute_on": ["ios"]}]
        method = "fetch_case_execution_data"
        service = runner_service
        run_name = "run_case_local"
        ws_run = runner_service.run_case_via_websocket
        target_id = 5
    else:
        snapshot = FakeScenarioBridge().fetch_scenario_execution_data(7)
        snapshot["cases"] = {}
        method = "fetch_scenario_execution_data"
        service = scenario_service
        run_name = "run_scenario_local"
        ws_run = scenario_service.run_scenario_via_websocket
        target_id = 7

    def fetch(bridge, target_id, env_id=None):
        if broken:
            raise RuntimeError("cloud 502")
        return snapshot

    async def forbidden(**kwargs):
        pytest.fail("预检失败不应进入设备执行")

    monkeypatch.setattr(cloud_bridge.CloudBridge, method, fetch)
    monkeypatch.setattr(service, run_name, forbidden)
    asyncio.run(ws_run(ws, target_id, None, "USB1", {}, upstream="https://first.example"))
    assert ws.closed
    assert len(closed) == 1 and closed[0]._closed
    assert any("运行前预检" in message.get("message", "") or "cloud 502" in message.get("message", "") for message in ws.messages)


@pytest.mark.parametrize("kind", ["case", "scenario"])
def test_ws_success_passes_same_raw_snapshot_once_and_closes(kind, monkeypatch):
    ws = FakeWebSocket()
    calls = []
    seen = []
    if kind == "case":
        snapshot = FakeBridge().fetch_case_execution_data(5)
        method = "fetch_case_execution_data"
        service, run_name, ws_run, target_id = runner_service, "run_case_local", runner_service.run_case_via_websocket, 5
    else:
        snapshot = FakeScenarioBridge().fetch_scenario_execution_data(7)
        method = "fetch_scenario_execution_data"
        service, run_name, ws_run, target_id = scenario_service, "run_scenario_local", scenario_service.run_scenario_via_websocket, 7

    def fetch(bridge, target_id, env_id=None):
        calls.append(bridge)
        return snapshot

    async def run(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(cloud_bridge.CloudBridge, method, fetch)
    monkeypatch.setattr(service, run_name, run)
    asyncio.run(ws_run(ws, target_id, 3, "USB1", {}))
    assert len(calls) == len(seen) == 1
    assert seen[0]["execution_data"] == snapshot
    assert calls[0]._closed and ws.closed


@pytest.mark.parametrize("kind", ["case", "scenario"])
def test_background_captures_upstream_headers_snapshot_and_closes_on_failure(kind, monkeypatch):
    import threading as threading_module

    original_thread = threading_module.Thread
    pending = []
    done = threading.Event()
    seen = []
    headers = {"Authorization": "Bearer first"}
    snapshot = {"nested": {"value": "first"}}
    monkeypatch.setattr(config_store, "get_upstream", lambda: "https://first.example")

    class DelayedThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    async def run(**kwargs):
        seen.append(kwargs)
        raise RuntimeError("fake runner failure")

    original_close = cloud_bridge.CloudBridge.close

    def close(bridge):
        original_close(bridge)
        done.set()

    service = runner_service if kind == "case" else scenario_service
    monkeypatch.setattr(service, "run_case_local" if kind == "case" else "run_scenario_local", run)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "close", close)
    monkeypatch.setattr(threading_module, "Thread", DelayedThread)
    start = service.start_background_case_run if kind == "case" else service.start_background_scenario_run
    start(**{
        f"{kind}_id": 5 if kind == "case" else 7,
        "env_id": 3, "device_serial": "USB1", "run_id": "fixed-run", "batch_id": "fixed-batch",
        "auth_headers": headers, "execution_data": snapshot,
    })
    record = run_registry.registry.get("fixed-run")
    assert record is not None and record.metadata["cloud_record_pending"]
    assert record in run_registry.registry.active(kind=kind)
    cancelled = run_registry.registry.cancel(run_ids=["fixed-run"])
    assert cancelled == [record]
    monkeypatch.setattr(threading_module, "Thread", original_thread)
    headers["Authorization"] = "Bearer later"
    snapshot["nested"]["value"] = "later"
    monkeypatch.setattr(config_store, "get_upstream", lambda: "https://later.example")
    worker = original_thread(target=pending[0])
    worker.start()
    worker.join(timeout=5)
    assert done.is_set()
    assert seen[0]["bridge"].upstream == "https://first.example"
    assert seen[0]["bridge"].auth_headers == {"Authorization": "Bearer first"}
    assert seen[0]["execution_data"] == {"nested": {"value": "first"}}
    assert seen[0]["bridge"]._closed
    assert seen[0]["abort_event"] is record.abort_event and record.abort_event.is_set()
    assert run_registry.registry.get("fixed-run") is None


@pytest.mark.parametrize("kind", ["case", "scenario"])
def test_background_thread_start_failure_removes_registration(kind, monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading, "Thread", BrokenThread)
    start = runner_service.start_background_case_run if kind == "case" else scenario_service.start_background_scenario_run
    with pytest.raises(RuntimeError, match="cannot start thread"):
        start(**{
            f"{kind}_id": 5 if kind == "case" else 7, "env_id": 3, "device_serial": "USB1",
            "run_id": "start-failed", "batch_id": "b", "auth_headers": {},
        })
    assert run_registry.registry.get("start-failed") is None


@pytest.mark.parametrize("kind", ["case", "scenario"])
def test_local_run_reuses_pre_registered_record_and_early_cancel(kind, fake_runner):
    from engine.core.run_registry import LocalRunRecord

    record = run_registry.registry.register(LocalRunRecord(
        kind=kind, target_id=5 if kind == "case" else 7, run_id="early-cancel", device_serial="USB1",
    ))
    record.abort_event.set()
    source = FakeBridge() if kind == "case" else FakeScenarioBridge()

    async def emit(message):
        if message.get("type") == "run_start":
            assert run_registry.registry.get("early-cancel") is record
            assert record.abort_event.is_set()

    run = runner_service.run_case_local if kind == "case" else scenario_service.run_scenario_local
    result = asyncio.run(run(**{
        f"{kind}_id": 5 if kind == "case" else 7, "env_id": 3, "device_serial": "USB1",
        "run_id": "early-cancel", "emit": emit, "bridge": source,
    }))
    assert result["status"] == "ABORTED"
    assert not FakeRunner.instances
    assert run_registry.registry.get("early-cancel") is None


def test_case_cloud_creation_does_not_block_event_loop(fake_runner):
    started = threading.Event()
    release = threading.Event()
    bridge = FakeBridge()
    original_create = bridge.create_run

    def create(payload):
        started.set()
        assert release.wait(timeout=2), "create_run blocked the event loop"
        return original_create(payload)

    bridge.create_run = create

    async def emit(message):
        pass

    async def run():
        task = asyncio.create_task(runner_service.run_case_local(
            case_id=5, env_id=3, device_serial="USB1", emit=emit, bridge=bridge
        ))
        while not started.is_set():
            await asyncio.sleep(0)
        release.set()
        assert (await task)["success"]

    asyncio.run(run())


def test_scheduler_loads_once_captures_input_and_closes(monkeypatch):
    from test_task_scheduler import _task

    fetched = []
    started = []
    source = FakeScenarioBridge()
    snapshot = source.fetch_scenario_execution_data(7, 3)
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: {"USB1", "USB2"})

    def fetch(bridge, scenario_id, env_id=None):
        fetched.append(bridge)
        return snapshot

    monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_scenario_execution_data", fetch)
    monkeypatch.setattr(scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs))
    result = task_scheduler.execute_task(_task(), {"Authorization": "Bearer first"})
    assert len(fetched) == 1 and fetched[0]._closed
    assert len(result) == len(started) == 2
    assert all(item["execution_data"] is snapshot for item in started)
    assert all(item["upstream"] == fetched[0].upstream for item in started)


def test_scheduler_closes_bridge_when_aggregate_fetch_fails(monkeypatch):
    from test_task_scheduler import _task

    bridges = []
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: {"USB1"})

    def fail(bridge, scenario_id, env_id=None):
        bridges.append(bridge)
        raise RuntimeError("cloud 503")

    monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_scenario_execution_data", fail)
    assert task_scheduler.execute_task(_task(), {}) == []
    assert len(bridges) == 1 and bridges[0]._closed


def test_scheduler_refresh_closes_bridge_on_failure(monkeypatch):
    from engine import presence

    bridges = []
    monkeypatch.setattr(config_store, "get_upstream", lambda: "https://first.example")
    monkeypatch.setattr(presence, "get_token", lambda: "first-token")

    def fail(bridge):
        bridges.append(bridge)
        raise RuntimeError("cloud 503")

    monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_client_tasks", fail)
    scheduler = task_scheduler.LocalTaskScheduler(runner=lambda task: None)
    try:
        assert scheduler.refresh_now()["refreshed"] == 0
        assert len(bridges) == 1 and bridges[0]._closed
    finally:
        scheduler.shutdown()


def test_fastbot_report_bridge_is_closed_on_finish_error(monkeypatch):
    closed = threading.Event()
    seen = []

    async def run(**kwargs):
        return {"crash_events": [], "summary": {"total_crashes": 0, "total_anrs": 0}}

    def finish(bridge, task_id, payload):
        seen.append(bridge)
        raise RuntimeError("cloud 503")

    original_close = cloud_bridge.CloudBridge.close

    def close(bridge):
        original_close(bridge)
        closed.set()

    monkeypatch.setattr(fastbot_service, "run_fastbot_local", run)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "finish_client_fastbot", finish)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "close", close)
    fastbot_service.run_and_report(
        task_id=9, auth_headers={"Authorization": "Bearer first"}, upstream="https://first.example",
        device_serial="USB1", package_name="com.example", duration=1, throttle=500,
        ignore_crashes=True, capture_log=False, enable_performance_monitor=False,
    )
    assert closed.wait(timeout=5)
    assert len(seen) == 1 and seen[0]._closed
    assert seen[0].upstream == "https://first.example"
    assert seen[0].auth_headers == {"Authorization": "Bearer first"}
