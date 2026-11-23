"""Offline frozen-engine check of shared-template analysis and execution.

Read fixture pixels, then exercise the production two-frame locator and image
assertion. No driver, device, gateway, app server or business variable is used.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

from .adaptive_template import ensure_descriptor, ocr_identity
from .image_assert import assert_image_presence
from .image_click import locate_stable_image
from .template_match import image_to_bgr


def check_image_match(fixtures_dir: str | Path) -> dict:
    directory = Path(fixtures_dir).expanduser().resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"图像自检目录不存在: {directory}")
    started = time.monotonic()
    cases = []
    for report in (156, 161):
        template_path = directory / f"{report}-template.png"
        screen_path = directory / f"{report}-screen.png"
        raw = template_path.read_bytes()
        screen_raw = screen_path.read_bytes()
        for name, data in ((template_path.name, raw), (screen_path.name, screen_raw)):
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError(f"图像自检需要原始 PNG: {name}")
        screen = image_to_bgr(screen_raw, source="screenshot")
        preparation_started = time.monotonic()
        config = ensure_descriptor(raw, deadline=preparation_started + 10)
        preparation_ms = round((time.monotonic() - preparation_started) * 1000, 2)
        captures = 0

        def capture():
            nonlocal captures
            captures += 1
            # Each production screenshot callback receives an independent frame.
            return screen.copy()

        matching_started = time.monotonic()
        if report == 156:
            diagnostic = locate_stable_image(capture, str(template_path), timeout=10, image_match=config)
            diagnostic.pop("_deadline", None)
            if diagnostic.get("stable_frames") != 2 or captures < 2:
                raise RuntimeError("156 图像点击定位未完成稳定双帧验证")
            action = "locate_stable_image"
        else:
            diagnostic = assert_image_presence(capture, str(template_path), "exists", timeout=10, image_match=config)["image_match"]
            action = "assert_image_exists"
        if not any(candidate.get("accepted") for candidate in diagnostic.get("candidates", [])):
            raise RuntimeError(f"{report} 图像自检没有通过严格验证的候选: {diagnostic.get('reason')}")
        if report == 156 and diagnostic.get("status") != "matched":
            raise RuntimeError("156 图像定位未得到唯一目标")
        best = next(candidate for candidate in diagnostic["candidates"] if candidate.get("accepted"))
        cases.append({
            "report": report, "action": action, "status": diagnostic["status"],
            "reason": diagnostic.get("reason"), "matcher_version": diagnostic.get("matcher_version"),
            "point": diagnostic.get("point") or best.get("point"), "rect": diagnostic.get("rect") or best.get("rect"),
            "scale": best.get("scale"), "branch": best.get("branch"),
            "stable_frames": diagnostic.get("stable_frames", 1), "captures": captures,
            "template_sha256": config["template_sha256"],
            "screen_sha256": hashlib.sha256(screen_raw).hexdigest(),
            "descriptor_sha256": config.get("descriptor_sha256"),
            "analysis_ms": preparation_ms,
            "matching_ms": round((time.monotonic() - matching_started) * 1000, 2),
            "timings": diagnostic.get("timings", {}),
            "metrics": best.get("metrics", {}),
            "region_matches": best.get("region_matches", {}),
        })
    return {"ok": True, "profile": "adaptive-v2", "offline": True, "ocr_engine": ocr_identity(),
            "cases": cases, "total_ms": round((time.monotonic() - started) * 1000, 2)}
