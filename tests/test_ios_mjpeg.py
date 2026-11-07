"""iOS 实时画面（engine.core.ios_mjpeg + WS /ws/ios-mjpeg）与设备中心 iOS 路由。

假 MJPEG 服务按 WDA 真机格式输出（2026-11-06 抓包：HTTP/1.0 200 + boundary=--BoundaryString，
每段带 Content-Length）。不碰真机。
"""
from __future__ import annotations

import asyncio
import io
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app
from engine.core import device_busy, ios_device, ios_mjpeg

UDID = "00008120-00146CA4110BC01E"


def _jpeg(color=(10, 20, 30), size=(60, 120), with_thumbnail_eoi=False) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="JPEG")
    data = buffer.getvalue()
    if with_thumbnail_eoi:
        # 在 APP 段里塞一个 EOI 标记（模拟内嵌缩略图）：只按标记扫描会把帧截断
        comment = b"\xff\xfe\x00\x06" + b"\xff\xd9" + b"ok"
        data = data[:2] + comment + data[2:]
    return data


def _part(frame: bytes, content_length=True) -> bytes:
    header = b"--BoundaryString\r\nContent-type: image/jpeg\r\n"
    if content_length:
        header += b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
    return header + b"\r\n" + frame + b"\r\n"


HTTP_HEAD = (
    b"HTTP/1.0 200 OK\r\nServer: WDA MJPEG Server\r\nConnection: close\r\n"
    b"Content-Type: multipart/x-mixed-replace; boundary=--BoundaryString\r\n\r\n"
)


# --------------------------- 解析 --------------------------- #

def test_parser_uses_content_length_even_with_embedded_eoi():
    frame = _jpeg(with_thumbnail_eoi=True)
    parser = ios_mjpeg.MjpegFrameParser()
    stream = HTTP_HEAD + _part(frame) + _part(_jpeg(color=(1, 2, 3)))
    out = []
    for i in range(0, len(stream), 7):  # 小块喂入：跨块拼帧
        out.extend(parser.feed(stream[i:i + 7]))
    assert out[0] == frame, "带 Content-Length 时按长度截取完整帧"
    assert len(out) == 2


def test_parser_falls_back_to_markers_without_content_length():
    frame = _jpeg()
    parser = ios_mjpeg.MjpegFrameParser()
    assert parser.feed(HTTP_HEAD + _part(frame, content_length=False)) == [frame]


def test_parser_overflow_raises():
    parser = ios_mjpeg.MjpegFrameParser(max_buffer=64)
    with pytest.raises(ios_mjpeg.MjpegStreamError):
        parser.feed(b"\xff\xd8\xff" + b"\x00" * 100)


# --------------------------- 假 WDA MJPEG 服务 --------------------------- #

class FakeMjpegServer:
    def __init__(self, fps=50):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.fps = fps
        self.connections = 0
        self.stop = threading.Event()
        self.silent = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.recv(1024)
            conn.sendall(HTTP_HEAD)
            index = 0
            while not self.stop.is_set():
                if not self.silent:
                    conn.sendall(_part(_jpeg(color=(index % 255, 0, 0))))
                    index += 1
                time.sleep(1.0 / self.fps)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


class _Host:
    def __init__(self, port):
        self.base_url = "http://127.0.0.1:1"
        self.mjpeg_port = port


class _Registry:
    def __init__(self, port):
        self.host = _Host(port)

    def get(self, udid):
        return self.host


@pytest.fixture()
def fake_stream(monkeypatch):
    server = FakeMjpegServer()
    monkeypatch.setattr(ios_mjpeg, "registry", _Registry(server.port))
    monkeypatch.setattr(ios_device, "wda_url", lambda serial, wait_seconds=None: "http://127.0.0.1:1")
    settings_calls = []
    monkeypatch.setattr(ios_mjpeg, "apply_mjpeg_settings", lambda serial: settings_calls.append(serial) or True)
    ios_mjpeg.reset_for_tests()
    server.settings_calls = settings_calls
    yield server
    ios_mjpeg.reset_for_tests()
    server.close()


def test_iter_frames_stops_promptly_on_stop(fake_stream):
    stop = threading.Event()
    frames = []
    started = time.monotonic()
    for frame in ios_mjpeg.iter_mjpeg_frames(fake_stream.port, stop):
        frames.append(frame)
        if len(frames) == 3:
            stop.set()
    assert len(frames) == 3 and frames[0][:2] == b"\xff\xd8"
    assert time.monotonic() - started < 3


def test_iter_frames_gives_up_on_silent_upstream(fake_stream):
    fake_stream.silent = True
    with pytest.raises(ios_mjpeg.MjpegStreamError):
        for _ in ios_mjpeg.iter_mjpeg_frames(fake_stream.port, threading.Event(), no_frame_timeout=1.5):
            pass


def _collect(hub, count, timeout=5.0):
    async def run():
        queue = hub.subscribe(asyncio.get_running_loop())
        frames = []
        try:
            while len(frames) < count:
                frames.append(await asyncio.wait_for(queue.get(), timeout))
        finally:
            hub.unsubscribe(queue)
        return frames

    return asyncio.run(run())


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_hub_lifecycle_latest_frame_stop_and_restart(fake_stream):
    hub = ios_mjpeg.mjpeg_hub(UDID)
    frames = _collect(hub, 3)
    assert all(f[:2] == b"\xff\xd8" for f in frames)
    assert fake_stream.settings_calls == [UDID], "接流前下发帧率 / 缩放"
    assert _wait(lambda: not hub._thread.is_alive()), "最后一个订阅者离开后读取线程退出"
    latest = hub.latest
    assert latest

    async def resubscribe():
        queue = hub.subscribe(asyncio.get_running_loop())
        first = await asyncio.wait_for(queue.get(), 1)
        second = await asyncio.wait_for(queue.get(), 5)
        hub.unsubscribe(queue)
        return first, second

    first, second = asyncio.run(resubscribe())
    assert first == latest, "新订阅者先拿到最近一帧（秒出画面）"
    assert second[:2] == b"\xff\xd8", "再次订阅重新拉起读取线程"


def test_hub_resubscribe_while_stopping_starts_new_reader(fake_stream):
    hub = ios_mjpeg.mjpeg_hub(UDID)
    loop = asyncio.new_event_loop()
    try:
        queue = hub.subscribe(loop)
        old_thread = hub._thread
        hub.unsubscribe(queue)  # 读取线程收到停止信号但可能还没退出
        queue2 = hub.subscribe(loop)
        assert hub._thread is not old_thread
        hub.unsubscribe(queue2)
    finally:
        loop.close()


def test_hub_reports_unavailable_wda(monkeypatch):
    monkeypatch.setattr(
        ios_device,
        "wda_url",
        lambda serial, wait_seconds=None: (_ for _ in ()).throw(ios_device.IOSDeviceError("WDA 启动失败：描述文件已过期")),
    )
    ios_mjpeg.reset_for_tests()
    hub = ios_mjpeg.mjpeg_hub(UDID)
    assert _collect(hub, 1) == [None]
    assert "描述文件" in hub.last_error


def test_apply_settings_skipped_during_stability_run(monkeypatch):
    import httpx

    monkeypatch.setattr(ios_mjpeg, "registry", _Registry(1))
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应访问 WDA")))
    assert device_busy.try_acquire(UDID)
    assert ios_mjpeg.apply_mjpeg_settings(UDID) is False


def test_apply_settings_reuses_session_or_creates_empty_one(monkeypatch):
    import httpx

    requests = []

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            requests.append(("GET", url))
            return _Resp({"value": {"ready": True}, "sessionId": None})

        def post(self, url, json=None):
            requests.append(("POST", url, json))
            if url.endswith("/session"):
                assert json == {"capabilities": {}}, "空会话不绑定 App"
                return _Resp({"value": {"sessionId": "S1"}})
            return _Resp({"value": None})

    monkeypatch.setattr(ios_mjpeg, "registry", _Registry(1))
    monkeypatch.setattr(httpx, "Client", _Client)
    assert ios_mjpeg.apply_mjpeg_settings(UDID) is True
    assert requests[-1][1].endswith("/session/S1/appium/settings")
    assert requests[-1][2] == {"settings": ios_mjpeg.MJPEG_SETTINGS}


# --------------------------- 路由 --------------------------- #

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setattr("engine.app.local_serials", lambda *args, **kwargs: set())
    config_store.reset_for_tests()
    presence.reset_for_tests()
    with TestClient(create_app()) as test_client:
        yield test_client
    presence.reset_for_tests()
    config_store.reset_for_tests()


def test_ws_relays_frames(client, fake_stream):
    with client.websocket_connect(f"/ws/ios-mjpeg/{UDID}?token=abc") as ws:
        frames = [ws.receive_bytes() for _ in range(3)]
    assert all(f[:2] == b"\xff\xd8" for f in frames)
    hub = ios_mjpeg.mjpeg_hub(UDID)
    assert _wait(lambda: hub.subscriber_count == 0), "页面断开后退订"


def test_ws_closes_4005_when_wda_unavailable(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(
        ios_device,
        "wda_url",
        lambda serial, wait_seconds=None: (_ for _ in ()).throw(
            ios_device.IOSDeviceError("iOS 设备未通过 USB 连接或未信任本机，" * 6)
        ),
    )
    ios_mjpeg.reset_for_tests()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/ios-mjpeg/{UDID}") as ws:
            ws.receive_bytes()
    assert exc.value.code == 4005
    assert exc.value.reason.startswith("P1005_WDA_UNAVAILABLE")
    assert len(exc.value.reason.encode("utf-8")) <= 123, "关闭原因按字节截断（中文 3 字节）"


def test_screenshot_route_ios_branch(client, monkeypatch):
    monkeypatch.setattr(ios_device, "preview_screenshot", lambda serial: {"base64_img": "QUJD", "image_format": "jpeg"})
    resp = client.get(f"/api/devices/{UDID}/screenshot")
    assert resp.status_code == 200 and resp.json() == {"base64_img": "QUJD", "image_format": "jpeg"}

    monkeypatch.setattr(
        ios_device,
        "preview_screenshot",
        lambda serial: (_ for _ in ()).throw(ios_device.IOSDeviceError(ios_device.WDA_STARTING_DETAIL)),
    )
    resp = client.get(f"/api/devices/{UDID}/screenshot")
    assert resp.status_code == 503 and "正在启动" in resp.json()["detail"]


def test_wda_check_route(client, monkeypatch):
    monkeypatch.setattr(ios_device, "wda_url", lambda serial, wait_seconds=None: "http://127.0.0.1:18100")
    data = client.post(f"/api/devices/{UDID}/wda/check").json()
    assert data["wda_healthy"] is True and data["status"] == "IDLE"

    monkeypatch.setattr(
        ios_device,
        "wda_url",
        lambda serial, wait_seconds=None: (_ for _ in ()).throw(ios_device.IOSDeviceError(ios_device.WDA_STARTING_DETAIL)),
    )
    data = client.post(f"/api/devices/{UDID}/wda/check").json()
    assert data["wda_healthy"] is False and "正在启动" in data["error"]

    assert client.post("/api/devices/3B6F68E6SA7TA0Q5/wda/check").status_code == 400


# --------------------------- 定时任务本机设备过滤 --------------------------- #

def test_scheduled_task_keeps_wired_iphone_only_when_on_this_machine(monkeypatch):
    from engine.core import scenario_service, task_scheduler

    started = []
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": True, "cases": []},
    )
    monkeypatch.setattr(scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs))
    task = {"id": 1, "name": "t", "task_type": "UI", "scenario_id": 3, "device_serials": [UDID, "OTHER-UDID"],
            "strategy_config": {}}
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: {UDID})
    monkeypatch.setattr(task_scheduler, "device_serials_of", lambda t: [UDID, "00008150-000C103434C0401C"])
    monkeypatch.setattr(task_scheduler, "task_type_of", lambda t: next(iter(task_scheduler.LOCAL_TASK_TYPES)))
    result = task_scheduler.execute_task(task, {"Authorization": "Bearer t"})
    assert [item["device_serial"] for item in result] == [UDID], "USB iPhone 按本机清单过滤（不在本机的不触发）"
