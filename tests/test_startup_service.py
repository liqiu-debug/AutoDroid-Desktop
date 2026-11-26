"""Startup lifecycle tests with fake adapters/cloud; never touch a real device."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from engine import config_store
from engine.core import device_busy, run_registry, startup_service
from engine.core.run_registry import LocalRunRecord, LocalRunRegistry
from engine.core.startup.contract import StartupConfig
from engine.core.startup_service import StartupService, _empty_report
from safe_lifespan import create_test_app


class Cloud:
    def __init__(self):
        self.created = {}
        self.finish = []
        self.progress = []
        self.recovered = []
        self.uploaded = []
        self.fail_finish = False
        self.response_lost = False
        self.owner = 7
        self.cancel = False
        self.devices = None

    def bridge(self, upstream="https://test.example", auth_headers=None):
        cloud = self

        class Bridge:
            def __init__(self):
                self.upstream = upstream
                self.auth_headers = auth_headers or {"Authorization": "Bearer private-login-token"}

            def __enter__(self): return self
            def __exit__(self, *args): return None
            def close(self): pass

            def get_json(self, path):
                if path.endswith("users/me"):
                    return {"id": cloud.owner}
                return cloud.devices or [{"serial": "USB1", "platform": "android", "status": "IDLE",
                    "client_instance_id": config_store.get_client_instance_id()}]

            def create_client_fastbot(self, payload):
                created = cloud.created.setdefault(payload["run_id"], {
                    "id": len(cloud.created) + 1, "task_id": len(cloud.created) + 1,
                    "run_id": payload["run_id"], "attempt": 1, "claim_token": "private-claim-token",
                    "status": "RUNNING", "config": payload["startup_config"],
                })
                if cloud.response_lost:
                    cloud.response_lost = False
                    raise httpx.ReadTimeout("create response lost")
                return deepcopy(created)

            def progress_startup(self, task_id, payload):
                cloud.progress.append((task_id, deepcopy(payload)))
                return {"cancel_requested": cloud.cancel}

            def finish_startup(self, task_id, payload):
                if cloud.fail_finish:
                    raise httpx.ConnectError("offline")
                cloud.finish.append((task_id, deepcopy(payload)))
                return {"status": payload["status"]}

            def recover_startup(self, task_id, payload):
                cloud.recovered.append((task_id, deepcopy(payload)))
                return {"status": payload.get("local_status", "ABORTED"), "attempt": 1,
                        "claim_token": "recovered-claim-token"}

            def upload_client_fastbot_artifact(self, task_id, path, relative, *, guard):
                cloud.uploaded.append((task_id, path, relative, deepcopy(guard)))
                return {"path": f"reports/fastbot/{task_id}/attempts/1/{relative}"}

        return Bridge()


class Adapter:
    def __init__(self, serial, platform, artifact_dir):
        self.device_serial, self.platform = serial, platform
        self.artifact_dir = artifact_dir
        self.closed = 0

    async def preflight(self, config): return {"ready": True}
    async def close(self): self.closed += 1


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    config_store.reset_for_tests()
    run_registry.registry.reset_for_tests()
    monkeypatch.setattr(startup_service, "_local_serials", lambda: {"USB1", "USB2"})
    yield
    run_registry.registry.reset_for_tests()
    config_store.reset_for_tests()


def payload(**kwargs):
    return {"platform": "android", "package_name": "com.example.app", "device_serials": ["USB1"],
            "iterations": 2, "cooldown_sec": 0, "startup_modes": ["cold"],
            "perfetto_slow_trace": {"enabled": False}, "request_id": "one-request", **kwargs}


async def drain(service):
    tasks = [job["task"] for job in list(service.jobs.values())]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)


def fake_runner(monkeypatch, calls, *, blocked=None, artifacts=False):
    async def run(adapter, config, *, stop_event, progress):
        calls.append(adapter.device_serial)
        report = _empty_report(config)
        report["summary"]["startup_runs"] = [{"mode": "cold", "iteration": 1, "sample_valid": True}]
        report["summary"]["progress"]["completed_count"] = 1
        await progress(report)
        if blocked:
            blocked.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.005)
        if artifacts:
            path = adapter.artifact_dir / "screenshots" / "failure.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"png")
            report["trace_artifacts"] = [{"path": "screenshots/failure.png"}]
        report["summary"]["status"] = "ABORTED" if stop_event.is_set() else "COMPLETED"
        return report
    monkeypatch.setattr("engine.core.startup.runner.run_startup", run)


def test_local_idempotency_single_launch_and_credential_free_outbox(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)

    async def scenario():
        one, two = await asyncio.gather(service.start(cloud.bridge(), payload()), service.start(cloud.bridge(), payload()))
        assert one["tasks"][0]["run_id"] == two["tasks"][0]["run_id"]
        await drain(service)
        assert calls == ["USB1"]
        assert len(cloud.created) == len(cloud.finish) == 1
        assert not device_busy.is_busy("USB1")
        saved = service.entries()[0]
        assert saved["status"] == "COMPLETED" and saved["sync_state"] == "synced"
        disk = service._path(saved["run_id"]).read_text()
        assert "private-login-token" not in disk and "private-claim-token" not in disk
        with pytest.raises(ValueError, match="其他配置"):
            await service.start(cloud.bridge(), payload(iterations=3))
    asyncio.run(scenario())


def test_upload_failure_recovers_same_task_with_artifact_attempt_guard(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls, artifacts=True)
    cloud.fail_finish = True
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)

    async def scenario():
        await service.start(cloud.bridge(), payload())
        await drain(service)
        assert service.entries()[0]["sync_state"] == "pending"
        cloud.fail_finish = False
        restarted = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
        await restarted.recover(cloud.bridge())
        assert len(calls) == len(cloud.created) == len(cloud.finish) == 1
        assert cloud.recovered[0][1]["claim_token"] == ""
        assert cloud.recovered[0][1]["local_status"] == "COMPLETED"
        assert cloud.uploaded[0][3]["attempt"] == 1
        assert cloud.finish[0][1]["report"]["trace_artifacts"][0]["path"].endswith("attempts/1/screenshots/failure.png")
        assert restarted.entries()[0]["sync_state"] == "synced"
    asyncio.run(scenario())


def test_restart_marks_partial_aborted_and_never_replays_device(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    run_id = str(uuid.uuid4())
    config = StartupConfig.model_validate(payload()).model_dump()
    report = _empty_report(config)
    report["summary"]["startup_runs"] = [{"iteration": 1, "automation_ms": 42}]
    entry = {"run_id": run_id, "task_id": 12, "owner_id": "7", "scope": startup_service._scope("https://test.example", "7"),
             "upstream": "https://test.example", "device_serial": "USB1", "config": config,
             "stage": "RUNNING", "status": "RUNNING", "sync_state": "pending", "attempt": 1,
             "artifacts": {}, "report": report}
    service._persist(entry)
    asyncio.run(service.recover(cloud.bridge()))
    assert calls == [] and cloud.created == {}
    assert cloud.finish[0][0] == 12 and cloud.finish[0][1]["status"] == "ABORTED"
    assert cloud.finish[0][1]["report"]["summary"]["startup_runs"][0]["automation_ms"] == 42


def test_lost_create_response_reconciles_without_running_samples(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    cloud.response_lost = True
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    async def scenario():
        response = await service.start(cloud.bridge(), payload())
        assert response["tasks"][0]["status"] == "ABORTED"
        assert calls == [] and not device_busy.is_busy("USB1")
        await service.recover(cloud.bridge())
        assert len(cloud.created) == 1 and len(cloud.finish) == 1
        assert cloud.finish[0][1]["status"] == "ABORTED"
        assert calls == []
    asyncio.run(scenario())


def test_cancel_preserves_partial_and_device_is_held_until_adapter_exits(tmp_path, monkeypatch):
    cloud, calls, started = Cloud(), [], threading.Event()
    fake_runner(monkeypatch, calls, blocked=started)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    async def scenario():
        response = await service.start(cloud.bridge(), payload())
        while not started.is_set(): await asyncio.sleep(0.005)
        assert device_busy.is_busy("USB1")
        assert service.cancel(response["tasks"][0]["id"])
        assert device_busy.is_busy("USB1")
        await drain(service)
        assert not device_busy.is_busy("USB1")
        assert cloud.finish[0][1]["status"] == "ABORTED"
        assert len(cloud.finish[0][1]["report"]["summary"]["startup_runs"]) == 1
    asyncio.run(scenario())


def test_foreign_account_cannot_upload_or_read_outbox(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    cloud.fail_finish = True
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    async def scenario():
        await service.start(cloud.bridge(), payload())
        await drain(service)
        cloud.owner = 8
        cloud.fail_finish = False
        await service.recover(cloud.bridge())
        assert cloud.finish == [] and cloud.recovered == []
        assert (await service.local_results(cloud.bridge()))["items"] == []
    asyncio.run(scenario())


def test_batch_preflight_failure_releases_all_without_creating_cloud_task(tmp_path):
    cloud = Cloud()
    cloud.devices = [{"serial": serial, "client_instance_id": config_store.get_client_instance_id()}
                     for serial in ["USB1", "USB2"]]
    class BadAdapter(Adapter):
        async def preflight(self, config):
            if self.device_serial == "USB2": raise ValueError("目标应用未安装")
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=BadAdapter)
    async def scenario():
        with pytest.raises(ValueError, match="未安装"):
            await service.start(cloud.bridge(), payload(device_serials=["USB1", "USB2"]))
        assert not device_busy.is_busy("USB1") and not device_busy.is_busy("USB2")
        assert not cloud.created
    asyncio.run(scenario())


def test_duplicate_web_claim_does_not_create_or_execute_twice(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    command = {"task_id": 9, "run_id": str(uuid.uuid4()), "attempt": 1, "claim_token": "private-claim-token",
               "config": payload(), "device_serial": "USB1"}
    async def scenario():
        await service.accept_command(cloud.bridge(), command, "7")
        await service.accept_command(cloud.bridge(), command, "7")
        await drain(service)
        assert calls == ["USB1"] and cloud.created == {}
        assert cloud.finish[0][0] == 9
    asyncio.run(scenario())


def test_case_fastbot_startup_mutual_exclusion_is_atomic():
    barrier = threading.Barrier(3)
    successes = []
    registries = []
    def attempt(kind):
        barrier.wait()
        try:
            if kind == "fastbot":
                if device_busy.try_acquire("USB1"): successes.append(kind)
            else:
                registry = LocalRunRegistry()
                registries.append(registry)
                registry.register(LocalRunRecord(kind=kind, target_id=1, device_serial="USB1"))
                successes.append(kind)
        except ValueError:
            pass
    threads = [threading.Thread(target=attempt, args=(kind,)) for kind in ["case", "startup", "fastbot"]]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(successes) == 1
    for registry in registries: registry.reset_for_tests()


def test_cancel_does_not_release_device_until_actual_complete():
    registry = LocalRunRegistry()
    record = registry.register(LocalRunRecord(kind="case", target_id=1, device_serial="USB1"))
    registry.cancel(run_ids=[record.run_id])
    assert device_busy.is_busy("USB1")
    assert not device_busy.try_acquire("USB1")
    registry.complete(record.run_id)
    assert device_busy.try_acquire("USB1")


def test_ios_aliases_share_atomic_device_and_recording_is_blocked():
    serial = "00008030-0012345678901234"
    assert device_busy.try_acquire("ios:" + serial, "startup", owner="run")
    assert not device_busy.try_acquire(serial)
    from engine.core import recording_service
    with pytest.raises(recording_service.RecordingError, match="冷热启动") as exc:
        recording_service.interact_device(0, 0, device_serial=serial)
    assert exc.value.status_code == 409


def test_startup_routes_are_local_and_info_declares_protocol(monkeypatch):
    async def devices(bridge): return {"items": [{"serial": "USB1", "available": True}]}
    async def start(bridge, data): return {"tasks": [{"id": 11}]}
    monkeypatch.setattr(startup_service.service, "devices", devices)
    monkeypatch.setattr(startup_service.service, "start", start)
    with TestClient(create_test_app()) as client:
        assert client.get("/api/client/info").json()["startup"]["protocol_version"] == 1
        assert client.get("/api/fastbot/startup/devices").json()["items"][0]["serial"] == "USB1"
        assert client.post("/api/fastbot/startup/run", json=payload()).json()["tasks"][0]["id"] == 11


def test_startup_bridge_guards_and_recover_paths(monkeypatch):
    from engine.core.cloud_bridge import CloudBridge
    bridge = CloudBridge(upstream="https://test.example", auth_headers={"Authorization": "Bearer token"})
    calls = []
    monkeypatch.setattr(bridge, "post_json", lambda path, payload, **kw: calls.append((path, payload)) or {})
    guard = {"client_instance_id": "node", "attempt": 3, "claim_token": "fence"}
    bridge.claim_startup({"client_instance_id": "node"})
    bridge.progress_startup(1, guard)
    bridge.finish_startup(1, {**guard, "status": "ABORTED"})
    bridge.recover_startup(1, {**guard, "run_id": "r"})
    assert [path for path, _ in calls] == ["/api/client-fastbot/startup/claim", "/api/client-fastbot/startup/1/progress",
                                         "/api/client-fastbot/startup/1/finish", "/api/client-fastbot/startup/1/recover"]
    assert calls[2][1]["attempt"] == 3


def test_lease_deadline_fences_actions_without_waiting_for_heartbeat(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    monkeypatch.setattr(service, "_lease_until", lambda entry: time.monotonic() - 1)
    async def scenario():
        await service.start(cloud.bridge(), payload())
        await drain(service)
        assert calls == []
        assert cloud.finish[0][1]["status"] == "ABORTED"
        assert not device_busy.is_busy("USB1")
    asyncio.run(scenario())


def test_lease_stop_event_observes_updated_deadline():
    record = LocalRunRecord(kind="startup", target_id=1, device_serial="USB1")
    job = {"record": record, "lease_until": time.monotonic() + 20}
    stop = startup_service.LeaseStopEvent(job)
    assert not stop.is_set()
    job["lease_until"] = time.monotonic() - 1
    assert stop.is_set() and record.abort_event.is_set()


def test_offline_cancel_requires_exact_validated_owner_login(tmp_path, monkeypatch):
    cloud, calls, started = Cloud(), [], threading.Event()
    fake_runner(monkeypatch, calls, blocked=started)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    async def scenario():
        result = await service.start(cloud.bridge(), payload())
        while not started.is_set(): await asyncio.sleep(0.005)
        task_id = result["tasks"][0]["id"]
        assert not service.cancel_authenticated_local(cloud.bridge(auth_headers={"Authorization": "Bearer other"}), task_id)
        assert service.cancel_authenticated_local(cloud.bridge(), task_id)
        assert device_busy.is_busy("USB1")
        await drain(service)
        assert cloud.finish[0][1]["status"] == "ABORTED"
    asyncio.run(scenario())


def test_offline_stop_route_and_401_cannot_bypass_authorization(tmp_path, monkeypatch):
    from engine import local_api
    cloud = Cloud()
    service = StartupService(tmp_path)
    bridge = cloud.bridge(auth_headers={"authorization": "Bearer owner"})
    service._validated_login = (bridge.upstream, bridge.auth_headers, "7")
    record = LocalRunRecord(kind="startup", target_id=8, device_serial="USB1")
    service.jobs[record.run_id] = {"record": record, "entry": {
        "task_id": 8, "scope": startup_service._scope(bridge.upstream, "7")}}
    def offline(task_id): raise httpx.ConnectError("offline")
    bridge.stop_startup = offline
    monkeypatch.setattr(local_api, "CloudBridge", lambda **kwargs: bridge)
    monkeypatch.setattr(startup_service, "service", service)
    with TestClient(create_test_app()) as client:
        result = client.post("/api/fastbot/startup/tasks/8/stop", headers={"Authorization": "Bearer owner"})
        assert result.status_code == 200 and result.json()["local_cancel_requested"]
        assert record.abort_event.is_set()
        record.abort_event.clear()
        def unauthorized(task_id):
            response = httpx.Response(401, request=httpx.Request("POST", "https://test.example"))
            raise httpx.HTTPStatusError("expired", request=response.request, response=response)
        bridge.stop_startup = unauthorized
        assert client.post("/api/fastbot/startup/tasks/8/stop").status_code == 401
        assert not record.abort_event.is_set()


def test_local_results_never_reuses_identity_after_http_401(tmp_path):
    service, cloud = StartupService(tmp_path), Cloud()
    bridge = cloud.bridge()
    async def scenario():
        assert (await service.local_results(bridge))["items"] == []
        def unauthorized(path):
            response = httpx.Response(401, request=httpx.Request("GET", "https://test.example"))
            raise httpx.HTTPStatusError("expired", request=response.request, response=response)
        bridge.get_json = unauthorized
        with pytest.raises(httpx.HTTPStatusError):
            await service.local_results(bridge)
    asyncio.run(scenario())


def test_session_change_during_preflight_never_launches_or_creates_cloud_task(tmp_path):
    cloud, entered, release = Cloud(), asyncio.Event(), asyncio.Event()
    class SlowPreflight(Adapter):
        async def preflight(self, config):
            entered.set()
            await release.wait()
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=SlowPreflight)
    async def scenario():
        task = asyncio.create_task(service.start(cloud.bridge(), payload()))
        await entered.wait()
        service.cancel_session()
        release.set()
        with pytest.raises(ValueError, match="登录状态已变化"):
            await task
        assert not cloud.created and not device_busy.is_busy("USB1") and not service.jobs
    asyncio.run(scenario())


def test_session_logout_signals_active_job_and_clears_cached_login(tmp_path, monkeypatch):
    from engine import presence
    service = StartupService(tmp_path)
    record = LocalRunRecord(kind="startup", target_id=1, device_serial="USB1")
    service.jobs[record.run_id] = {"record": record}
    service._validated_login = ("https://test.example", {"authorization": "Bearer owner"}, "7")
    monkeypatch.setattr(startup_service, "service", service)
    monkeypatch.setattr(presence, "_token", "owner")
    presence.clear_token()
    assert record.abort_event.is_set() and service._validated_login is None


def test_delayed_old_logout_cannot_clear_new_login(monkeypatch):
    from engine import presence
    monkeypatch.setattr(presence, "_token", "new-login")
    calls = []
    async def logout(*args, **kwargs): calls.append(kwargs)
    monkeypatch.setattr(presence, "logout_once", logout)
    with TestClient(create_test_app()) as client:
        response = client.post("/api/client/session/logout", headers={"Authorization": "Bearer old-login"})
        assert response.status_code == 200 and response.json()["session_changed"]
        assert presence.get_token() == "new-login" and not calls
        assert client.post("/api/client/session/logout").status_code == 401


def test_current_logout_cancels_before_cloud_wait_and_keeps_concurrent_login(monkeypatch):
    from engine import presence
    monkeypatch.setattr(presence, "_token", "old-login")
    calls = []
    async def logout(*args, **kwargs):
        assert presence.get_token() is None
        assert not presence.can_adopt_request_token("old-login")
        calls.append(kwargs)
        presence.set_token("new-login")
    monkeypatch.setattr(presence, "logout_once", logout)
    with TestClient(create_test_app()) as client:
        response = client.post("/api/client/session/logout", headers={"Authorization": "Bearer old-login"})
        assert response.status_code == 200 and response.json()["logged_out"]
        assert presence.get_token() == "new-login"
        assert calls[0]["auth_headers"] == {"Authorization": "Bearer old-login"}


def test_old_device_list_cannot_change_login_or_schedule_cloud_rebind(monkeypatch):
    from engine import presence
    config_store.set_upstream("https://test.example")
    monkeypatch.setattr(presence, "_token", "new-login")
    monkeypatch.setattr("engine.app.scan_devices", lambda: (_ for _ in ()).throw(AssertionError("old scan")))
    async def listing(request):
        assert request.method == "GET"
        return httpx.Response(200, json=[])
    async def scenario():
        app = create_test_app()
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            await app.state.http.aclose()
            app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(listing))
            app.state.local_presence = {"ts": 0, "attempt_ts": 0, "serials": set(), "task": None}
            app.state.device_discovery = {"ts": 0, "serials": set(), "task": None}
            app.state.presence_heal = {"ts": 0, "task": None}
            async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
                response = await client.get("/api/devices/", headers={"Authorization": "Bearer old-login"})
            assert response.status_code == 200
            assert presence.get_token() == "new-login"
            assert app.state.device_discovery["task"] is None
            refresh = app.state.local_presence.get("task")
            if refresh: await refresh
            await app.state.http.aclose()
    monkeypatch.setattr("engine.app.local_serials", lambda: set())
    asyncio.run(scenario())


def test_worker_does_not_claim_with_old_login_after_identifying_session_changes(tmp_path, monkeypatch):
    from engine import presence
    cloud, identified, allow = Cloud(), threading.Event(), threading.Event()
    claims = []
    def factory(**kwargs):
        bridge = cloud.bridge(**kwargs)
        original = bridge.get_json
        def identify(path):
            if path.endswith("users/me"):
                identified.set()
                assert allow.wait(1)
            return original(path)
        bridge.get_json = identify
        bridge.claim_startup = lambda payload: claims.append(dict(bridge.auth_headers)) or {}
        return bridge
    service = StartupService(tmp_path, bridge_factory=factory)
    monkeypatch.setattr(startup_service, "service", service)
    monkeypatch.setattr(config_store, "get_upstream", lambda: "https://test.example")
    monkeypatch.setattr(presence, "_token", "old")
    async def scenario():
        task = asyncio.create_task(startup_service._worker_loop())
        while not identified.is_set(): await asyncio.sleep(0.005)
        presence.set_token("new")
        allow.set()
        await asyncio.sleep(0.03)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert all(headers == {"Authorization": "Bearer new"} for headers in claims)
    asyncio.run(scenario())


def test_finished_persist_failure_still_releases_device_and_cleans_job(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    fake_runner(monkeypatch, calls)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    real_persist = service._persist
    def persist(entry):
        if entry["stage"] == "FINISHED": raise OSError("disk full")
        real_persist(entry)
    monkeypatch.setattr(service, "_persist", persist)
    async def scenario():
        await service.start(cloud.bridge(), payload())
        await drain(service)
        assert not device_busy.is_busy("USB1") and not service.jobs
        assert cloud.finish[0][1]["status"] == "COMPLETED"
    asyncio.run(scenario())


def test_batch_disk_failure_releases_every_prepared_device(tmp_path, monkeypatch):
    cloud = Cloud()
    cloud.devices = [{"serial": serial, "client_instance_id": config_store.get_client_instance_id()}
                     for serial in ["USB1", "USB2"]]
    adapters = []
    def adapter(*args):
        item = Adapter(*args)
        adapters.append(item)
        return item
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=adapter)
    def fail(entry): raise OSError("disk full")
    monkeypatch.setattr(service, "_persist", fail)
    async def scenario():
        with pytest.raises(OSError, match="disk full"):
            await service.start(cloud.bridge(), payload(device_serials=["USB1", "USB2"]))
        assert all(item.closed for item in adapters)
        assert not device_busy.is_busy("USB1") and not device_busy.is_busy("USB2")
        assert not cloud.created
    asyncio.run(scenario())


def test_trace_analysis_runs_after_device_release_and_canonical_path_is_preserved(tmp_path, monkeypatch):
    cloud, calls = Cloud(), []
    async def run(adapter, config, *, stop_event, progress):
        calls.append(adapter.device_serial)
        report = _empty_report(config, status="COMPLETED")
        path = adapter.artifact_dir / "traces" / "startup_cold_001.perfetto-trace"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"trace")
        report["trace_artifacts"] = [{"path": "traces/startup_cold_001.perfetto-trace",
                                      "analysis_status": "PENDING", "capture_mode": "original_sample"}]
        return report
    def analyze(path, package, *, platform_name):
        assert not device_busy.is_busy("USB1")
        assert path.read_bytes() == b"trace" and package == "com.example.app" and platform_name == "android"
        return {"analysis_status": "ANALYZED", "analysis_level": "startup_evidence", "startup_intervals": [{"dur": 123}]}
    monkeypatch.setattr("engine.core.startup.runner.run_startup", run)
    monkeypatch.setattr("engine.core.startup_trace_analysis.analyze_startup_trace", analyze)
    service = StartupService(tmp_path, bridge_factory=cloud.bridge, adapter_factory=Adapter)
    async def scenario():
        await service.start(cloud.bridge(), payload())
        await drain(service)
        artifact = cloud.finish[0][1]["report"]["trace_artifacts"][0]
        assert artifact["path"].endswith("attempts/1/traces/startup_cold_001.perfetto-trace")
        assert artifact["analysis_status"] == "ANALYZED"
        assert artifact["analysis"]["startup_intervals"] == [{"dur": 123}]
        assert calls == ["USB1"]
    asyncio.run(scenario())


def test_manual_wda_check_and_screenshot_are_blocked_before_io(monkeypatch):
    from engine.core import ios_device
    serial = "00008030-0012345678901234"
    def unexpected(*args, **kwargs): raise AssertionError("must not reach device during startup")
    monkeypatch.setattr(ios_device, "wda_url", unexpected)
    monkeypatch.setattr(ios_device, "preview_screenshot", unexpected)
    assert device_busy.try_acquire(serial, "startup", owner="test")
    with TestClient(create_test_app()) as client:
        assert client.post(f"/api/devices/{serial}/wda/check").status_code == 409
        assert client.get(f"/api/devices/{serial}/screenshot").status_code == 409


def test_recording_pool_drain_waits_for_owner_and_closes_once(monkeypatch):
    from engine.core import ios_device
    serial = "00008030-0012345678901234"
    closed = []
    class Driver:
        def disconnect(self): closed.append("closed")
    pool = ios_device.IOSDriverPool(driver_factory=lambda *args: Driver())
    monkeypatch.setattr(ios_device, "wda_url", lambda *args: "http://fake")
    driver = pool.acquire(serial)
    assert device_busy.try_acquire(serial, "startup", owner="test")
    done = threading.Event()
    worker = threading.Thread(target=lambda: (pool.drain_for_collection(serial), done.set()))
    worker.start()
    assert not done.wait(0.02)
    assert closed == [] and pool._entries[serial].lock.locked()
    pool.release(serial, driver)
    worker.join(1)
    assert done.is_set() and closed == ["closed"] and serial not in pool._entries


def test_mjpeg_preflight_drains_inflight_reader_before_collection(monkeypatch):
    from engine.core import ios_device, ios_mjpeg
    serial = "00008030-0012345678901234"
    entered, release, drained = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(ios_device, "wda_url", lambda *args, **kwargs: (entered.set(), release.wait(1)))
    hub = ios_mjpeg.MjpegHub(serial)
    async def scenario():
        queue = hub.subscribe(asyncio.get_running_loop())
        while not entered.is_set(): await asyncio.sleep(0.005)
        assert device_busy.try_acquire(serial, "startup", owner="test")
        worker = threading.Thread(target=lambda: (hub.pause_for_collection(), drained.set()))
        worker.start()
        assert not drained.wait(0.02)
        release.set()
        await asyncio.to_thread(worker.join, 1)
        assert drained.is_set() and all(not thread.is_alive() for thread, _ in hub._readers)
        assert await queue.get() is None
        assert await hub.subscribe(asyncio.get_running_loop()).get() is None
        hub.unsubscribe(queue)
    asyncio.run(scenario())
