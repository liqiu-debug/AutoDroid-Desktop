"""CloudBridge 回源与 client-runs 载荷契约测试。

R1（blocker）：ensure_template_local 回源 URL 不得出现双 static/ 段
（image_path="static/images/x.png" → /api/static/images/x.png 或 /static/images/x.png），
对真实 StaticFiles 挂载（backend/main.py 同款 mount）验证回源命中。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from engine.core.cloud_bridge import CloudBridge


class _StaticServer:
    """真实 StaticFiles 双挂载（/api/static + /static → STATIC_DIR），记录请求路径。"""

    def __init__(self, static_dir: Path) -> None:
        import uvicorn

        self.paths: list = []
        server_self = self

        app = FastAPI()

        @app.middleware("http")
        async def _record(request, call_next):
            server_self.paths.append(request.url.path)
            return await call_next(request)

        app.mount("/api/static", StaticFiles(directory=str(static_dir)), name="api_static")
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                break
            time.sleep(0.05)
        assert self._server.started, "StaticFiles 测试服务未启动"

    @property
    def base_url(self) -> str:
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture()
def static_server(tmp_path):
    static_dir = tmp_path / "static"
    (static_dir / "images").mkdir(parents=True)
    (static_dir / "images" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"img")
    server = _StaticServer(static_dir)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_ensure_template_local_fetches_real_static_mount(static_server, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    from engine import config_store

    config_store.reset_for_tests()
    try:
        bridge = CloudBridge(upstream=static_server.base_url)
        local_path = bridge.ensure_template_local("static/images/x.png")
        assert Path(local_path).is_file()
        assert Path(local_path).read_bytes().startswith(b"\x89PNG")
        # 命中真实挂载：/api/static/images/x.png（rel 自带 static/ 前缀，不再双拼）
        assert "/api/static/images/x.png" in static_server.paths
        # R1 回归：不得出现 /api/static/static/... 双 static/ 段
        assert not [p for p in static_server.paths if "/static/static/" in p]
    finally:
        config_store.reset_for_tests()


def test_ensure_template_local_legacy_rel_without_static_prefix(static_server, tmp_path, monkeypatch):
    """S8：历史数据 image_path 无 static/ 前缀（images/x.png）→ 补 /api/static/<rel> 候选。"""
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    from engine import config_store

    config_store.reset_for_tests()
    try:
        bridge = CloudBridge(upstream=static_server.base_url)
        local_path = bridge.ensure_template_local("images/x.png")
        assert Path(local_path).is_file()
        assert Path(local_path).read_bytes().startswith(b"\x89PNG")
        assert "/api/static/images/x.png" in static_server.paths
        assert not [p for p in static_server.paths if "/static/static/" in p]
    finally:
        config_store.reset_for_tests()


def test_ensure_template_local_falls_back_to_slash_prefix(tmp_path, monkeypatch):
    """首选拦截 /api/<rel> 404 时回退 /<rel>（/static/<rel>），同样不双拼 static/。"""
    import time as _time

    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    static_dir = tmp_path / "static"
    (static_dir / "images").mkdir(parents=True)
    (static_dir / "images" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"img")
    paths = []

    app = FastAPI()

    @app.middleware("http")
    async def _record(request, call_next):
        paths.append(request.url.path)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        _time.sleep(0.05)
    try:
        port = server.servers[0].sockets[0].getsockname()[1]
        monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
        from engine import config_store

        config_store.reset_for_tests()
        try:
            bridge = CloudBridge(upstream=f"http://127.0.0.1:{port}")
            local_path = bridge.ensure_template_local("static/images/x.png")
            assert Path(local_path).is_file()
            assert "/static/images/x.png" in paths
            assert not [p for p in paths if "/static/static/" in p]
        finally:
            config_store.reset_for_tests()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_ensure_template_local_missing_file_raises(static_server, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    from engine import config_store

    config_store.reset_for_tests()
    try:
        bridge = CloudBridge(upstream=static_server.base_url)
        with pytest.raises(FileNotFoundError):
            bridge.ensure_template_local("static/images/nope.png")
    finally:
        config_store.reset_for_tests()


class _RawRecordServer:
    """裸 HTTP 接收端：记录请求行/头/原始 body（引擎只发 multipart，不收，故不依赖 python-multipart）。"""

    def __init__(self) -> None:
        import http.server
        import json

        self.received: list = []
        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 协议名
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                server_self.received.append(
                    {
                        "path": self.path,
                        "content_type": self.headers.get("content-type") or "",
                        "body": body,
                    }
                )
                payload = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # 静音测试输出
                return

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=10)


def test_upload_client_fastbot_artifact_sends_multipart_path_and_bytes(tmp_path):
    """回放 mp4 以 multipart 上传，path 走表单字段（对齐平台 client_fastbot 端点）。"""
    server = _RawRecordServer()
    server.start()
    try:
        replay_bytes = b"\x00\x00\x00\x18ftypmp42-replay-bytes"
        replay = tmp_path / "crash_101010_01.mp4"
        replay.write_bytes(replay_bytes)
        bridge = CloudBridge(upstream=server.base_url)
        result = bridge.upload_client_fastbot_artifact(
            9, str(replay), "replays/crash_101010_01.mp4"
        )
        assert result == {"ok": True}

        assert len(server.received) == 1
        request = server.received[0]
        assert request["path"] == "/api/client-fastbot/9/artifacts"
        assert request["content_type"].startswith("multipart/form-data; boundary=")
        body = request["body"]
        assert b'name="path"' in body and b"replays/crash_101010_01.mp4" in body
        assert b'name="file"; filename="crash_101010_01.mp4"' in body
        assert b"application/octet-stream" in body
        assert replay_bytes in body
    finally:
        server.stop()
