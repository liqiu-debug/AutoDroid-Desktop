"""本地设备 I/O / 本地执行路由集成测试（TestClient + mock upstream）。"""
from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app
from engine.core import runner_service


class _MockCloud:
    """云端数据面 mock：client-templates / client-runs / cases / settings。"""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.template_status = 200
        self.template_path = "static/images/element_cloud1.png"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
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

            def do_POST(self):  # noqa: N802
                body = self._read_body()
                owner.requests.append(
                    {"method": "POST", "path": self.path,
                     "headers": {k.lower(): v for k, v in self.headers.items()},
                     "body": body}
                )
                if self.path.endswith("/auth/token"):
                    self._send(200, {"access_token": "tok-test", "token_type": "bearer"})
                    return
                if "/api/client-fastbot/" in self.path and self.path.endswith("/finish"):
                    self._send(200, {"task_id": 9, "report_id": 1, "status": "COMPLETED"})
                    return
                if self.path.endswith("/api/client-fastbot/"):
                    self._send(201, {
                        "id": 9, "package_name": "com.x", "duration": 60, "throttle": 500,
                        "ignore_crashes": False, "capture_log": True, "device_serial": "USB1",
                        "status": "RUNNING", "total_crashes": 0, "total_anrs": 0,
                        "executor_name": "桌面客户端: t", "created_at": "2026-10-26T00:00:00",
                        "started_at": "2026-10-26T00:00:00", "finished_at": None,
                    })
                    return
                if self.path.endswith("/api/client-templates"):
                    self._send(owner.template_status, {"image_path": owner.template_path})
                    return
                if "/api/client-runs/" in self.path and self.path.endswith("/steps"):
                    self._send(200, {"accepted": True})
                    return
                if "/api/client-runs/" in self.path and self.path.endswith("/finish"):
                    self._send(200, {"report_id": "rep-99"})
                    return
                if self.path.endswith("/api/client-runs/"):
                    self._send(201, {"id": 42, "run_id": "run-42"})
                    return
                self._send(404, {"detail": "Not found"})

            def do_GET(self):  # noqa: N802
                owner.requests.append(
                    {"method": "GET", "path": self.path,
                     "headers": {k.lower(): v for k, v in self.headers.items()}}
                )
                if self.path.startswith("/api/cases/") and self.path.endswith("/steps"):
                    self._send(200, [
                        {"order": 1, "action": "click", "args": {},
                         "platform_overrides": {"android": {"selector": "确定", "by": "text"}},
                         "execute_on": ["android"], "timeout": 10, "error_strategy": "ABORT",
                         "description": "点确定", "retry_count": 0}
                    ])
                    return
                if self.path.startswith("/api/cases/"):
                    self._send(200, {"id": 5, "name": "登录用例", "variables": []})
                    return
                if self.path.startswith("/api/reports/dashboard/"):
                    self._send(200, {"stats_marker": self.path, "ok": True})
                    return
                if self.path.startswith("/api/environments/"):
                    self._send(200, [{"key": "ENV_K", "value": "v"}])
                    return
                if self.path.startswith("/api/settings"):
                    self._send(200, [{"key": "app_key_map", "value": "{}"}])
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
def mock_cloud():
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


def _png_b64() -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), color=(1, 2, 3)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def test_crop_template_uploads_and_caches(client, mock_cloud, tmp_path):
    resp = client.post(
        "/api/device/crop_template",
        json={"screenshot_base64": _png_b64(), "x1": 1, "y1": 1, "x2": 10, "y2": 10},
    )
    assert resp.status_code == 200
    assert resp.json()["image_path"] == "static/images/element_cloud1.png"

    uploads = [r for r in mock_cloud.requests if r["path"].endswith("/api/client-templates")]
    assert uploads, "应调用云端 /api/client-templates"
    assert "multipart/form-data" in uploads[0]["headers"].get("content-type", "")

    cache_file = config_store.user_data_dir() / "templates" / "element_cloud1.png"
    assert cache_file.is_file()
    assert cache_file.read_bytes()[:2] == b"\x89P"


def test_crop_template_upload_failure_blocks_with_retry(client, mock_cloud):
    """R6：上传失败阻断并提示重试——不得回退本地兜底文件名（写进步骤即悬空引用）。"""
    mock_cloud.template_status = 500
    resp = client.post(
        "/api/device/crop_template",
        json={"screenshot_base64": _png_b64(), "x1": 1, "y1": 1, "x2": 8, "y2": 8},
    )
    assert resp.status_code == 502
    assert "重试" in resp.json()["detail"]
    assert "image_path" not in resp.json()


def test_crop_template_invalid_region(client):
    resp = client.post(
        "/api/device/crop_template",
        json={"screenshot_base64": _png_b64(), "x1": 5, "y1": 5, "x2": 1, "y2": 1},
    )
    assert resp.status_code == 400
    assert "裁剪区域无效" in resp.json()["detail"]


def test_device_dump_is_local_not_proxied(client, mock_cloud, monkeypatch):
    from engine.core import recording_service

    class _FakeDevice:
        info = {"displayWidth": 10, "displayHeight": 20}

        def dump_hierarchy(self):
            return "<hierarchy><node text='a' bounds='[0,0][1,1]' /></hierarchy>"

        def screenshot(self):
            from PIL import Image

            return Image.new("RGB", (10, 20))

    monkeypatch.setattr(recording_service._recording_android_device_pool, "acquire", lambda s: _FakeDevice())
    resp = client.get("/api/device/dump?serial=USB1&include_device_info=false")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["hierarchy_xml"].startswith("<hierarchy")
    assert "device_info" not in payload
    # 本地拦截：mock upstream 不应收到 /api/device/dump
    assert not [r for r in mock_cloud.requests if "device/dump" in r["path"]]


def test_device_dump_hierarchy_hash_cache(client, mock_cloud, monkeypatch):
    from engine.core import recording_service

    class _FakeDevice:
        info = {}

        def dump_hierarchy(self):
            return "<hierarchy><node text='a' bounds='[0,0][1,1]' /></hierarchy>"

        def screenshot(self):
            from PIL import Image

            return Image.new("RGB", (4, 4))

    monkeypatch.setattr(recording_service._recording_android_device_pool, "acquire", lambda s: _FakeDevice())
    first = client.get("/api/device/dump?serial=USB1").json()
    cached = client.get(f"/api/device/dump?serial=USB1&hierarchy_hash={first['hierarchy_hash']}").json()
    assert cached.get("hierarchy_cached") is True
    assert "hierarchy_xml" not in cached
    assert cached["hierarchy_hash"] == first["hierarchy_hash"]


def test_run_requires_device_serial(client):
    resp = client.post("/api/cases/5/run")
    assert resp.status_code == 400
    assert "请选择执行设备" in resp.json()["detail"]


def test_dashboard_endpoints_are_proxied_transparently(client, mock_cloud):
    """P3-3 运行大盘核对：GET /api/reports/dashboard/* 纯反代即工作（零开发）。"""
    for path in ("/api/reports/dashboard/stats", "/api/reports/dashboard/overview"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.json()["stats_marker"] == path
    proxied = [r["path"] for r in mock_cloud.requests if r["method"] == "GET"]
    assert "/api/reports/dashboard/stats" in proxied
    assert "/api/reports/dashboard/overview" in proxied


def test_run_case_intercepts_and_returns_expected_shape(client, monkeypatch):
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(runner_service, "start_background_case_run", fake_start)
    resp = client.post("/api/cases/5/run?env_id=3&device_serial=USB1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["case_id"] == 5
    assert data["device_serial"] == "USB1"
    assert data["runner"] == "cross_platform"
    assert data["queued"] is False
    assert data["run_id"] and data["batch_id"]
    assert captured["case_id"] == 5
    assert captured["device_serial"] == "USB1"
    assert captured["env_id"] == 3


def test_run_batch_intercepts_and_returns_expected_shape(client, monkeypatch):
    started = []

    monkeypatch.setattr(
        runner_service,
        "precheck_for_device",
        lambda bridge, case_id, serial, env_id=None: {"ok": True, "platform": "android"},
    )
    monkeypatch.setattr(
        runner_service, "start_background_case_run", lambda **kwargs: started.append(kwargs)
    )
    resp = client.post(
        "/api/cases/5/run-batch",
        json={"env_id": 3, "device_serials": ["USB1", "USB2", None]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["case_id"] == 5
    assert data["device_serials"] == ["USB1", "USB2"]
    assert len(data["run_ids"]) == 2
    assert data["queued_count"] == 0
    assert data["blocked_prechecks"] == [{"device_serial": None, "reason": "请选择执行设备"}]
    assert {item["device_serial"] for item in data["runs"]} == {"USB1", "USB2"}
    assert len(started) == 2


def test_run_batch_all_precheck_failed_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        runner_service,
        "precheck_for_device",
        lambda bridge, case_id, serial, env_id=None: {"ok": False, "step_checks": []},
    )
    resp = client.post("/api/cases/5/run-batch", json={"device_serials": ["USB1"]})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "C1001_CASE_PRECHECK_FAILED"
    assert detail["items"]


def test_ws_run_streams_protocol_messages(client, monkeypatch):
    async def fake_run_via_ws(websocket, case_id, env_id, device_serial, auth_headers):
        await websocket.accept()
        await websocket.send_json({"type": "run_start", "case_id": case_id, "case_name": "t",
                                   "total_steps": 1, "status": "RUNNING"})
        await websocket.send_json({"type": "step_update", "step_index": 0, "status": "success",
                                   "log": "ok", "duration": 0.1})
        await websocket.send_json({"type": "run_complete", "case_id": case_id, "success": True,
                                   "status": "PASS", "passed": 1, "failed": 0,
                                   "total_duration": 0.1, "report_id": "rep-99"})
        await websocket.close()

    seen = {}

    async def spy(websocket, case_id, env_id, device_serial, auth_headers):
        seen["case_id"] = case_id
        seen["device_serial"] = device_serial
        seen["env_id"] = env_id
        await fake_run_via_ws(websocket, case_id, env_id, device_serial, auth_headers)

    monkeypatch.setattr(runner_service, "run_case_via_websocket", spy)
    with client.websocket_connect("/ws/run/5?env_id=3&device_serial=USB1") as ws:
        assert ws.receive_json()["type"] == "run_start"
        assert ws.receive_json()["type"] == "step_update"
        complete = ws.receive_json()
        assert complete["type"] == "run_complete"
        assert complete["report_id"] == "rep-99"
    assert seen == {"case_id": 5, "device_serial": "USB1", "env_id": 3}


def test_ws_run_is_local_not_proxied(client, mock_cloud, monkeypatch):
    async def spy(websocket, case_id, env_id, device_serial, auth_headers):
        await websocket.accept()
        await websocket.send_json({"type": "run_start", "case_id": case_id})
        await websocket.close()

    monkeypatch.setattr(runner_service, "run_case_via_websocket", spy)
    with client.websocket_connect("/ws/run/5?device_serial=USB1") as ws:
        assert ws.receive_json()["case_id"] == 5
    # 本地拦截：/ws/run 不应反代到 upstream
    assert not [r for r in mock_cloud.requests if "/ws/run" in r["path"]]


# --------------------------- 设备流（scrcpy 本地） --------------------------- #

@pytest.fixture()
def fake_stream_manager(monkeypatch):
    from engine.core.device_stream import manager as stream_manager

    class _FakeManager:
        def __init__(self):
            self.touches = []
            self.reconnects = []

        def get_devices_list(self):
            return [{"serial": "USB1", "ready": True}]

        def get_device(self, serial):
            return {"serial": serial, "ready": True} if serial == "USB1" else None

        def send_touch_event(self, serial, action, x, y, *, method="scrcpy"):
            self.touches.append((serial, action, x, y, method))

        def reconnect_device(self, serial):
            self.reconnects.append(serial)

        def get_video_generator(self, serial):
            if serial != "USB1":
                raise ValueError(f"设备 {serial} 未就绪或不存在")
            yield b"\x00\x00\x00\x01SPS"
            yield b"\x00\x00\x00\x01IDR"

        def start_tracking(self):
            pass

        def stop_tracking(self):
            pass

    fake = _FakeManager()
    monkeypatch.setattr(stream_manager, "device_manager", fake)
    monkeypatch.setattr(stream_manager, "get_stream_profile_state", lambda serial: {"profile": "hd"})
    monkeypatch.setattr(stream_manager, "set_stream_profile_override", lambda serial, p: None)
    return fake


def test_stream_devices_list_is_local(client, mock_cloud, fake_stream_manager):
    resp = client.get("/api/stream/devices")
    assert resp.status_code == 200
    assert resp.json() == [{"serial": "USB1", "ready": True}]
    assert not [r for r in mock_cloud.requests if "/stream/devices" in r["path"]]


def test_stream_touch_is_local(client, mock_cloud, fake_stream_manager):
    resp = client.post("/api/stream/devices/USB1/touch", json={"action": 0, "x": 10, "y": 20})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert fake_stream_manager.touches == [("USB1", 0, 10, 20, "scrcpy")]
    assert not [r for r in mock_cloud.requests if "/touch" in r["path"]]


def test_stream_touch_unknown_device_404(client, fake_stream_manager):
    resp = client.post("/api/stream/devices/NOPE/touch", json={"action": 0, "x": 1, "y": 2})
    # FakeManager 不校验存在性；真实 send_touch_event 对未知设备抛 ValueError→404。
    # 这里断言路由层的 ValueError→404 映射（用异常替身触发）。
    def _boom(serial, action, x, y, *, method="scrcpy"):
        raise ValueError(f"设备 {serial} 未就绪或不存在")

    fake_stream_manager.send_touch_event = _boom
    resp = client.post("/api/stream/devices/NOPE/touch", json={"action": 0, "x": 1, "y": 2})
    assert resp.status_code == 404
    assert "未就绪或不存在" in resp.json()["detail"]


def test_stream_profile_roundtrip(client, fake_stream_manager):
    resp = client.get("/api/stream/devices/USB1/stream-profile")
    assert resp.status_code == 200
    assert resp.json()["profile"] == "hd"

    resp = client.post("/api/stream/devices/USB1/stream-profile", json={"profile": "smooth"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "reconnecting"  # 设备在管理中 → 立即重启生效
    assert fake_stream_manager.reconnects == ["USB1"]


def test_stream_reconnect(client, fake_stream_manager):
    resp = client.post("/api/stream/devices/USB1/reconnect")
    assert resp.status_code == 200
    assert resp.json() == {"status": "reconnecting", "serial": "USB1"}
    assert fake_stream_manager.reconnects == ["USB1"]


def test_ws_scrcpy_streams_frames(client, mock_cloud, fake_stream_manager):
    with client.websocket_connect("/ws/scrcpy/USB1") as ws:
        assert ws.receive_bytes() == b"\x00\x00\x00\x01SPS"
        assert ws.receive_bytes() == b"\x00\x00\x00\x01IDR"
    # 本地拦截：/ws/scrcpy 不应反代 upstream
    assert not [r for r in mock_cloud.requests if "scrcpy" in r["path"]]


def test_ws_scrcpy_video_pump_uses_dedicated_executor(client, fake_stream_manager, monkeypatch):
    """视频泵 next() 走专用线程池（C4：不与 dump/interact/touch 共用默认池）。"""
    from concurrent.futures import ThreadPoolExecutor

    from engine import local_api

    class _RecordingExecutor(ThreadPoolExecutor):
        def __init__(self):
            super().__init__(max_workers=2)
            self.calls = 0

        def submit(self, fn, *args, **kwargs):
            self.calls += 1
            return super().submit(fn, *args, **kwargs)

    recorder = _RecordingExecutor()
    monkeypatch.setattr(local_api, "VIDEO_PUMP_EXECUTOR", recorder)
    try:
        with client.websocket_connect("/ws/scrcpy/USB1") as ws:
            assert ws.receive_bytes() == b"\x00\x00\x00\x01SPS"
            assert ws.receive_bytes() == b"\x00\x00\x00\x01IDR"
    finally:
        recorder.shutdown(wait=False)
    assert recorder.calls >= 3, f"视频泵未走专用线程池: calls={recorder.calls}"


def test_ws_scrcpy_unknown_device_closes_with_4004(client, fake_stream_manager):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/scrcpy/NOPE") as ws:
            ws.receive_bytes()
    assert exc.value.code == 4004


# --------------------------- T7：iOS MJPEG 本地拦截（显式降级） --------------------------- #

def test_ios_mjpeg_http_intercepted_with_degradation(client, mock_cloud):
    """GET /api/stream/ios-mjpeg/{serial} 本地拦截：503 显式降级，不透传云端。"""
    resp = client.get("/api/stream/ios-mjpeg/ios-1")
    assert resp.status_code == 503
    assert "后续版本" in resp.json()["detail"]
    # ?token= 查询通道容忍（<img src> 直挂形态）
    resp2 = client.get("/api/stream/ios-mjpeg/ios-1?token=abc.def")
    assert resp2.status_code == 503
    # 本地拦截：不应反代 upstream
    assert not [r for r in mock_cloud.requests if "ios-mjpeg" in r["path"]]


def test_ios_mjpeg_ws_intercepted_closes_4000_with_reason(client, mock_cloud):
    """WS /ws/ios-mjpeg/{serial} 本地拦截：accept 后 close(4000) + 降级原因（容忍 ?token=）。"""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/ios-mjpeg/ios-1?token=abc.def") as ws:
            ws.receive_text()
    assert exc.value.code == 4000
    assert "后续版本" in (exc.value.reason or "")
    # 不带 P1005_WDA_UNAVAILABLE（避免前端误判为「设备中心启动 WDA」路径）
    assert "P1005_WDA_UNAVAILABLE" not in (exc.value.reason or "")
    assert not [r for r in mock_cloud.requests if "ios-mjpeg" in r["path"]]
