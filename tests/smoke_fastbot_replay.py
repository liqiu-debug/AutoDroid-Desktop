"""真机冒烟：验证「异常回放」录制链路（scrcpy 取流 → 窗口截取 → mp4 转封装）。

用途：改了 `engine/core/device_stream/recorder.py` 或 `fastbot_service` 的回放接线后，
在真机上确认「能截出可播放、时长与窗口一致的 mp4」，不必真跑一次带崩溃的稳定性。

    cd AutoDroid-Desktop
    AUTODROID_ENGINE_ASSETS_DIR=$PWD/assets .venv/bin/python tests/smoke_fastbot_replay.py [--animate]

注意：本脚本会启动自己的设备流（含设备侧 scrcpy 重启），**跑之前先退出客户端 App**，
否则两边抢同一个 `localabstract:scrcpy`，客户端投屏会断（重连即可恢复）。
只做只读录制，不改设备状态。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.adb_scan import resolve_adb_binary, scan_devices  # noqa: E402
from engine.core.device_stream.manager import device_manager  # noqa: E402

PRE_ROLL_SEC, POST_ROLL_SEC, SEGMENT_SEC = 30, 5, 5
READY_TIMEOUT_SEC = 40
BUFFER_FILL_SEC = 40  # 让环形缓冲至少装下一个完整窗口
MIN_FRAMES = 30


def _animate_loop(serial: str, stop: threading.Event) -> None:
    """持续滑动屏幕，模拟被测应用画面在动。"""
    adb = resolve_adb_binary()
    while not stop.is_set():
        subprocess.run(
            [adb, "-s", serial, "shell", "input", "swipe", "640", "1600", "640", "900", "120"],
            capture_output=True,
        )
        time.sleep(0.05)


def _wait_ready(serial: str) -> dict:
    deadline = time.time() + READY_TIMEOUT_SEC
    info = None
    while time.time() < deadline:
        info = device_manager.get_device(serial) or info
        if info and info.get("ready"):
            return info
        time.sleep(1)
    raise RuntimeError(f"设备流未就绪: {(info or {}).get('error')}")


def main() -> int:
    args = [item for item in sys.argv[1:] if not item.startswith("--")]
    serial = args[0] if args else scan_devices()[0]["serial"]
    report_dir = Path(tempfile.mkdtemp(prefix="autodroid-replay-smoke-"))
    result: dict = {"serial": serial, "report_dir": str(report_dir)}
    stop_animate = threading.Event()
    try:
        device_manager.start_tracking()
        info = _wait_ready(serial)
        result["device"] = {
            "name": info.get("device_name"),
            "resolution": f"{info.get('screen_width')}x{info.get('screen_height')}",
        }

        device_manager.start_recording(
            serial, 999999, str(report_dir), PRE_ROLL_SEC, POST_ROLL_SEC, SEGMENT_SEC
        )
        result["recording_started"] = True
        if "--animate" in sys.argv:
            threading.Thread(
                target=_animate_loop, args=(serial, stop_animate), daemon=True
            ).start()
        time.sleep(BUFFER_FILL_SEC)

        capture = device_manager.capture_replay(serial, "CRASH", "12:00:00")
        stop_animate.set()
        result["capture"] = capture.to_dict() if capture else None
        replay_path = None
        if capture and capture.filename:
            replay_path = report_dir / "replays" / capture.filename
        if replay_path and replay_path.is_file():
            result["output_bytes"] = replay_path.stat().st_size
            import cv2

            cap = cv2.VideoCapture(str(replay_path))
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            result["video"] = {
                "frames": frames,
                "fps": round(fps, 3),
                "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "seconds": round(frames / fps, 2) if fps else None,
                "first_frame_ok": bool(cap.read()[0]),
            }
            cap.release()
    finally:
        stop_animate.set()
        try:
            device_manager.stop_recording(serial)
        except Exception as exc:  # pragma: no cover - 冒烟收尾
            result["stop_error"] = str(exc)
        buffer_dir = report_dir / "replays" / ".buffer"
        replay_buffer_left = buffer_dir.is_dir() and any(buffer_dir.iterdir())
        result["buffer_left_behind"] = replay_buffer_left
        device_manager.stop_tracking()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    video = result.get("video") or {}
    window = PRE_ROLL_SEC + POST_ROLL_SEC
    ok = (
        bool(result.get("capture", {}).get("status") == "READY")
        and video.get("frames", 0) >= MIN_FRAMES
        and bool(video.get("first_frame_ok"))
        and not result.get("buffer_left_behind")
        # 时长与窗口一致（变帧率码流不得被固定 fps 快放/慢放）
        and video.get("seconds") is not None
        and abs(float(video["seconds"]) - window) <= max(5.0, window * 0.3)
    )
    if not ok:
        print("\n校验未通过：期望 READY + 可解码帧 ≥ %d + 时长≈%ds + 缓冲清理干净" % (MIN_FRAMES, window))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
