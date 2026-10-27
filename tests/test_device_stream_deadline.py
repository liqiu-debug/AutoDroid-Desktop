"""视频生成器无帧总 deadline 测试（C4：next() 不再永久阻塞）。

背景：断流/卡死时 get_video_generator 原先会无限空转，消费端 next()
永远不返回 → 占死线程池 → 本地设备 I/O 全体饿死。加 15s（可配）无帧
deadline 后生成器必须结束，消费端能收尾。
"""
from __future__ import annotations

import threading
import time

import pytest

from engine.core.device_stream.manager import (
    VIDEO_STALL_TIMEOUT_SEC,
    DeviceInfo,
    ScrcpyDeviceManager,
    _broadcast_video_packet,
    _video_stall_timeout,
)


def _make_ready_manager(serial: str = "USB1"):
    """构造带 ready 设备的管理器（不起 scrcpy，不进 _devices 的真实连接路径）。"""
    mgr = ScrcpyDeviceManager()
    mgr._running = True
    dev = DeviceInfo(serial, local_port=27183)
    dev.ready = True
    mgr._devices[serial] = dev
    return mgr, dev


def test_video_generator_ends_after_stall_deadline(monkeypatch):
    """无帧时按 deadline 结束（不再永久阻塞 next()）。"""
    monkeypatch.setenv("AUTODROID_SCRCPY_STALL_TIMEOUT", "0.3")
    mgr, dev = _make_ready_manager()
    gen = mgr.get_video_generator("USB1")
    started = time.monotonic()
    packets = list(gen)  # 无任何帧：应约 0.3s 后结束
    elapsed = time.monotonic() - started
    assert packets == []
    assert elapsed >= 0.25, f"过早结束: {elapsed:.3f}s"
    assert elapsed < 2.0, f"deadline 未生效，仍在阻塞: {elapsed:.3f}s"
    # 生成器退出后客户端队列必须摘除（不泄漏广播列表）
    assert dev.input_queues == []


def test_video_generator_frames_reset_deadline_then_stall_ends(monkeypatch):
    """持续有帧时不受 deadline 影响；停帧后按 deadline 结束。"""
    monkeypatch.setenv("AUTODROID_SCRCPY_STALL_TIMEOUT", "0.4")
    mgr, dev = _make_ready_manager()
    gen = mgr.get_video_generator("USB1")

    received = []

    def _consume():
        for packet in gen:
            received.append(packet)

    worker = threading.Thread(target=_consume, daemon=True)
    worker.start()

    # 等生成器把客户端队列挂上广播列表（首次 next() 时才进入函数体）
    deadline = time.monotonic() + 2.0
    while not dev.input_queues and time.monotonic() < deadline:
        time.sleep(0.01)
    assert dev.input_queues, "客户端队列未挂上"

    # 每 0.1s 喂一帧（间隔 < 0.4s deadline）：生成器应持续产出
    for i in range(5):
        _broadcast_video_packet(dev, b"\x00\x00\x00\x01\x65" + bytes([i]))
        time.sleep(0.1)
    assert len(received) >= 3, f"有帧期间不应被 deadline 掐断: {len(received)}"

    # 停止喂帧 → deadline 后消费线程必须退出（生成器结束）
    worker.join(timeout=2.5)
    assert not worker.is_alive(), "无帧后生成器未按 deadline 结束"
    assert dev.input_queues == []


def test_video_generator_not_ready_raises_on_first_next():
    """未就绪设备：首个 next() 抛 ValueError（生成器函数体惰性执行）。"""
    mgr = ScrcpyDeviceManager()
    mgr._running = True
    gen = mgr.get_video_generator("NOPE")
    with pytest.raises(ValueError, match="未就绪或不存在"):
        next(gen)


def test_video_stall_timeout_default_and_env(monkeypatch):
    monkeypatch.delenv("AUTODROID_SCRCPY_STALL_TIMEOUT", raising=False)
    assert _video_stall_timeout() == VIDEO_STALL_TIMEOUT_SEC == 15.0

    monkeypatch.setenv("AUTODROID_SCRCPY_STALL_TIMEOUT", "0.5")
    assert _video_stall_timeout() == 0.5

    # 非法值回退默认
    monkeypatch.setenv("AUTODROID_SCRCPY_STALL_TIMEOUT", "abc")
    assert _video_stall_timeout() == VIDEO_STALL_TIMEOUT_SEC
    monkeypatch.setenv("AUTODROID_SCRCPY_STALL_TIMEOUT", "-1")
    assert _video_stall_timeout() == VIDEO_STALL_TIMEOUT_SEC
