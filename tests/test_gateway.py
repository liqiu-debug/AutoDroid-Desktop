"""引擎网关集成测试：mock upstream + TestClient。"""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import LOCAL_PRESENCE_MAX_AGE_SECONDS, create_app


class _MockUpstream:
    """极简平台侧 mock：auth/token、client-devices/register、devices 列表。"""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.devices: List[Dict[str, Any]] = [
            {"id": 1, "serial": "MOCK1", "status": "IDLE", "platform": "android"},
            {"id": 2, "serial": "MOCK2", "status": "OFFLINE", "platform": "android"},
        ]
        self.register_status = 200
        self.list_status = 200
        self.package_bytes = bytes(range(256)) * 512  # 128 KiB 安装包
        self.last_gzip_length = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: A003 - 静音
                return

            def _read_body(self) -> bytes:
                length = int(self.headers.get("content-length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - http.server 命名
                body = self._read_body()
                owner.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body.decode("utf-8", "replace"),
                    }
                )
                if self.path.endswith("/auth/token"):
                    self._send(200, {"access_token": "tok", "token_type": "bearer"})
                    return
                if self.path.endswith("/api/client-devices/register"):
                    self._send(owner.register_status, {"id": 9, "serial": "X", "status": "IDLE"})
                    return
                if self.path.endswith("/api/client-devices/heartbeat"):
                    self._send(200, {"updated": 1})
                    return
                if self.path.endswith("/api/client-devices/logout"):
                    self._send(200, {"updated": 1})
                    return
                self._send(404, {"detail": "Not found"})

            def do_GET(self):  # noqa: N802 - http.server 命名
                owner.requests.append(
                    {"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
                )
                if self.path.startswith("/api/gzip-payload"):
                    payload = json.dumps({"hello": "gzipped", "items": [1, 2, 3]}).encode("utf-8")
                    body = gzip.compress(payload)
                    owner.last_gzip_length = len(body)
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-encoding", "gzip")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/fastbot/devices"):
                    owner.requests.append({"path": self.path, "body": None, "headers": dict(self.headers)})
                    self._send(200, owner.devices)
                    return
                if self.path.startswith("/api/packages/7/download"):
                    # 安装包下载：与云端 FileResponse 一致，带 content-length，支持 Range 续传
                    body = owner.package_bytes
                    total = len(body)
                    match = re.match(r"bytes=(\d+)-", self.headers.get("range") or "")
                    if match:
                        start = int(match.group(1))
                        chunk = body[start:]
                        self.send_response(206)
                        self.send_header("content-type", "application/octet-stream")
                        self.send_header("content-range", f"bytes {start}-{total - 1}/{total}")
                        self.send_header("content-length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                        return
                    self.send_response(200)
                    self.send_header("content-type", "application/octet-stream")
                    self.send_header("accept-ranges", "bytes")
                    self.send_header("content-length", str(total))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/devices"):
                    if owner.list_status != 200:
                        self._send(owner.list_status, {"detail": "Not authenticated"})
                        return
                    self._send(200, owner.devices)
                    return
                self._send(404, {"detail": "Not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def mock_upstream():
    server = _MockUpstream()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    # 本机 adb 在场清单（列表覆盖 / 心跳）默认空集：测试不拉起真实 adb
    monkeypatch.setattr("engine.app.local_serials", lambda *args, **kwargs: set())
    monkeypatch.setattr("engine.adb_scan.local_serials", lambda *args, **kwargs: set())
    config_store.reset_for_tests()
    presence.reset_for_tests()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    presence.reset_for_tests()
    config_store.reset_for_tests()


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询等待后台任务落地（发现登记 / 补心跳 / 在场缓存刷新都是后台任务）。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _warm_local_presence(test_client: TestClient, serials) -> None:
    """直接预热本机在场缓存（绕开后台刷新的时序）。"""
    import time

    now = time.monotonic()
    test_client.app.state.local_presence.update(
        {"ts": now, "attempt_ts": now, "serials": set(serials)}
    )


def test_client_info(client):
    resp = client.get("/api/client/info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["clientMode"] is True
    assert data["version"] == "9.9.9-test"
    assert data["platform"] in ("darwin", "win32", "linux")


def test_auth_token_rejects_invalid_server(client):
    resp = client.post("/api/auth/token", data={"username": "u", "password": "p"}, headers={"X-AutoDroid-Server": "example.com"})
    assert resp.status_code == 400
    assert "请输入有效的平台地址" in resp.json()["detail"]


def test_auth_token_sets_upstream_and_strips_header(client, mock_upstream):
    resp = client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url + "/"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "tok"
    # 归一化：去尾部 /
    assert config_store.get_upstream() == mock_upstream.base_url
    config_file = json.loads(config_store.config_path().read_text("utf-8"))
    assert config_file["server"] == mock_upstream.base_url
    # 请求头已剥离，form 原样转发
    auth_calls = [r for r in mock_upstream.requests if r["path"].endswith("/auth/token")]
    assert auth_calls and auth_calls[0]["path"] == "/auth/token"
    assert "x-autodroid-server" not in auth_calls[0]["headers"]
    assert auth_calls[0]["headers"].get("authorization") is None
    assert "username=u" in auth_calls[0]["body"]


def test_auth_token_upstream_failure_passthrough(client):
    # 用假 http 客户端确定性地触发连接失败（沙箱内真实 TCP 拒绝可能被透明代理改写）
    import httpx

    class _FailingHttp:
        def build_request(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

        def get(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

        async def aclose(self):
            return None

    client.app.state.http = _FailingHttp()
    resp = client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": "http://127.0.0.1:59999"},
    )
    assert resp.status_code == 502
    assert "上游服务器连接失败" in resp.json()["detail"]


def test_auth_token_requires_header_before_upstream_known(client):
    resp = client.post("/api/auth/token", data={"username": "u", "password": "p"})
    assert resp.status_code == 400
    assert "X-AutoDroid-Server" in resp.json()["detail"]


def test_devices_sync_registers_and_returns_authoritative_list(client, mock_upstream, monkeypatch):
    fake_devices = [
        {
            "platform": "android",
            "serial": "USB1",
            "model": "Pixel 8",
            "brand": "GOOGLE",
            "android_version": "14",
            "os_version": "14",
            "resolution": "1080x2400",
            "connection_type": "usb",
        }
    ]
    monkeypatch.setattr("engine.app.scan_devices", lambda: fake_devices)
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.post("/api/devices/sync", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    payload = resp.json()
    # 响应包 = sync 端点形状，devices 来自权威列表端点
    assert payload["synced"] == 1
    assert payload["online"] == 1
    assert payload["offline"] == 1
    assert [d["serial"] for d in payload["devices"]] == ["MOCK1", "MOCK2"]

    registers = [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/register")]
    assert len(registers) == 1
    body = json.loads(registers[0]["body"])
    assert body["serial"] == "USB1"
    assert body["client_instance_id"]
    assert registers[0]["headers"].get("authorization") == "Bearer tok"
    lists = [r for r in mock_upstream.requests if r["path"].startswith("/api/devices")]
    assert lists and lists[0]["headers"].get("authorization") == "Bearer tok"


def test_devices_sync_conflict_still_returns_list(client, mock_upstream, monkeypatch):
    mock_upstream.register_status = 409
    monkeypatch.setattr(
        "engine.app.scan_devices",
        lambda: [
            {
                "platform": "android",
                "serial": "OTHER",
                "model": "M",
                "brand": "B",
                "android_version": "13",
                "os_version": "13",
                "resolution": "1x1",
            }
        ],
    )
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.post("/api/devices/sync", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    assert resp.json()["synced"] == 0
    assert len(resp.json()["devices"]) == 2


def test_devices_sync_without_upstream(client):
    resp = client.post("/api/devices/sync")
    assert resp.status_code == 502
    assert "未配置上游服务器" in resp.json()["detail"]


def test_generic_api_is_proxied(client, mock_upstream):
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    assert resp.json()[0]["serial"] == "MOCK1"


def test_ws_plain_http_returns_501(client):
    resp = client.get("/ws/runs/1")
    assert resp.status_code == 501


def test_static_and_spa_fallback(client, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("AUTODROID_FRONTEND_DIST", str(dist))

    root = client.get("/")
    assert root.status_code == 200
    assert root.text == "<html>app</html>"

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"

    spa = client.get("/devices/center")
    assert spa.status_code == 200
    assert spa.text == "<html>app</html>"


def test_no_secret_persisted(client, mock_upstream, tmp_path):
    client.post(
        "/api/auth/token",
        data={"username": "admin", "password": "super-secret"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    data_dir = config_store.config_path().parent
    assert config_store.config_path().is_file()
    for path in data_dir.rglob("*"):
        if path.is_file():
            text = path.read_text("utf-8", errors="replace")
            assert "super-secret" not in text
            assert "access_token" not in text


def test_gzip_content_encoding_passthrough(client, mock_upstream):
    """上游 gzip 响应原样透传：Content-Encoding 保留、body 仍可按 gzip 解出。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.get("/api/gzip-payload", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    # content-encoding 原样保留（引擎不解压后丢头）
    assert resp.headers.get("content-encoding") == "gzip"
    # 原样字节转发：content-length 仍是上游声明的压缩后长度
    assert resp.headers.get("content-length") == str(mock_upstream.last_gzip_length)
    # 响应体仍是 gzip 载荷（httpx 客户端透明解码后可读回 JSON）
    assert resp.json() == {"hello": "gzipped", "items": [1, 2, 3]}
    # 请求头的 Accept-Encoding 原样透传给上游（未被引擎改写）
    calls = [r for r in mock_upstream.requests if r["path"].startswith("/api/gzip-payload")]
    assert calls and calls[0]["headers"].get("accept-encoding") == "gzip"


def test_package_download_keeps_content_length_for_progress(client, mock_upstream):
    """安装包下载经引擎反代时保留 content-length：壳内更新下载器靠它算进度（否则恒为 0%）。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.get("/api/packages/7/download?token=tok")
    assert resp.status_code == 200
    total = len(mock_upstream.package_bytes)
    assert resp.headers.get("content-length") == str(total)
    assert "chunked" not in (resp.headers.get("transfer-encoding") or "")
    assert resp.content == mock_upstream.package_bytes


def test_package_download_range_resume_keeps_range_headers(client, mock_upstream):
    """断点续传：206 的 content-range / content-length 原样透传。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    total = len(mock_upstream.package_bytes)
    resp = client.get("/api/packages/7/download?token=tok", headers={"Range": "bytes=1000-"})
    assert resp.status_code == 206
    assert resp.headers.get("content-range") == f"bytes 1000-{total - 1}/{total}"
    assert resp.headers.get("content-length") == str(total - 1000)
    assert resp.content == mock_upstream.package_bytes[1000:]


def test_server_header_stripped_on_generic_proxy(client, mock_upstream):
    """x-autodroid-server 是本地引擎专有头，任意请求都不得透传给上游。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.get(
        "/api/devices/",
        headers={"Authorization": "Bearer tok", "X-AutoDroid-Server": "http://evil.example.com"},
    )
    assert resp.status_code == 200
    lists = [r for r in mock_upstream.requests if r["path"].startswith("/api/devices")]
    assert lists
    assert "x-autodroid-server" not in lists[-1]["headers"]
    assert lists[-1]["headers"].get("authorization") == "Bearer tok"


def test_login_captures_token_and_logout_on_shutdown(tmp_path, monkeypatch, mock_upstream):
    """登录响应中的 access_token 只驻留内存；进程退出（lifespan shutdown）调 logout。"""
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    config_store.reset_for_tests()
    presence.reset_for_tests()
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/token",
            data={"username": "u", "password": "p"},
            headers={"X-AutoDroid-Server": mock_upstream.base_url},
        )
        assert resp.status_code == 200
        assert presence.get_token() == "tok"
        # token 不落盘：config.json 只允许 {server, client_instance_id}
        config = json.loads(config_store.config_path().read_text("utf-8"))
        assert set(config.keys()) <= {"server", "client_instance_id"}
        assert config.get("server") == mock_upstream.base_url
    # 退出 with 块 = lifespan shutdown → logout_once 被调用，且只置本实例登记的设备
    logouts = [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/logout")]
    assert logouts, "退出时应调用 /api/client-devices/logout"
    assert logouts[-1]["headers"].get("authorization") == "Bearer tok"
    assert json.loads(logouts[-1]["body"]) == {"client_instance_id": config_store.get_client_instance_id()}
    presence.reset_for_tests()
    config_store.reset_for_tests()


def test_heartbeat_loop_reports_online_serials(client, mock_upstream, monkeypatch):
    """登录成功后心跳循环按周期上报本机在线 serials（带 Bearer token）。

    心跳只走轻量 `adb devices`（local_serials），不做属性扫描；serial 排序上报。
    """
    import time

    monkeypatch.setenv("AUTODROID_HEARTBEAT_INTERVAL", "0.05")
    monkeypatch.setattr("engine.adb_scan.local_serials", lambda: {"USB2", "USB1"})
    scans = []
    monkeypatch.setattr("engine.adb_scan.scan_devices", lambda: scans.append(1) or [])
    resp = client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    assert resp.status_code == 200
    deadline = time.time() + 5.0
    beats = []
    while time.time() < deadline:
        beats = [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/heartbeat")]
        if beats:
            break
        time.sleep(0.05)
    assert beats, "心跳循环应至少上报一次"
    body = json.loads(beats[-1]["body"])
    assert body["serials"] == ["USB1", "USB2"]
    assert beats[-1]["headers"].get("authorization") == "Bearer tok"
    assert scans == [], "心跳不得再做全量属性扫描"


def test_heartbeat_skipped_without_token(client, mock_upstream):
    """未登录（无 token）时心跳链路静默跳过，不发请求。"""
    import asyncio

    import httpx

    async def run():
        async with httpx.AsyncClient() as http:
            return await presence.heartbeat_once(http, serials=["USB1"])

    assert presence.get_token() is None
    assert asyncio.run(run()) is None
    beats = [r for r in mock_upstream.requests if "heartbeat" in r["path"]]
    assert not beats


def test_heartbeat_once_posts_serials(client, mock_upstream):
    """heartbeat_once 带 Bearer token 上报 serials，返回 updated 计数。"""
    import asyncio

    import httpx

    config_store.set_upstream(mock_upstream.base_url)
    presence.set_token("tok")

    async def run():
        async with httpx.AsyncClient() as http:
            return await presence.heartbeat_once(http, serials=["USB1", "USB2"])

    try:
        assert asyncio.run(run()) == 1
    finally:
        presence.clear_token()
    beats = [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/heartbeat")]
    assert beats
    body = json.loads(beats[-1]["body"])
    assert body["serials"] == ["USB1", "USB2"]
    assert beats[-1]["headers"].get("authorization") == "Bearer tok"


def test_devices_list_registers_new_local_devices_throttled(client, mock_upstream, monkeypatch):
    """GET /api/devices/ 节流自愈：本机新插入的设备在后台自动登记，列表照常回源。

    消除「必须先去设备中心点一键同步，用例编辑器才看得到设备」的体验断点；
    登记跑在后台任务里（不让 2s 级轮询等一轮 adb 扫描），重复轮询不得重复上报
    （节流 + 已登记集合去重）。
    """
    fake_devices = [
        {
            "platform": "android",
            "serial": "USB9",
            "model": "Pixel 9",
            "brand": "GOOGLE",
            "android_version": "15",
            "os_version": "15",
            "resolution": "1080x2400",
            "connection_type": "usb",
        }
    ]
    monkeypatch.setattr("engine.app.scan_devices", lambda: fake_devices)
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )

    resp1 = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    resp2 = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert [d["serial"] for d in resp1.json()] == ["MOCK1", "MOCK2"]

    def registers():
        return [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/register")]

    assert _wait_until(lambda: len(registers()) >= 1), "后台发现轮次应完成一次登记上报"
    task = client.app.state.device_discovery["task"]
    assert _wait_until(lambda: task is not None and task.done())
    assert len(registers()) == 1, "重复轮询不得重复上报"
    body = json.loads(registers()[0]["body"])
    assert body["serial"] == "USB9"
    assert body["client_instance_id"]
    assert body["serial"] in client.app.state.device_discovery["serials"]

    lists = [r for r in mock_upstream.requests if r["path"].startswith("/api/devices")]
    assert len(lists) == 2, "两次 GET 都应照常回源云端权威列表"
    assert lists[-1]["headers"].get("authorization") == "Bearer tok"


def test_devices_list_discovery_skips_when_round_in_flight(client, mock_upstream, monkeypatch):
    """有在途发现轮次时（轮次锁被占）本轮直接跳过：不叠轮，列表照常透传。"""
    scans = []

    def fake_scan():
        scans.append(1)
        return []

    monkeypatch.setattr("engine.app.scan_devices", fake_scan)
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    # 强制过节流窗口 + 人为占住轮次锁（模拟慢轮在途）
    client.app.state.device_discovery["ts"] = 0.0
    lock = client.app.state.device_discovery_lock
    lock._locked = True
    try:
        resp = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    finally:
        lock._locked = False

    assert resp.status_code == 200
    assert [d["serial"] for d in resp.json()] == ["MOCK1", "MOCK2"]
    assert scans == [], "在途轮次期间不得再叠一轮扫描"


def test_devices_list_discovery_rounds_do_not_overlap(client, mock_upstream, monkeypatch):
    """慢轮跑过节流窗口后并发 GET：轮次锁保证同一时刻至多一轮扫描/上报。"""
    import threading
    import time

    counter = {"active": 0, "max": 0, "calls": 0}
    counter_lock = threading.Lock()

    def slow_scan():
        with counter_lock:
            counter["calls"] += 1
            counter["active"] += 1
            counter["max"] = max(counter["max"], counter["active"])
        # 模拟慢轮：跑过 10s 节流窗口（让并发请求也想触发新一轮）
        client.app.state.device_discovery["ts"] = 0.0
        time.sleep(0.4)
        with counter_lock:
            counter["active"] -= 1
        return []

    monkeypatch.setattr("engine.app.scan_devices", slow_scan)
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )

    results = []
    errors = []

    def hit():
        try:
            results.append(client.get("/api/devices/", headers={"Authorization": "Bearer tok"}))
        except Exception as exc:  # pragma: no cover - 测试失败时暴露
            errors.append(exc)

    first = threading.Thread(target=hit)
    first.start()
    time.sleep(0.1)  # 让第一轮进入 slow_scan
    second = threading.Thread(target=hit)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors, errors
    assert len(results) == 2 and all(r.status_code == 200 for r in results)
    # 发现轮次在后台：等它跑完再断言（避免与 lifespan 关闭竞争）
    assert _wait_until(lambda: counter["calls"] >= 1 and counter["active"] == 0)
    task = client.app.state.device_discovery["task"]
    assert _wait_until(lambda: task is not None and task.done())
    assert counter["max"] == 1, f"发现轮次叠跑了: max={counter['max']}"


def test_overlay_local_presence_only_flips_offline_local_devices():
    """本机在场覆盖：只把本机 adb 在场且云端判 OFFLINE 的设备翻回 IDLE。"""
    from engine.app import overlay_local_presence

    devices = [
        {"serial": "A", "status": "OFFLINE"},  # 本机在场 → IDLE
        {"serial": "B", "status": "BUSY"},  # 本机在场但在执行 → 原样
        {"serial": "C", "status": "OFFLINE"},  # 不在本机 → 原样（真离线）
        {"serial": "D", "status": "WDA_DOWN"},  # 非 OFFLINE 状态原样
        {"status": "OFFLINE"},  # 无 serial 跳过
    ]
    revived = overlay_local_presence(devices, {"A", "B", "D"})
    assert revived == ["A"]
    assert [d.get("status") for d in devices] == ["IDLE", "BUSY", "OFFLINE", "WDA_DOWN", "OFFLINE"]

    # 本机清单未知（None）或为空：不做任何覆盖
    devices = [{"serial": "A", "status": "OFFLINE"}]
    assert overlay_local_presence(devices, None) == []
    assert overlay_local_presence(devices, set()) == []
    assert devices[0]["status"] == "OFFLINE"


def test_devices_list_overlays_local_presence_and_heals_cloud(client, mock_upstream):
    """本机插着、云端心跳超时判 OFFLINE 的设备：列表显示 IDLE，并以请求用户身份补发心跳。

    录制页据 status 拦截录制，而投屏/驱动都在本机——假离线不得再拦住用户；
    补心跳让云端 last_seen_at 回来，其他消费方（Web 端、定时任务）也随之恢复。
    """
    mock_upstream.devices.append({"id": 3, "serial": "MOCK3", "status": "BUSY", "platform": "android"})
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    _warm_local_presence(client, {"MOCK2", "MOCK3"})

    resp = client.get("/api/devices/", headers={"Authorization": "Bearer req-tok"})
    assert resp.status_code == 200
    statuses = {d["serial"]: d["status"] for d in resp.json()}
    assert statuses == {"MOCK1": "IDLE", "MOCK2": "IDLE", "MOCK3": "BUSY"}
    assert resp.json()[1]["platform"] == "android", "其余字段原样保留"

    def heals():
        return [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/heartbeat")]

    assert _wait_until(lambda: len(heals()) >= 1), "应补发一次心跳让云端自愈"
    assert json.loads(heals()[-1]["body"]) == {"serials": ["MOCK2"]}
    # 以当前请求用户身份上报（请求头的 Authorization），不依赖登录时捕获的 token
    assert heals()[-1]["headers"].get("authorization") == "Bearer req-tok"

    # 节流：10s 内重复轮询不再重复补心跳
    client.get("/api/devices/", headers={"Authorization": "Bearer req-tok"})
    client.get("/api/devices/", headers={"Authorization": "Bearer req-tok"})
    assert len(heals()) == 1


def test_devices_list_keeps_cloud_status_when_local_presence_unknown(client, mock_upstream):
    """本机在场缓存未知/过期（adb 卡住或缺失）时不覆盖：云端 OFFLINE 原样返回、不补心跳。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    # 缓存 ts=0 → 超过 LOCAL_PRESENCE_MAX_AGE_SECONDS → 视为未知
    client.app.state.local_presence.update({"ts": 0.0, "serials": {"MOCK2"}})

    resp = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    statuses = {d["serial"]: d["status"] for d in resp.json()}
    assert statuses == {"MOCK1": "IDLE", "MOCK2": "OFFLINE"}
    heals = [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/heartbeat")]
    assert not heals


def test_devices_list_warms_local_presence_in_background(client, mock_upstream, monkeypatch):
    """首轮 GET 只触发后台刷新（本次按云端状态返回），缓存就绪后下一轮即覆盖假离线。"""
    monkeypatch.setattr("engine.app.local_serials", lambda: {"MOCK2"})
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )

    first = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    assert first.status_code == 200
    assert _wait_until(lambda: client.app.state.local_presence["serials"] == {"MOCK2"})

    second = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    statuses = {d["serial"]: d["status"] for d in second.json()}
    assert statuses["MOCK2"] == "IDLE"


def test_fastbot_devices_overlay_and_heal_parity(client, mock_upstream):
    """智能稳定性 / 流畅度 / 冷热启动三页的设备列表与 /api/devices/ 同等对待。

    回归「客户端长时间静止后跑这三项显示设备离线，去设备中心刷新才恢复」：
    这三页只查 /api/fastbot/devices，该端点此前既不刷新在场缓存、也不补心跳，
    缓存一过期覆盖就失效，只剩云端心跳超时的 OFFLINE。
    """
    mock_upstream.devices = [{"id": 2, "serial": "MOCK2", "status": "OFFLINE", "platform": "android"}]
    _login(client, mock_upstream)
    _warm_local_presence(client, {"MOCK2"})

    resp = client.get("/api/fastbot/devices?include_ios=true", headers={"Authorization": "Bearer req-tok"})
    assert resp.status_code == 200
    assert {d["serial"]: d["status"] for d in resp.json()} == {"MOCK2": "IDLE"}

    def heals():
        return [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/heartbeat")]

    assert _wait_until(lambda: len(heals()) >= 1), "测试页也要补心跳让云端自愈"
    assert json.loads(heals()[-1]["body"]) == {"serials": ["MOCK2"]}
    assert heals()[-1]["headers"].get("authorization") == "Bearer req-tok", "以请求用户身份上报"


def test_fastbot_devices_schedules_presence_refresh(client, mock_upstream, monkeypatch):
    """测试页也驱动在场缓存后台刷新（与 /api/devices/ 一致）。"""
    monkeypatch.setattr("engine.app.local_serials", lambda: {"MOCK2"})
    mock_upstream.devices = [{"id": 2, "serial": "MOCK2", "status": "OFFLINE", "platform": "android"}]
    _login(client, mock_upstream)
    # 停掉常驻刷新，单独验证「请求驱动」这条路径
    client.app.state.stop_local_presence_loop()
    client.app.state.local_presence.update({"ts": 0.0, "attempt_ts": 0.0, "serials": set()})

    resp = client.get("/api/fastbot/devices?include_ios=true", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    assert _wait_until(lambda: client.app.state.local_presence["serials"] == {"MOCK2"}), "测试页应触发后台刷新"


def _self_hosted_client(tmp_path, monkeypatch, local_serials_value):
    """自建 app：让常驻刷新以极短周期跑（fixture 的 client 已经起在 10s 周期上）。"""
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setattr("engine.app.local_serials", lambda: set(local_serials_value))
    monkeypatch.setattr("engine.app.LOCAL_PRESENCE_LOOP_SECONDS", 0.02)
    config_store.reset_for_tests()
    presence.reset_for_tests()
    app = create_app()
    return TestClient(app)


def test_local_presence_loop_keeps_cache_fresh_without_requests(tmp_path, monkeypatch, mock_upstream):
    """长时间没有任何设备请求：常驻刷新仍让在场缓存不过期。

    这是「客户端放久了再跑测试报设备离线」的根治点：智能稳定性/流畅度/冷热启动
    三页只查 /api/fastbot/devices，缓存一过期覆盖就失效、只剩云端心跳超时的 OFFLINE。
    """
    with _self_hosted_client(tmp_path, monkeypatch, {"MOCK2"}) as test_client:
        test_client.post(
            "/api/auth/token",
            data={"username": "u", "password": "p"},
            headers={"X-AutoDroid-Server": mock_upstream.base_url},
        )
        assert test_client.app.state.local_presence.get("loop_task") is not None, "启动时就该起常驻刷新"
        # 全程不碰任何设备接口，缓存也要自己续上
        assert _wait_until(lambda: test_client.app.state.local_presence["serials"] == {"MOCK2"}), "无请求也应刷新"
        assert time.monotonic() - test_client.app.state.local_presence["ts"] < LOCAL_PRESENCE_MAX_AGE_SECONDS


def test_fastbot_devices_show_local_presence_after_long_idle(tmp_path, monkeypatch, mock_upstream):
    """端到端回归症状：长时间静止后打开测试页，本机插着的设备不该显示离线。"""
    mock_upstream.devices = [{"id": 2, "serial": "MOCK2", "status": "OFFLINE", "platform": "android"}]
    with _self_hosted_client(tmp_path, monkeypatch, {"MOCK2"}) as test_client:
        test_client.post(
            "/api/auth/token",
            data={"username": "u", "password": "p"},
            headers={"X-AutoDroid-Server": mock_upstream.base_url},
        )
        # 模拟「放久了」：先把缓存打成过期，且不访问设备中心
        test_client.app.state.local_presence.update({"ts": 0.0, "attempt_ts": 0.0, "serials": set()})
        assert _wait_until(lambda: test_client.app.state.local_presence["serials"] == {"MOCK2"})

        resp = test_client.get("/api/fastbot/devices?include_ios=true", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert {d["serial"]: d["status"] for d in resp.json()} == {"MOCK2": "IDLE"}, "本机在场优先于云端心跳超时"


def test_request_identity_restarts_heartbeat_after_engine_restart(client, mock_upstream):
    """引擎重启/窗口重载后内存 token 丢失：带 token 的请求就地认领并重启心跳循环。

    否则心跳永久停摆 → 云端按 last_seen 超时判离线，而前端照常带 token 请求、
    界面看不出异常——就是「放久了设备离线」。
    """
    from engine import presence

    _login(client, mock_upstream)
    assert presence.get_token() is not None
    presence.stop_heartbeat_loop()
    presence.clear_token()  # 模拟引擎重启：内存态全丢
    assert presence.get_token() is None

    resp = client.get("/api/devices/", headers={"Authorization": "Bearer req-tok"})
    assert resp.status_code == 200
    assert presence.get_token() == "req-tok", "就地认领请求身份"
    task = presence.get_heartbeat_task()
    assert task is not None and not task.done(), "心跳循环已重启"


def test_devices_list_passes_through_upstream_error_status(client, mock_upstream):
    """云端非 2xx（如会话过期 401）原样透传：前端据此走会话过期处理。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    mock_upstream.list_status = 401
    resp = client.get("/api/devices/", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


def test_devices_list_forwards_query_string(client, mock_upstream):
    """设备中心的 refresh_ios_wda 等查询参数原样回源。"""
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )
    resp = client.get("/api/devices/?refresh_ios_wda=true", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    lists = [r for r in mock_upstream.requests if r["path"].startswith("/api/devices")]
    assert lists[-1]["path"] == "/api/devices/?refresh_ios_wda=true"


def test_heartbeat_once_uses_light_local_serials(client, mock_upstream, monkeypatch):
    """heartbeat_once 缺省 serials 走轻量 local_serials：adb 不可用跳过、空清单不发、有设备排序上报。"""
    import asyncio

    import httpx

    config_store.set_upstream(mock_upstream.base_url)
    presence.set_token("tok")
    scans = []
    monkeypatch.setattr("engine.adb_scan.scan_devices", lambda: scans.append(1) or [])

    async def run():
        async with httpx.AsyncClient() as http:
            return await presence.heartbeat_once(http)

    def beats():
        return [r for r in mock_upstream.requests if r["path"].endswith("/api/client-devices/heartbeat")]

    try:
        monkeypatch.setattr("engine.adb_scan.local_serials", lambda: None)
        assert asyncio.run(run()) is None
        assert not beats(), "adb 清单不可用时不上报"

        monkeypatch.setattr("engine.adb_scan.local_serials", lambda: set())
        assert asyncio.run(run()) == 0
        assert not beats(), "空清单不上报"

        monkeypatch.setattr("engine.adb_scan.local_serials", lambda: {"USB2", "USB1"})
        assert asyncio.run(run()) == 1
        assert json.loads(beats()[-1]["body"]) == {"serials": ["USB1", "USB2"]}
    finally:
        presence.clear_token()
    assert scans == [], "心跳不得做全量属性扫描"


def test_device_screenshot_local_preview(client, monkeypatch):
    """设备中心截图走本机 adb（不反代云端）；契约 {base64_img, image_format}。"""
    import engine.local_api as local_api
    import base64 as b64
    from PIL import Image
    import io as _io

    def fake_png(adb, serial):
        buf = _io.BytesIO()
        Image.new("RGB", (2000, 1000), "red").save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(local_api, "_screencap_png", fake_png)
    monkeypatch.setattr("engine.adb_scan.resolve_adb_binary", lambda: "/resolved/adb")
    resp = client.get("/api/devices/3B6F/screenshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["image_format"] == "jpeg"
    raw = b64.b64decode(data["base64_img"])
    img = Image.open(_io.BytesIO(raw))
    assert max(img.size) <= 1280, "预览规格：最长边 ≤1280"


def test_device_screenshot_device_not_attached(client, monkeypatch):
    import engine.local_api as local_api

    def missing(adb, serial):
        raise local_api._DeviceNotAttached("设备未连接到本机: X1")

    monkeypatch.setattr(local_api, "_screencap_png", missing)
    monkeypatch.setattr("engine.adb_scan.resolve_adb_binary", lambda: "/resolved/adb")
    resp = client.get("/api/devices/X1/screenshot")
    assert resp.status_code == 404
    assert "设备未连接到本机" in resp.json()["detail"]


def test_device_screenshot_adb_missing_hint(client, monkeypatch):
    import engine.local_api as local_api

    def no_adb():
        raise FileNotFoundError("未检测到 adb")

    monkeypatch.setattr("engine.adb_scan.resolve_adb_binary", no_adb)
    resp = client.get("/api/devices/X1/screenshot")
    assert resp.status_code == 503
    assert "adb" in resp.json()["detail"]



def _login(client, mock_upstream):
    client.post(
        "/api/auth/token",
        data={"username": "u", "password": "p"},
        headers={"X-AutoDroid-Server": mock_upstream.base_url},
    )


def test_device_lists_show_local_stability_run_as_busy(client, mock_upstream):
    """客户端本机跑稳定性：云端仍报空闲，引擎在两个设备列表上覆盖为「跑测中」。"""
    from engine.core import device_busy

    _login(client, mock_upstream)
    device_busy.reset_for_tests()
    assert device_busy.try_acquire("MOCK1")
    try:
        devices = client.get("/api/devices/", headers={"Authorization": "Bearer tok"}).json()
        assert {d["serial"]: d["status"] for d in devices}["MOCK1"] == "FASTBOT_RUNNING"
        resp = client.get("/api/fastbot/devices?include_ios=true", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert {d["serial"]: d["status"] for d in resp.json()}["MOCK1"] == "FASTBOT_RUNNING"
        fastbot_req = [r for r in mock_upstream.requests if r["path"].startswith("/api/fastbot/devices")][-1]
        assert "include_ios=true" in fastbot_req["path"], "查询参数原样回源"
    finally:
        device_busy.reset_for_tests()
    devices = client.get("/api/devices/", headers={"Authorization": "Bearer tok"}).json()
    assert {d["serial"]: d["status"] for d in devices}["MOCK1"] == "IDLE"
