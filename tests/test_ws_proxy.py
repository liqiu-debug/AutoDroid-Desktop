"""WebSocket 反向代理往返测试（本机 echo upstream）。"""
from __future__ import annotations

import asyncio
import inspect
import threading

import pytest
from fastapi.testclient import TestClient

from engine import config_store
from engine.app import create_app
from engine.ws_proxy import ws_target_url


class _WsEchoServer:
    """独立线程事件循环上的 echo 服务，充当 upstream 的 /ws/echo。"""

    def __init__(self) -> None:
        import websockets

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        async def handler(ws, *args):
            async for message in ws:
                await ws.send(message)

        async def starter():
            return await websockets.serve(handler, "127.0.0.1", 0)

        self._server = asyncio.run_coroutine_threadsafe(starter(), self._loop).result()
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        async def shutdown():
            result = self._server.close()
            if inspect.isawaitable(result):
                await result
            wait_closed = getattr(self._server, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()

        asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture()
def ws_echo():
    server = _WsEchoServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    config_store.reset_for_tests()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    config_store.reset_for_tests()


def test_ws_target_url_from_http_upstream():
    assert ws_target_url("http://127.0.0.1:9", "/ws/echo") == "ws://127.0.0.1:9/ws/echo"


def test_ws_round_trip(client, ws_echo):
    config_store.set_upstream(ws_echo.base_url)
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("hello-p0")
        assert ws.receive_text() == "hello-p0"
        ws.send_bytes(b"\x00\x01\x02frame")
        assert ws.receive_bytes() == b"\x00\x01\x02frame"


def test_ws_api_path_upgrades_are_proxied(client, ws_echo):
    """/api/** 的 WebSocket 升级默认走 ws 反代（本地拦截的场景执行 WS 除外）。"""
    config_store.set_upstream(ws_echo.base_url)
    with client.websocket_connect("/api/other/ws/echo") as ws:
        ws.send_text("hello-api-ws")
        assert ws.receive_text() == "hello-api-ws"


def test_ws_scenario_run_is_local_not_proxied(client, ws_echo):
    """P2：/api/scenarios/ws/run/{id} 已本地拦截（不再反代云端执行）。"""
    config_store.set_upstream(ws_echo.base_url)
    with client.websocket_connect("/api/scenarios/ws/run/1") as ws:
        message = ws.receive_json()
        # 无设备参数 → 本地协议的错误收尾（而非 echo 反代）
        assert message["type"] in ("log", "run_complete")
        assert "echo" not in str(message)


def test_ws_without_upstream_is_rejected(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/echo") as ws:
            ws.receive_text()
