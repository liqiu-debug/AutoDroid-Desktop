"""反代/同步辅助函数单元测试。"""
from engine.app import _count_online, _list_devices_payload
from engine.proxy import build_target_url, filter_headers, HOP_BY_HOP
from engine.ws_proxy import ws_target_url


def test_build_target_url_keeps_prefix_and_query():
    assert build_target_url("https://host/pfx", "/api/devices/", "a=1") == "https://host/pfx/api/devices/?a=1"
    assert build_target_url("https://host/", "api/x") == "https://host/api/x"


def test_ws_target_url_schemes():
    assert ws_target_url("https://host/pfx", "/ws/stream", "t=1") == "wss://host/pfx/ws/stream?t=1"
    assert ws_target_url("http://127.0.0.1:8000", "/ws/stream") == "ws://127.0.0.1:8000/ws/stream"


def test_filter_headers_strips_hop_by_hop_and_keeps_auth():
    headers = [
        ("Connection", "keep-alive"),
        ("Authorization", "Bearer t"),
        ("Transfer-Encoding", "chunked"),
        ("X-Custom", "1"),
    ]
    kept = filter_headers(headers, HOP_BY_HOP)
    assert ("Authorization", "Bearer t") in kept
    assert ("X-Custom", "1") in kept
    assert not any(k.lower() in HOP_BY_HOP for k, _ in kept)


def test_count_online_matches_backend_semantics():
    devices = [
        {"status": "IDLE"},
        {"status": "OFFLINE"},
        {"status": "OFFLINE", "app_presence": {"status": "ONLINE"}},
        {"status": "BUSY"},
    ]
    assert _count_online(devices) == 3


def test_list_payload_accepts_array_and_wrapped():
    assert _list_devices_payload([{"id": 1}]) == [{"id": 1}]
    assert _list_devices_payload({"devices": [{"id": 2}], "total": 1}) == [{"id": 2}]
    assert _list_devices_payload({"detail": "err"}) is None
    assert _list_devices_payload("nope") is None


# ==================== 反代超时分级（C5） ==================== #


def test_pick_request_timeout_normal_api_short_read():
    from engine.proxy import NORMAL_TIMEOUT, pick_request_timeout

    assert pick_request_timeout("GET", "/api/cases/") is NORMAL_TIMEOUT
    assert pick_request_timeout("POST", "/api/cases/", content_length=100) is NORMAL_TIMEOUT
    assert pick_request_timeout("GET", "/api/devices/") is NORMAL_TIMEOUT
    # pro-file 等子串不得误命中长超时特征
    assert pick_request_timeout("GET", "/api/user/profile") is NORMAL_TIMEOUT
    assert pick_request_timeout("GET", "/api/reports/123") is NORMAL_TIMEOUT


def test_pick_request_timeout_long_for_stream_upload_download():
    from engine.proxy import LONG_TIMEOUT, pick_request_timeout

    # 流式
    assert pick_request_timeout("GET", "/api/stream/ios-mjpeg/USB1") is LONG_TIMEOUT
    assert pick_request_timeout("GET", "/api/stream/devices") is LONG_TIMEOUT
    # SSE
    assert pick_request_timeout("GET", "/api/events", accept="text/event-stream") is LONG_TIMEOUT
    # 上传 / 导入导出 / 下载 / 录制
    assert pick_request_timeout("POST", "/api/templates/upload", content_length=10) is LONG_TIMEOUT
    assert pick_request_timeout("POST", "/api/cases/import", content_length=10) is LONG_TIMEOUT
    assert pick_request_timeout("GET", "/api/reports/1/export") is LONG_TIMEOUT
    assert pick_request_timeout("GET", "/api/packages/1/download") is LONG_TIMEOUT
    # 大包上传（超过 1MiB）
    assert pick_request_timeout("POST", "/api/anything", content_length=2 * 1024 * 1024) is LONG_TIMEOUT
    # chunked / 未知大小上传
    assert pick_request_timeout("POST", "/api/anything", content_length=None) is LONG_TIMEOUT


# ==================== 客户端断连取消上游（C5） ==================== #


class _FakeURL:
    def __init__(self, path: str, query: str = ""):
        self.path = path
        self.query = query


class _FakeRequest:
    """proxy_request 所需请求面的最小替身。"""

    def __init__(self, method="GET", path="/api/x", query="", headers=None, body=b"", disconnect_after=None):
        self.method = method
        self.url = _FakeURL(path, query)
        self.headers = dict(headers or {})
        self._body = body
        # is_disconnected 第 N 次调用起返回 True；None = 从不断连
        self._disconnect_after = disconnect_after
        self._checks = 0

    async def body(self):
        return self._body

    async def is_disconnected(self):
        self._checks += 1
        if self._disconnect_after is None:
            return False
        return self._checks >= self._disconnect_after

    async def stream(self):
        if self._body:
            yield self._body


class _ScriptedUpstream:
    """127.0.0.1 mock upstream：/slow 延迟后回包；/chunked 分片慢吐。"""

    def __init__(self, slow_delay=0.0, chunks=0, chunk_delay=0.0):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        self.slow_delay = slow_delay
        self.chunks = chunks
        self.chunk_delay = chunk_delay
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):  # noqa: N802
                import time as _time

                if self.path.startswith("/slow"):
                    _time.sleep(owner.slow_delay)
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/chunked"):
                    total = owner.chunks * 4
                    self.send_response(200)
                    self.send_header("content-type", "application/octet-stream")
                    self.send_header("content-length", str(total))
                    self.end_headers()
                    for i in range(owner.chunks):
                        self.wfile.write(bytes([i]) * 4)
                        self.wfile.flush()
                        _time.sleep(owner.chunk_delay)
                    return
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def test_proxy_request_cancels_upstream_when_client_disconnects_mid_send():
    """客户端在上游响应前断连：立即取消上游请求（499），不等慢上游。"""
    import asyncio
    import time

    import httpx

    from engine.proxy import proxy_request

    async def _run():
        upstream = _ScriptedUpstream(slow_delay=2.0)
        upstream.start()
        try:
            async with httpx.AsyncClient() as http:
                req = _FakeRequest(path="/slow", disconnect_after=1)
                started = time.monotonic()
                resp = await proxy_request(http, req, upstream.base_url)
                elapsed = time.monotonic() - started
                assert resp.status_code == 499
                assert elapsed < 1.5, f"未及时取消上游: {elapsed:.2f}s"
        finally:
            upstream.stop()

    asyncio.run(_run())


def test_proxy_request_stops_reading_upstream_body_on_disconnect():
    """响应流中途断连：停止读上游并收尾，不再把整包读完。"""
    import asyncio

    import httpx

    from engine.proxy import proxy_request

    async def _run():
        upstream = _ScriptedUpstream(chunks=20, chunk_delay=0.05)
        upstream.start()
        try:
            async with httpx.AsyncClient() as http:
                req = _FakeRequest(path="/chunked", disconnect_after=3)
                resp = await proxy_request(http, req, upstream.base_url)
                assert resp.status_code == 200
                got = []
                async for chunk in resp.body_iterator:
                    got.append(chunk)
                assert 0 < len(got) < 20, f"断连后仍读完整包: {len(got)}"
        finally:
            upstream.stop()

    asyncio.run(_run())


def test_proxy_request_passes_tiered_timeout_to_httpx():
    """proxy_request 真实把分级超时写进上游请求（普通=短、流式=长）。"""
    import asyncio

    import httpx

    from engine.proxy import LONG_TIMEOUT, NORMAL_TIMEOUT, proxy_request

    async def _run():
        upstream = _ScriptedUpstream()
        upstream.start()
        try:
            async with httpx.AsyncClient() as http:
                captured = []
                real_build = http.build_request

                def spy_build(*args, **kwargs):
                    captured.append(kwargs.get("timeout"))
                    return real_build(*args, **kwargs)

                http.build_request = spy_build
                await proxy_request(http, _FakeRequest(path="/api/cases/"), upstream.base_url)
                await proxy_request(http, _FakeRequest(path="/api/stream/devices"), upstream.base_url)
                assert captured[0] is NORMAL_TIMEOUT
                assert captured[1] is LONG_TIMEOUT
        finally:
            upstream.stop()

    asyncio.run(_run())
