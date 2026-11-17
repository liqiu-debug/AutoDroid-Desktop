"""Offline desktop OCR, using bundled PP-OCR models through ONNX Runtime."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


def bundled_ocr_paths() -> dict[str, str]:
    import rapidocr

    root = Path(rapidocr.__file__).resolve().parent
    resources = {
        "Det.model_path": root / "models/ch_PP-OCRv4_det_infer.onnx",
        "Cls.model_path": root / "models/ch_ppocr_mobile_v2.0_cls_infer.onnx",
        "Rec.model_path": root / "models/ch_PP-OCRv4_rec_infer.onnx",
        "Rec.rec_keys_path": root / "models/ppocr_keys_v1.txt",
    }
    # Explicit paths prevent first-run downloads or writes inside the installed app.
    for path in [root / "config.yaml", root / "default_models.yaml", *resources.values()]:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"内置 OCR 资源缺失: {path.name}")
    return {key: str(path) for key, path in resources.items()}


class DesktopOCREngine:
    """Expose the driver's existing ocr() contract with normalized text/box results."""

    def __init__(self, *, use_angle_cls: bool = False, lang: str = "ch") -> None:
        if lang not in ("ch", "en"):
            raise ValueError(f"内置 OCR 暂不支持语言: {lang}")
        from rapidocr import RapidOCR

        self._lock = threading.Lock()
        self._engine = RapidOCR(params={
            **bundled_ocr_paths(),
            "Global.with_onnx": True,
            "Global.use_cls": bool(use_angle_cls),
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })

    def ocr(self, image: Any, cls: bool = False) -> list[dict[str, Any]]:
        # RapidOCR mutates per-call state; prewarm and concurrent devices share this instance.
        with self._lock:
            result = self._engine(image, use_cls=bool(cls))
            if result.txts is None:
                return []
            return [
                {"text": text, "score": float(score), "box": box.tolist()}
                for text, score, box in zip(result.txts, result.scores, result.boxes)
            ]


def create_ocr_engine(*, use_angle_cls: bool = False, lang: str = "ch") -> DesktopOCREngine:
    return DesktopOCREngine(use_angle_cls=use_angle_cls, lang=lang)


def check_ocr() -> dict[str, Any]:
    """Exercise real inference in the frozen executable without a device or server."""
    import cv2
    import numpy as np

    from engine.core.ocr_service import get_ocr_engine
    from engine.core.utils.ocr_compat import extract_ocr_text, run_paddle_ocr

    sample = np.full((100, 480, 3), 255, dtype=np.uint8)
    cv2.putText(sample, "OCR 123.45", (20, 65), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 2)
    text = extract_ocr_text(run_paddle_ocr(get_ocr_engine(), sample))
    if "123.45" not in text:
        raise RuntimeError(f"OCR 自检未识别到预期数字: {text!r}")
    return {"ok": True, "backend": "rapidocr-onnxruntime", "text": text}
