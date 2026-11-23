"""HTTP launch semantics with an ASGI cloud and no app/hardware lifecycle."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from engine import config_store, local_api, presence
from engine.core import runner_service, scenario_service
from engine.core.cloud_bridge import CloudBridge
from engine.core.launch_requests import ENGINE_INSTANCE_ID, LaunchRequestRegistry


def inputs(kind, target, count=1):
    case = {"case": {"id": 5, "name": "A", "variables": [], "steps": []},
            "standard_steps": [{"order": 1, "action": "click", "args": {}, "execute_on": ["android"],
                                "platform_overrides": {"android": {"by": "id", "selector": "#ok"}}}]}
    common = {"schema_version": 1, "env_variables": {}, "app_key_mapping": {}}
    if kind == "case":
        return {**common, **case}
    return {**common, "scenario": {"id": target, "name": "S"}, "cases": {"5": case},
            "scenario_steps": [{"id": i, "order": i, "case_id": 5, "alias": str(i)} for i in range(count)]}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    config_store.reset_for_tests()
    presence.reset_for_tests()
    config_store.set_upstream("http://cloud.invalid")
    app = FastAPI()
    app.include_router(local_api.router)
    app.state.launch_requests = LaunchRequestRegistry()
    state = {"fetches": [], "started": [], "created": [], "cloud_closed": 0}

    def fetch(kind, target, env):
        state["fetches"].append((kind, target, env))
        if state.get("fetch_error"):
            raise state["fetch_error"]
        return inputs(kind, target, state.get("count", 1))

    monkeypatch.setattr(CloudBridge, "fetch_case_execution_data", lambda self, target, env=None: fetch("case", target, env))
    monkeypatch.setattr(CloudBridge, "fetch_scenario_execution_data", lambda self, target, env=None: fetch("scenario", target, env))
    monkeypatch.setattr(runner_service, "start_background_case_run", lambda **kw: state["started"].append(kw))
    monkeypatch.setattr(scenario_service, "start_background_scenario_run", lambda **kw: state["started"].append(kw))

    async def cloud(request):
        state["created"].append(json.loads(request.content))
        return httpx.Response(200, json={"id": 40 + len(state["created"]), "created": True})

    async def run(body):
        async with httpx.AsyncClient(transport=httpx.MockTransport(cloud)) as upstream:
            app.state.http = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://engine") as client:
                try:
                    return await body(client)
                finally:
                    await app.state.launch_requests.close()

    yield app, state, run
    presence.reset_for_tests()
    config_store.reset_for_tests()


def headers(key="once", instance=ENGINE_INSTANCE_ID, token="a"):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key,
            "X-AutoDroid-Engine-Instance": instance}


@pytest.mark.parametrize("count", [1, 12, 100])
@pytest.mark.parametrize("kind", ["case", "scenario"])
def test_one_snapshot_for_deduplicated_multi_device_batch(harness, count, kind):
    _, state, run = harness
    state["count"] = count

    async def check(client):
        path = "/api/cases/5/run-batch" if kind == "case" else "/api/scenarios/7/run"
        response = await client.post(path, json={"env_id": 3, "device_serials": ["A", "B", "A"]}, headers=headers())
        assert response.status_code == 200
        result = response.json()
        assert len(state["fetches"]) == 1
        assert len(state["started"]) == 2
        assert [row["device_serial"] for row in result["runs"]] == ["A", "B"]
        assert state["started"][0]["execution_data"] == inputs(kind, 5 if kind == "case" else 7, count)
        assert state["started"][0]["upstream"] == "http://cloud.invalid"
        assert state["started"][0]["auth_headers"]["authorization"] == "Bearer a"
        retry = await client.post(path, json={"env_id": 3, "device_serials": ["B", "A"]}, headers=headers())
        assert retry.json() == result
        assert len(state["started"]) == 2
        assert len(state["fetches"]) == 1

    asyncio.run(run(check))


def test_local_single_case_is_prechecked_before_background_start(harness):
    _, state, run = harness

    async def check(client):
        blocked = await client.post("/api/cases/5/run?device_serial=ios:00008110-001234567890001E")
        assert blocked.status_code == 400
        assert blocked.json()["detail"]["code"] == "C1001_CASE_PRECHECK_FAILED"
        assert state["started"] == []
        accepted = await client.post("/api/cases/5/run?device_serial=A")
        assert accepted.status_code == 200
        assert len(state["started"]) == 1

    asyncio.run(run(check))


def test_partial_block_and_all_block_keep_concrete_precheck_reasons(harness):
    _, state, run = harness

    async def check(client):
        response = await client.post("/api/scenarios/7/run", json={"device_serials": ["A", None, "ios:00008110-001234567890001E"]})
        assert response.status_code == 200
        result = response.json()
        assert [item["device_serial"] for item in result["runs"]] == ["A"]
        assert len(result["blocked_prechecks"]) == 2
        response = await client.post("/api/scenarios/7/run", json={"device_serials": ["ios:00008110-001234567890001E"]})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "S1001_SCENARIO_PRECHECK_FAILED"
        assert len(state["started"]) == 1

    asyncio.run(run(check))


def test_launch_instance_and_parameter_conflicts_never_submit(harness):
    _, state, run = harness

    async def check(client):
        path = "/api/cases/5/run-batch"
        body = {"device_serials": ["A"]}
        old = await client.post(path, json=body, headers=headers(instance="old-process"))
        assert old.status_code == 409
        assert old.json()["detail"]["code"] == "L1002_ENGINE_INSTANCE_CHANGED"
        missing = await client.post(path, json=body, headers={"Idempotency-Key": "once"})
        assert missing.status_code == 400
        assert not state["fetches"]
        first = await client.post(path, json=body, headers=headers())
        assert first.status_code == 200
        changed = await client.post(path, json={**body, "env_id": 2}, headers=headers())
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "L1001_IDEMPOTENCY_CONFLICT"
        assert len(state["started"]) == 1

    asyncio.run(run(check))


@pytest.mark.parametrize("status", [401, 403, 404, 502])
def test_cloud_read_failure_is_not_reported_as_missing_case(harness, status):
    _, state, run = harness
    request = httpx.Request("GET", "http://cloud.invalid/execution-data")
    response = httpx.Response(status, request=request, json={"detail": "云端不可用"})
    state["fetch_error"] = httpx.HTTPStatusError("cloud failure", request=request, response=response)

    async def check(client):
        result = await client.post("/api/scenarios/7/run", json={"device_serials": ["A"]})
        assert result.status_code == status
        assert result.json()["detail"] == "云端不可用"
        assert state["started"] == []

    asyncio.run(run(check))


def test_slow_cloud_record_is_cancelled_at_shared_deadline(harness, monkeypatch):
    _, state, run = harness
    monkeypatch.setattr(local_api, "CREATE_RECORD_DEADLINE_SECONDS", 0.03)
    cancelled = []

    async def create(self, payload, client):
        if payload["device_serial"] == "A":
            return {"id": 42}
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(payload["run_id"])

    monkeypatch.setattr(CloudBridge, "create_run_async", create)

    async def check(client):
        response = await asyncio.wait_for(client.post("/api/scenarios/7/run", json={"device_serials": ["A", "B"]}), 1)
        assert response.status_code == 200
        rows = response.json()["runs"]
        assert [row["execution_id"] for row in rows] == [42, None]
        assert [row["cloud_record_pending"] for row in rows] == [False, True]
        assert cancelled == [rows[1]["run_id"]]
        assert state["started"][1]["run_id"] == rows[1]["run_id"]
        assert state["started"][1]["preset_run_key"] is None

    asyncio.run(run(check))


def test_precreate_concurrency_is_bounded(harness, monkeypatch):
    _, _, run = harness
    active = 0
    maximum = 0

    async def create(self, payload, client):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.005)
            return {"id": 42}
        finally:
            active -= 1

    monkeypatch.setattr(CloudBridge, "create_run_async", create)

    async def check(client):
        response = await client.post("/api/scenarios/7/run", json={"device_serials": [str(i) for i in range(13)]})
        assert response.status_code == 200
        assert maximum == 4

    asyncio.run(run(check))


def test_cancel_precreation_closes_confirmed_and_response_lost_records():
    async def check():
        records = {}
        entered = asyncio.Event()
        first_unconfirmed = True

        class Bridge:
            auth_headers = {"Authorization": "Bearer test"}

            def _url(self, path):
                return f"http://cloud.invalid{path}"

            async def create_run_async(self, payload, client):
                nonlocal first_unconfirmed
                serial = payload["device_serial"]
                record = records.setdefault(serial, {"id": len(records) + 1, "status": "RUNNING"})
                if serial == "B" and first_unconfirmed:
                    first_unconfirmed = False
                    entered.set()
                    await asyncio.Event().wait()
                return record

        def cloud(request):
            key = int(request.url.path.split("/")[-2])
            record = next(record for record in records.values() if record["id"] == key)
            record["status"] = json.loads(request.content)["status"]
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(cloud)) as client:
            launch = asyncio.create_task(local_api._precreate_scenario_records(
                Bridge(), client, [{"device_serial": "A", "run_id": "a"}, {"device_serial": "B", "run_id": "b"}],
            ))
            await entered.wait()
            launch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await launch
        assert len(records) == 2
        assert {record["status"] for record in records.values()} == {"ABORTED"}

    asyncio.run(check())


def test_partial_thread_failure_returns_only_started_devices_and_closes_cloud_record(harness, monkeypatch):
    app, state, run = harness

    def start(**kwargs):
        if kwargs["device_serial"] == "B":
            raise RuntimeError("thread unavailable")
        state["started"].append(kwargs)

    monkeypatch.setattr(scenario_service, "start_background_scenario_run", start)

    async def check(client):
        finished = []

        async def cloud(request):
            payload = json.loads(request.content)
            if request.url.path.endswith("/finish"):
                finished.append(payload)
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"id": 41 if payload["device_serial"] == "A" else 42})

        async with httpx.AsyncClient(transport=httpx.MockTransport(cloud)) as upstream:
            app.state.http = upstream
            path = "/api/scenarios/7/run"
            body = {"device_serials": ["A", "B"]}
            response = await client.post(path, json=body, headers=headers())
            assert response.status_code == 200
            assert [row["device_serial"] for row in response.json()["runs"]] == ["A"]
            assert response.json()["blocked_prechecks"][0]["device_serial"] == "B"
            assert finished[0]["status"] == "ERROR"
            retry = await client.post(path, json=body, headers=headers())
            assert retry.json() == response.json()
            assert len(state["started"]) == 1

    asyncio.run(run(check))


def test_response_lost_record_is_resolved_and_finished_if_thread_fails(harness, monkeypatch):
    app, state, run = harness

    def start(**kwargs):
        if kwargs["device_serial"] == "B":
            raise RuntimeError("thread unavailable")
        state["started"].append(kwargs)

    monkeypatch.setattr(scenario_service, "start_background_scenario_run", start)

    async def check(client):
        records = {}
        attempts = []

        async def cloud(request):
            payload = json.loads(request.content)
            if request.url.path.endswith("/finish"):
                key = int(request.url.path.split("/")[-2])
                next(row for row in records.values() if row["id"] == key)["status"] = payload["status"]
                return httpx.Response(200, json={})
            record = records.setdefault(payload["run_id"], {
                "id": len(records) + 1, "status": "RUNNING", "serial": payload["device_serial"],
            })
            attempts.append(payload["run_id"])
            if record["serial"] == "B" and attempts.count(payload["run_id"]) == 1:
                raise httpx.ReadTimeout("response lost after commit", request=request)
            return httpx.Response(200, json={"id": record["id"]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(cloud)) as upstream:
            app.state.http = upstream
            response = await client.post("/api/scenarios/7/run", json={"device_serials": ["A", "B"]}, headers=headers())
        assert response.status_code == 200
        assert len(records) == 2
        assert next(row for row in records.values() if row["serial"] == "B")["status"] == "ERROR"
        assert len(state["started"]) == 1

    asyncio.run(run(check))
