"""Single-flight HTTP launches, scoped to one engine process and login session.

An HTTP waiter can disappear while a launch is creating cloud records. Keep the
launch task and its response until process exit so retrying a key never replays
device actions. This registry deliberately does not retain execution inputs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
ENGINE_INSTANCE_ID = str(uuid.uuid4())


def session_scope(upstream: str, auth_headers: Dict[str, str]) -> str:
    headers = {key.lower(): value for key, value in auth_headers.items()}
    credential = headers.get("authorization") or headers.get("cookie") or ""
    # Credentials and their digests stay in memory and are never logged.
    return hashlib.sha256(f"{upstream.rstrip('/')}\0{credential}".encode()).hexdigest()


def launch_fingerprint(kind: str, target_id: int, env_id: Optional[int], serials: list) -> str:
    return json.dumps(
        [kind, target_id, env_id, sorted(set(serials), key=lambda value: value or "")],
        ensure_ascii=False, separators=(",", ":"),
    )


def launch_ids(scope: str, request_id: Optional[str]) -> Tuple[str, Callable[[str], str]]:
    batch = uuid.uuid5(uuid.NAMESPACE_URL, f"{ENGINE_INSTANCE_ID}:{scope}:{request_id}") if request_id else uuid.uuid4()
    return str(batch), lambda serial: str(uuid.uuid5(batch, serial))


class LaunchRequestRegistry:
    """Owned by one FastAPI app/event loop; entries are never evicted mid-process."""

    def __init__(self) -> None:
        self._requests: Dict[Tuple[str, str], Tuple[str, asyncio.Task]] = {}

    async def submit(
        self,
        scope: str,
        request_id: str,
        fingerprint: str,
        launch: Callable[[], Awaitable[JSONResponse]],
    ) -> JSONResponse:
        key = (scope, request_id)
        entry = self._requests.get(key)
        if entry is not None and entry[0] != fingerprint:
            return JSONResponse(status_code=409, content={"detail": {
                "code": "L1001_IDEMPOTENCY_CONFLICT",
                "message": "该启动请求已使用其他参数，请确认原执行结果后重新操作",
            }})
        if entry is None:
            # No await between lookup and registration: a second request observes
            # this task even before cloud I/O or any local worker starts.
            async def run() -> JSONResponse:
                try:
                    return await launch()
                except Exception:
                    logger.exception("启动流程异常，保留请求登记以防止重复执行")
                    return JSONResponse(status_code=500, content={"detail": {
                        "code": "L1004_LAUNCH_RESULT_UNKNOWN",
                        "message": "启动结果待确认，请查看执行记录，勿重复启动",
                    }})

            task = asyncio.create_task(run(), name="autodroid-launch")
            self._requests[key] = (fingerprint, task)

            def discard_unsubmitted(completed: asyncio.Task) -> None:
                if completed.cancelled():
                    return
                response = completed.result()
                if response.status_code < 400:
                    return
                try:
                    detail = json.loads(response.body).get("detail")
                except (ValueError, AttributeError):
                    return
                if isinstance(detail, dict) and detail.get("code") == "L1004_LAUNCH_RESULT_UNKNOWN":
                    return
                # A data-read or precheck rejection has not submitted a device.
                # Allow the same key to retry after transient upstream failure.
                if self._requests.get(key, (None, None))[1] is completed:
                    self._requests.pop(key, None)

            task.add_done_callback(discard_unsubmitted)
        else:
            task = entry[1]
        return await asyncio.shield(task)

    async def close(self, grace_period: float = 0.0) -> None:
        tasks = [task for _, task in self._requests.values() if not task.done()]
        if tasks and grace_period > 0:
            # Finish accepted submissions before closing the shared HTTP pool.
            # The app supplies a grace period; unit-test/failed-start cleanup can
            # still request immediate cancellation.
            _, pending = await asyncio.wait(tasks, timeout=grace_period)
            tasks = list(pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._requests.clear()
