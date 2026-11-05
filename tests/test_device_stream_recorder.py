"""异常回放录制器测试：帧数统计与回放时长（变帧率码流不得被固定 fps 快放）。

背景（2026-11-04 真机实测）：OPPO PKZ110 上 32 秒回放窗口里 scrcpy 只送到
约 130 帧（画面不动就不发帧），cv2 按猜出来的 25fps 写盘 → 回放只有 5 秒，
等于快进。录制器现在按「窗口内实际帧数 / 窗口时长」定 fps。
"""
from __future__ import annotations

import time

import pytest

from engine.core.device_stream import recorder as device_recorder
from engine.core.device_stream.recorder import (
    DEFAULT_REPLAY_FPS,
    RollingScrcpyRecorderSession,
    _count_h264_frames,
    _resolve_replay_fps,
)

# 一个最小可辨识的 Annex-B 包：SPS + PPS + IDR（= 1 帧）
_IDR_PACKET = (
    b"\x00\x00\x00\x01\x67\x42\x00\x1e\x95\xa8\x14\x01\x6e\x9b\x80\x80\x80\xa0"
    b"\x00\x00\x00\x01\x68\xce\x3c\x80"
    b"\x00\x00\x00\x01\x65\x88\x84\x00\x10\xff\xfe\xf4\xff\x00\x10\x00\x00\x03"
)
_P_PACKET = b"\x00\x00\x00\x01\x41\x9a\x00\x11\x22\x33"


def test_count_h264_frames_counts_vcl_nals():
    assert _count_h264_frames(_IDR_PACKET) == 1
    assert _count_h264_frames(_P_PACKET) == 1
    assert _count_h264_frames(_IDR_PACKET + _P_PACKET + _P_PACKET) == 3
    # 只有参数集（SPS/PPS/SEI）没有 VCL 的包不算帧
    assert _count_h264_frames(b"\x00\x00\x00\x01\x67\x42\x00\x1e\x95\xa8") == 0
    assert _count_h264_frames(b"") == 0


def test_resolve_replay_fps_prefers_measured_window_rate():
    # 130 帧覆盖 32 秒窗口 → 约 4 fps（时长对齐窗口，而不是 5 秒快进）
    assert _resolve_replay_fps(25.0, 130, 32) == pytest.approx(4.0625)
    # 帧率正常时同样按实测走（60fps 码流写 60fps，不做慢放）
    assert _resolve_replay_fps(25.0, 2100, 35) == pytest.approx(60.0)


def test_resolve_replay_fps_falls_back_when_window_unknown():
    assert _resolve_replay_fps(30.0, 0, 0) == 30.0
    assert _resolve_replay_fps(25.0, 1, 30) == 25.0
    # 无法解析的容器 fps → 固定兜底值
    assert _resolve_replay_fps(0.0, 0, 0) == DEFAULT_REPLAY_FPS
    assert _resolve_replay_fps(float("nan"), 5, 0.2) == DEFAULT_REPLAY_FPS
    # 实测值离谱（解码器报错/窗口极短）时不采用
    assert _resolve_replay_fps(25.0, 100000, 30) == 25.0


def _session(tmp_path) -> RollingScrcpyRecorderSession:
    session = RollingScrcpyRecorderSession(
        serial="USB1",
        task_id=9,
        report_dir=str(tmp_path / "reports"),
        project_root=str(tmp_path),
        pre_roll_sec=30,
        post_roll_sec=0,
        segment_sec=5,
    )
    session.seed_init_packets([_IDR_PACKET])
    return session


def test_capture_replay_passes_window_frames_and_duration(tmp_path, monkeypatch):
    """截取时把窗口帧数与窗口时长交给转封装（真机 fps 修正的输入）。"""
    session = _session(tmp_path)
    try:
        # 10 帧分布在 3 个 5s 分段里，末段仍在写入（截取前会被冻结）
        session.ingest(_IDR_PACKET)
        for _ in range(9):
            session.ingest(_P_PACKET)
        calls = []
        monkeypatch.setattr(
            device_recorder,
            "transcode_h264_to_mp4",
            lambda source, output, **kwargs: calls.append((source, output, kwargs)) or 10,
        )
        result = session.capture_replay("CRASH", "12:00:00")

        assert result.status == "READY", result.error
        assert result.filename.endswith(".mp4")
        assert len(calls) == 1
        _, output_path, kwargs = calls[0]
        assert output_path.endswith(result.filename)
        assert kwargs["expected_frames"] == 10
        assert 1 <= kwargs["expected_duration_sec"] <= 6
        # 截取后源文件（中间产物）不留痕
        assert not list(tmp_path.rglob("*.source.h264"))
    finally:
        session.stop()


def test_capture_replay_reports_unavailable_without_buffered_data(tmp_path, monkeypatch):
    session = _session(tmp_path)
    try:
        monkeypatch.setattr(
            device_recorder, "transcode_h264_to_mp4", lambda *a, **k: 0
        )
        session.ingest(_IDR_PACKET)  # 有数据，但下面手动清空缓冲模拟空窗口
        session._segments.clear()
        session._current_segment = None
        result = session.capture_replay("CRASH", "12:00:00")
        assert result.status == "UNAVAILABLE"
        assert "no buffered video data" in result.error
    finally:
        session.stop()


def test_ingest_counts_frames_per_segment(tmp_path):
    session = _session(tmp_path)
    try:
        for _ in range(5):
            session.ingest(_IDR_PACKET)
            time.sleep(0.001)
        total = sum(segment.frame_count for segment in session._segments)
        assert total == 5
    finally:
        session.stop()
