# 来源：语义对齐 AutoDroid backend/device_stream/ios_mjpeg.py（MjpegStreamParser、apply_wda_mjpeg_settings、
#       广播流 + 最近一帧缓存）。
"""桌面端 iOS 实时画面：WDA MJPEG（设备 9100 端口，经 iproxy 转发）→ WS 每条二进制消息一帧 JPEG。

WDA 由 engine.core.fastbot_ios.wda.registry 统一管理，与录制 / 稳定性测试共用。
每台设备一个读取线程（MjpegHub），多个页面订阅同一路流；新订阅者先拿到最近一帧（秒出画面），
最后一个订阅者离开后读取线程退出。
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

import httpx

from engine.core import device_busy, ios_device
from engine.core.fastbot_ios.tools import udid_from_serial
from engine.core.fastbot_ios.wda import registry

logger = logging.getLogger(__name__)

# 实时画面：15fps / 50% 缩放 / 质量 50（对齐平台默认帧率与质量；本机 USB 链路，缩放后帧小、页面解码快）
MJPEG_SETTINGS = {
    "mjpegServerFramerate": 15,
    "mjpegScalingFactor": 50,
    "mjpegServerScreenshotQuality": 50,
}
MAX_FRAME_BYTES = 8 * 1024 * 1024
# 读超时短一些：停止信号最多等这么久就能被读取线程看到
SOCKET_READ_TIMEOUT_SECONDS = 1.0
# 连着这么久收不到一帧视为上游卡死，断开重连
NO_FRAME_RECONNECT_SECONDS = 10.0
# 连续这么多次连不上 / 断流后判定上游不可用，通知订阅者（WS 以 4005 关闭）
MAX_CONSECUTIVE_FAILURES = 5
RECONNECT_DELAY_SECONDS = 1.0

_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"
_CONTENT_LENGTH_RE = re.compile(rb"content-length\s*:\s*(\d+)", re.IGNORECASE)


class MjpegStreamError(RuntimeError):
    pass


class MjpegFrameParser:
    """增量解析 multipart MJPEG，提取完整 JPEG 帧（语义同平台 MjpegStreamParser）。

    WDA 每个分段带 Content-Length：按长度精确截取（JPEG 内嵌缩略图也有 EOI 标记，
    只按标记扫描会把帧截断）；没有 Content-Length 时退回 SOI/EOI 扫描。
    HTTP 响应头、boundary 行等非 JPEG 前缀自动跳过。
    """

    def __init__(self, max_buffer: int = MAX_FRAME_BYTES * 2) -> None:
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    def feed(self, data: bytes) -> List[bytes]:
        if data:
            self._buffer.extend(data)
        frames: List[bytes] = []
        while True:
            frame = self._extract_one()
            if frame is None:
                break
            frames.append(frame)
        if len(self._buffer) > self._max_buffer:
            raise MjpegStreamError(f"MJPEG 解析缓冲溢出（{len(self._buffer)} 字节），流已损坏")
        return frames

    def _extract_one(self) -> Optional[bytes]:
        soi = self._buffer.find(_JPEG_SOI)
        if soi < 0:
            return None
        matches = _CONTENT_LENGTH_RE.findall(bytes(self._buffer[:soi]))
        if matches:
            frame_len = int(matches[-1])
            if 0 < frame_len <= MAX_FRAME_BYTES:
                end = soi + frame_len
                if len(self._buffer) < end:
                    return None
                frame = bytes(self._buffer[soi:end])
                del self._buffer[:end]
                return frame
        eoi = self._buffer.find(_JPEG_EOI, soi + len(_JPEG_SOI))
        if eoi < 0:
            return None
        end = eoi + len(_JPEG_EOI)
        frame = bytes(self._buffer[soi:end])
        del self._buffer[:end]
        return frame


def iter_mjpeg_frames(
    port: int,
    stop: threading.Event,
    connect_timeout: float = 5.0,
    no_frame_timeout: float = NO_FRAME_RECONNECT_SECONDS,
) -> Iterator[bytes]:
    """连上 127.0.0.1:port 的 WDA MJPEG 服务逐帧产出 JPEG；断流 / 长时间无帧 / 停止即结束。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=connect_timeout)
    try:
        sock.settimeout(SOCKET_READ_TIMEOUT_SECONDS)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        parser = MjpegFrameParser()
        last_frame_at = time.monotonic()
        while not stop.is_set():
            try:
                chunk = sock.recv(256 * 1024)
            except socket.timeout:
                if time.monotonic() - last_frame_at > no_frame_timeout:
                    raise MjpegStreamError(f"{no_frame_timeout:.0f}s 未收到画面")
                continue
            if not chunk:
                return
            for frame in parser.feed(chunk):
                last_frame_at = time.monotonic()
                yield frame
    finally:
        try:
            sock.close()
        except OSError:
            pass


Subscriber = Tuple[asyncio.AbstractEventLoop, "asyncio.Queue[Optional[bytes]]"]


class MjpegHub:
    """单台设备的 MJPEG 读取线程 + 订阅者分发。

    订阅队列只留最新 2 帧（慢客户端丢旧帧）；队列里的 None 表示上游不可用。
    """

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.latest: Optional[bytes] = None
        self.last_error = ""
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> "asyncio.Queue[Optional[bytes]]":
        queue: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=2)
        with self._lock:
            self._subs.append((loop, queue))
            if self.latest:
                queue.put_nowait(self.latest)
            # 读取线程没在跑，或已被要求退出（上一个订阅者刚离开）：起新线程
            if self._thread is None or not self._thread.is_alive() or self._stop.is_set():
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._run, args=(self._stop,), name=f"ios-mjpeg-{self.serial}", daemon=True
                )
                self._thread.start()
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[Optional[bytes]]") -> None:
        with self._lock:
            self._subs = [item for item in self._subs if item[1] is not queue]
            if not self._subs:
                self._stop.set()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()

    def _broadcast(self, frame: Optional[bytes]) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(_offer, queue, frame)
            except RuntimeError:
                pass  # 订阅者的事件循环已关闭

    def _run(self, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                # 每次（重）连前确认 WDA 就绪：设备重插、WDA 被系统回收后这里会重新拉起
                ios_device.wda_url(self.serial, wait_seconds=ios_device.WDA_RUN_WAIT_SECONDS)  # 页面显示连接中，等冷启动
                if stop.is_set():
                    return
                apply_mjpeg_settings(self.serial)
                for frame in iter_mjpeg_frames(registry.get(self.serial).mjpeg_port, stop):
                    failures = 0
                    self.latest = frame
                    self._broadcast(frame)
            except ios_device.IOSDeviceError as exc:
                self.last_error = str(exc)
                logger.warning("iOS 实时画面：WDA 不可用 serial=%s error=%s", self.serial, exc)
                if not stop.is_set():
                    self._broadcast(None)
                return
            except (OSError, MjpegStreamError) as exc:
                self.last_error = str(exc)
                logger.info("iOS 实时画面断流重连 serial=%s: %s", self.serial, exc)
            if stop.is_set():
                return
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning("iOS 实时画面上游不可用 serial=%s: %s", self.serial, self.last_error)
                self._broadcast(None)
                return
            stop.wait(RECONNECT_DELAY_SECONDS)


def _offer(queue: "asyncio.Queue[Optional[bytes]]", frame: Optional[bytes]) -> None:
    if queue.full():
        try:
            queue.get_nowait()  # 丢旧帧，只保留最新画面
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(frame)


def apply_mjpeg_settings(serial: str) -> bool:
    """下发帧率 / 缩放 / 质量（尽力而为，失败沿用 WDA 默认；语义同平台 apply_wda_mjpeg_settings）。

    设置只能在会话内下发：有活动会话就用它，没有就建一个不绑定 App 的空会话。
    稳定性测试占用设备时跳过：测试自己会下发同样的设置，且新建会话会顶掉测试会话。
    """
    try:
        udid = udid_from_serial(serial)
    except ValueError:
        return False
    if device_busy.is_busy(serial) or device_busy.is_busy(udid):
        return False
    base = registry.get(udid).base_url
    try:
        with httpx.Client(timeout=5.0) as client:
            payload = client.get(f"{base}/status").json() or {}
            session_id = payload.get("sessionId") or (payload.get("value") or {}).get("sessionId")
            if not session_id:
                created = client.post(f"{base}/session", json={"capabilities": {}}).json() or {}
                session_id = created.get("sessionId") or (created.get("value") or {}).get("sessionId")
            if not session_id:
                return False
            client.post(
                f"{base}/session/{session_id}/appium/settings", json={"settings": dict(MJPEG_SETTINGS)}
            ).raise_for_status()
        return True
    except Exception as exc:
        logger.info("iOS 实时画面设置下发失败（沿用默认）serial=%s: %s", serial, exc)
        return False


_hubs: Dict[str, MjpegHub] = {}
_hubs_lock = threading.Lock()


def mjpeg_hub(serial: str) -> MjpegHub:
    udid = udid_from_serial(serial)
    with _hubs_lock:
        hub = _hubs.get(udid)
        if hub is None:
            hub = MjpegHub(udid)
            _hubs[udid] = hub
        return hub


def shutdown() -> None:
    """引擎退出：停止所有 MJPEG 读取线程（在 ios_device.shutdown 关闭 WDA 之前调用）。"""
    with _hubs_lock:
        hubs = list(_hubs.values())
        _hubs.clear()
    for hub in hubs:
        hub.stop()


def reset_for_tests() -> None:
    shutdown()
