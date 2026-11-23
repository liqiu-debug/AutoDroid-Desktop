"""Launch request identity survives waiter loss without replaying device work."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.responses import JSONResponse

from engine.core import launch_requests
from engine.core.launch_requests import (
    LaunchRequestRegistry,
    launch_fingerprint,
    launch_ids,
    session_scope,
)


def _body(response):
    return json.loads(response.body)


def test_atomic_singleflight_shares_one_launch_and_caches_success():
    async def exercise():
        registry = LaunchRequestRegistry()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def launch():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return JSONResponse({"batch_id": "batch", "runs": [{"run_id": "run"}]})

        waiters = [asyncio.create_task(registry.submit("session", "request", "params", launch)) for _ in range(20)]
        await entered.wait()
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        responses = await asyncio.gather(*waiters)
        assert all(_body(response) == {"batch_id": "batch", "runs": [{"run_id": "run"}]} for response in responses)
        cached = await registry.submit("session", "request", "params", launch)
        assert _body(cached) == _body(responses[0])
        assert calls == 1
        await registry.close()

    asyncio.run(exercise())


def test_http_waiter_cancellation_leaves_launch_running_for_same_key_retry():
    async def exercise():
        registry = LaunchRequestRegistry()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def launch():
            nonlocal calls
            calls += 1
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return JSONResponse({"run_id": "first-run"})

        waiter = asyncio.create_task(registry.submit("session", "request", "params", launch))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not cancelled.is_set()
        retry = asyncio.create_task(registry.submit("session", "request", "params", launch))
        release.set()
        response = await retry
        assert _body(response) == {"run_id": "first-run"}
        assert calls == 1
        assert not cancelled.is_set()
        await registry.close()

    asyncio.run(exercise())


def test_same_key_different_parameters_conflict_during_and_after_launch():
    async def exercise():
        registry = LaunchRequestRegistry()
        entered, release = asyncio.Event(), asyncio.Event()

        async def launch():
            entered.set()
            await release.wait()
            return JSONResponse({"run_id": "original"})

        async def forbidden():
            pytest.fail("conflicting parameters started another launch")

        pending = asyncio.create_task(registry.submit("scope", "key", "original-params", launch))
        await entered.wait()
        during = await registry.submit("scope", "key", "other-params", forbidden)
        assert during.status_code == 409
        assert _body(during)["detail"]["code"] == "L1001_IDEMPOTENCY_CONFLICT"
        release.set()
        await pending
        after = await registry.submit("scope", "key", "other-params", forbidden)
        assert after.status_code == 409
        await registry.close()

    asyncio.run(exercise())


def test_rejected_data_read_or_precheck_can_retry_same_key():
    async def exercise():
        registry = LaunchRequestRegistry()
        attempts = 0

        async def launch():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return JSONResponse(status_code=504, content={"detail": "尚未启动，读取超时"})
            if attempts == 2:
                return JSONResponse(status_code=400, content={"detail": {"code": "S1001_SCENARIO_PRECHECK_FAILED", "items": []}})
            return JSONResponse({"run_id": "started-once"})

        assert (await registry.submit("scope", "key", "params", launch)).status_code == 504
        assert (await registry.submit("scope", "key", "params", launch)).status_code == 400
        response = await registry.submit("scope", "key", "params", launch)
        assert _body(response) == {"run_id": "started-once"}
        await registry.submit("scope", "key", "params", launch)
        assert attempts == 3
        await registry.close()

    asyncio.run(exercise())


def test_unknown_or_exception_result_is_retained_to_prevent_replay():
    async def exercise():
        for raises in (False, True):
            registry = LaunchRequestRegistry()
            attempts = 0

            async def launch():
                nonlocal attempts
                attempts += 1
                if raises:
                    raise RuntimeError("failure after a worker may have started")
                return JSONResponse(status_code=500, content={"detail": {"code": "L1004_LAUNCH_RESULT_UNKNOWN", "message": "结果待确认"}})

            first = await registry.submit("scope", "key", "params", launch)
            second = await registry.submit("scope", "key", "params", launch)
            assert first.status_code == second.status_code == 500
            assert _body(first)["detail"]["code"] == "L1004_LAUNCH_RESULT_UNKNOWN"
            assert first.body == second.body
            assert attempts == 1
            await registry.close()

    asyncio.run(exercise())


def test_identical_request_ids_are_independent_between_login_and_upstream_scopes():
    async def exercise():
        registry = LaunchRequestRegistry()
        scopes = [
            session_scope("https://first.example", {"Authorization": "Bearer alice"}),
            session_scope("https://first.example", {"Authorization": "Bearer bob"}),
            session_scope("https://second.example", {"Authorization": "Bearer alice"}),
        ]
        calls = []

        async def launch():
            calls.append(len(calls))
            return JSONResponse({"launch": calls[-1]})

        results = await asyncio.gather(*(registry.submit(scope, "same-key", "same-params", launch) for scope in scopes))
        assert len(calls) == 3
        assert {_body(response)["launch"] for response in results} == {0, 1, 2}
        await registry.close()

    asyncio.run(exercise())


def test_session_scope_uses_effective_auth_and_does_not_expose_credentials():
    scope = session_scope("https://server.example/", {"Authorization": "Bearer secret", "Cookie": "ignored", "X-Extra": "1"})
    assert scope == session_scope("https://server.example", {"authorization": "Bearer secret", "cookie": "other"})
    assert scope != session_scope("https://server.example", {"Authorization": "Bearer changed"})
    assert len(scope) == 64
    assert "secret" not in scope
    assert session_scope("https://server.example", {"cookie": "a"}) != session_scope("https://server.example", {"cookie": "b"})


def test_fingerprint_normalizes_device_order_and_duplicates_but_preserves_selection():
    fingerprint = launch_fingerprint("scenario", 7, 3, ["B", "A", "A"])
    assert fingerprint == launch_fingerprint("scenario", 7, 3, ["A", "B"])
    for kind, target, env, serials in [
        ("case", 7, 3, ["A", "B"]),
        ("scenario", 8, 3, ["A", "B"]),
        ("scenario", 7, 4, ["A", "B"]),
        ("scenario", 7, 3, ["A"]),
    ]:
        assert fingerprint != launch_fingerprint(kind, target, env, serials)


def test_run_ids_are_stable_per_request_device_session_and_engine(monkeypatch):
    batch, run_id = launch_ids("session", "request")
    retry_batch, retry_run_id = launch_ids("session", "request")
    assert batch == retry_batch
    assert run_id("USB1") == retry_run_id("USB1")
    assert run_id("USB1") != run_id("USB2")
    assert launch_ids("other-session", "request")[0] != batch
    assert launch_ids("session", "other-request")[0] != batch
    monkeypatch.setattr(launch_requests, "ENGINE_INSTANCE_ID", "new-engine-process")
    assert launch_ids("session", "request")[0] != batch
    assert launch_ids("session", None)[0] != launch_ids("session", None)[0]


def test_engine_shutdown_cancels_pending_launch_and_clears_registry():
    async def exercise():
        registry = LaunchRequestRegistry()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def launch():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        waiter = asyncio.create_task(registry.submit("scope", "key", "params", launch))
        await entered.wait()
        await registry.close()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert cancelled.is_set()
        assert not registry._requests

    asyncio.run(exercise())


def test_engine_shutdown_grace_waits_for_accepted_launch_without_cancelling():
    async def exercise():
        registry = LaunchRequestRegistry()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def launch():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return JSONResponse({"run_id": "accepted"})

        waiter = asyncio.create_task(registry.submit("scope", "key", "params", launch))
        await entered.wait()
        closing = asyncio.create_task(registry.close(grace_period=1))
        await asyncio.sleep(0)
        assert not closing.done()
        assert not cancelled.is_set()
        release.set()
        response = await waiter
        await closing
        assert _body(response) == {"run_id": "accepted"}
        assert not cancelled.is_set()
        assert not registry._requests

    asyncio.run(exercise())


def test_engine_shutdown_cancels_launch_only_after_grace_expires():
    async def exercise():
        registry = LaunchRequestRegistry()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def launch():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        waiter = asyncio.create_task(registry.submit("scope", "key", "params", launch))
        await entered.wait()
        closing = asyncio.create_task(registry.close(grace_period=0.02))
        await asyncio.sleep(0)
        assert not cancelled.is_set()
        await asyncio.wait_for(closing, timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert cancelled.is_set()
        assert not registry._requests

    asyncio.run(exercise())
