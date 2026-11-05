"""iOS 异常回放：读 WDA MJPEG（设备 9100）滚动缓冲，崩溃时截窗口写 mp4。

MJPEG 实测 13.5fps / 589×1278 / ≈94KB 帧；变帧率 → 写出时按固定 fps 按时间戳重采样，
保证回放时长与真实窗口一致（同 Android 回放「不快放」约定）。
"""
from __future__ import annotations

import collections
import logging
import os
import socket
import threading
import time
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

OUTPUT_FPS = 10
MAX_BUFFER_BYTES = 120 * 1024 * 1024
# 崩溃发现可能滞后（批内崩溃时 /actions 挂起 + 拉报告 ≤20s）：缓冲额外保留 90s，
# 回放窗口按 .ips 时间戳回溯截取
DETECTION_LAG_KEEP_SEC = 90
STORE_MIN_INTERVAL_SEC = 1.0 / OUTPUT_FPS


class MjpegReplayRecorder:
    def __init__(self, port: int, replay_dir: str, pre_roll_sec: int = 30, post_roll_sec: int = 5) -> None:
        self.port = port
        self.replay_dir = replay_dir
        self.pre_roll_sec = int(pre_roll_sec)
        self.post_roll_sec = int(post_roll_sec)
        self._frames: Deque[Tuple[float, bytes]] = collections.deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sequence = 0
        self.frames_received = 0
        self.last_error = ""

    # ------------------------------------------------------------ ingest
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"ios-mjpeg-{self.port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._frames.clear()
            self._bytes = 0

    def _push(self, jpeg: bytes) -> None:
        now = time.monotonic()
        keep = self.pre_roll_sec + self.post_roll_sec + DETECTION_LAG_KEEP_SEC
        with self._lock:
            if self._frames and now - self._frames[-1][0] < STORE_MIN_INTERVAL_SEC:
                stamp, old = self._frames[-1]
                self._frames[-1] = (stamp, jpeg)  # 降到 ≤10fps 存储，保留最新画面
                self._bytes += len(jpeg) - len(old)
                self.frames_received += 1
                return
            self._frames.append((now, jpeg))
            self._bytes += len(jpeg)
            while self._frames and (now - self._frames[0][0] > keep or self._bytes > MAX_BUFFER_BYTES):
                _, old = self._frames.popleft()
                self._bytes -= len(old)
        self.frames_received += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            except OSError as exc:
                self.last_error = str(exc)
                self._stop.wait(2)
                continue
            try:
                sock.settimeout(10)
                sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                buf = b""
                while not self._stop.is_set():
                    chunk = sock.recv(256 * 1024)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                        if start < 0 or end < 0:
                            if start > 0:
                                buf = buf[start:]
                            break
                        self._push(buf[start:end + 2])
                        buf = buf[end + 2:]
            except OSError as exc:
                self.last_error = str(exc)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            self._stop.wait(1)

    # ------------------------------------------------------------ capture
    def capture(self, event_type: str, event_time: str, event_mono: Optional[float] = None) -> Dict[str, object]:
        event_mono = time.monotonic() if event_mono is None else event_mono
        remaining = event_mono + self.post_roll_sec - time.monotonic()
        if remaining > 0:
            self._stop.wait(remaining)
        with self._lock:
            frames = [f for f in self._frames if event_mono - self.pre_roll_sec <= f[0] <= event_mono + self.post_roll_sec]
        if not frames:
            return {"status": "FAILED", "error": f"回放缓冲无画面（MJPEG 未就绪：{self.last_error or '无帧'}）"}
        self._sequence += 1
        safe_type = "".join(ch.lower() for ch in event_type if ch.isalnum()) or "event"
        safe_time = "".join(ch for ch in event_time if ch.isdigit()) or str(int(time.time()))
        filename = f"{safe_type}_{safe_time}_{self._sequence:02d}.mp4"
        os.makedirs(self.replay_dir, exist_ok=True)
        path = os.path.join(self.replay_dir, filename)
        try:
            written = write_mp4(frames, path)
        except Exception as exc:
            logger.warning("iOS 回放写出失败: %s", exc)
            return {"status": "FAILED", "error": f"回放写出失败：{exc}"}
        duration = max(1, int(round(frames[-1][0] - frames[0][0])))
        return {
            "status": "READY", "path": path, "filename": filename,
            "pre_roll_sec": int(round(event_mono - frames[0][0])),
            "post_roll_sec": max(0, int(round(frames[-1][0] - event_mono))),
            "duration_sec": duration, "error": "", "frames": written,
        }


def write_mp4(frames: List[Tuple[float, bytes]], path: str, fps: int = OUTPUT_FPS) -> int:
    import cv2
    import numpy as np

    decoded: List[Tuple[float, object]] = []
    for stamp, jpeg in frames:
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            decoded.append((stamp, image))
    if not decoded:
        raise RuntimeError("no decodable frames")
    height, width = decoded[0][1].shape[:2]
    width -= width % 2
    height -= height % 2
    tmp = f"{os.path.splitext(path)[0]}.tmp.mp4"
    writer = None
    for codec in ("avc1", "H264", "mp4v"):
        candidate = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*codec), fps, (width, height))
        if candidate.isOpened():
            writer = candidate
            break
        candidate.release()
    if writer is None:
        raise RuntimeError("unable to initialize mp4 writer")
    count = 0
    try:
        start, end = decoded[0][0], decoded[-1][0]
        idx = 0
        total = max(1, int((end - start) * fps) + 1)
        for n in range(total):  # 按时间戳重采样：静止画面重复上一帧
            t = start + n / fps
            while idx + 1 < len(decoded) and decoded[idx + 1][0] <= t:
                idx += 1
            image = decoded[idx][1]
            if image.shape[0] != height or image.shape[1] != width:
                image = cv2.resize(image, (width, height))
            writer.write(image)
            count += 1
    finally:
        writer.release()
    os.replace(tmp, path)
    return count
