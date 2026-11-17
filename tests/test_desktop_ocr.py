"""Real offline OCR and the same extraction paths used by desktop device steps."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import socket
import sys

import cv2
import numpy as np
from PIL import Image
import pytest

from engine.core.ocr_service import OCRService
from engine.core.utils import desktop_ocr
from engine.core.utils.ocr_compat import extract_ocr_text, iter_ocr_text_items, run_paddle_ocr

FIXTURE = Path(__file__).parent / "fixtures/ocr/refund.png"


@pytest.fixture(scope="module")
def offline_engine():
    from rapidocr.utils import DownloadFile

    def no_network(*args, **kwargs):
        pytest.fail("Desktop OCR must initialize and recognize entirely offline")

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "paddleocr", None)
        patch.setitem(sys.modules, "paddle", None)
        patch.setattr(socket.socket, "connect", no_network)
        patch.setattr(DownloadFile, "run", no_network)
        yield desktop_ocr.create_ocr_engine()


def test_offline_chinese_english_number_and_boxes(offline_engine):
    result = run_paddle_ocr(offline_engine, cv2.imread(str(FIXTURE)))
    text = extract_ocr_text(result)
    assert "退款金额" in text
    assert "123.45" in text
    assert "Order AB123" in text
    items = iter_ocr_text_items(result)
    assert len(items) == 2
    for item in items:
        assert 0.5 <= item["score"] <= 1
        assert len(item["box"]) == 4
        assert all(0 <= x <= 620 and 0 <= y <= 200 for x, y in item["box"])


def test_blank_image_is_an_empty_result(offline_engine):
    result = run_paddle_ocr(offline_engine, np.full((100, 480, 3), 255, dtype=np.uint8))
    assert iter_ocr_text_items(result) == []
    assert extract_ocr_text(result) == ""


def test_android_region_and_regex_extraction_use_bundled_engine(offline_engine, monkeypatch):
    from engine.core.drivers import cross_platform_runner
    from engine.core.drivers.android_driver import AndroidDriver

    monkeypatch.setattr(OCRService, "_engine", offline_engine)
    driver = AndroidDriver.__new__(AndroidDriver)
    driver.device_id = "offline-test"
    monkeypatch.setattr(cross_platform_runner, "is_driver_pool_enabled", lambda: False)
    monkeypatch.setattr(cross_platform_runner.DriverFactory, "create", Mock(return_value=driver))
    runner = cross_platform_runner.TestCaseRunner(platform="android", device_id="offline-test")
    with Image.open(FIXTURE) as image:
        driver._device = SimpleNamespace(screenshot=Mock(return_value=image))
        result = runner.run_step({
            "action": "extract_by_ocr", "timeout": 10,
            "args": {
                "region": "[0, 0, 1, 0.5]", "output_var": "refund_amount",
                "extract_rule": {"extract_rule": "regex", "custom_regex": r"退款金额\s*(\d+\.\d+)"},
            },
        })
    assert result["status"] == "PASS", result
    assert runner.runtime_variables["refund_amount"] == "123.45"


def test_ios_uses_the_same_text_and_box_contract(offline_engine, monkeypatch):
    from engine.core.drivers.ios.vision import IOSVisionMixin

    monkeypatch.setattr(OCRService, "_engine", offline_engine)
    assert "退款金额" in IOSVisionMixin()._extract_text_from_image(cv2.imread(str(FIXTURE)))


def test_shared_engine_handles_concurrent_recognition(offline_engine):
    image = cv2.imread(str(FIXTURE))
    with ThreadPoolExecutor(max_workers=3) as pool:
        texts = list(pool.map(lambda _: extract_ocr_text(run_paddle_ocr(offline_engine, image)), range(3)))
    assert all("123.45" in text and "退款金额" in text for text in texts)


def test_missing_bundled_model_fails_without_downloading(offline_engine, monkeypatch, tmp_path):
    import rapidocr

    monkeypatch.setattr(rapidocr, "__file__", str(tmp_path / "__init__.py"))
    with pytest.raises(FileNotFoundError, match="内置 OCR 资源缺失"):
        desktop_ocr.create_ocr_engine()


@pytest.mark.parametrize("frozen,expected", [(True, "完整客户端"), (False, "requirements.txt")])
def test_initialization_error_preserves_cause_and_retries(monkeypatch, frozen, expected):
    cause = ImportError("ONNX shared library missing")
    recovered = object()
    factory = Mock(side_effect=[cause, recovered])
    monkeypatch.setattr(OCRService, "_engine", None)
    monkeypatch.setattr(OCRService, "_initialization_time", None)
    monkeypatch.setattr(desktop_ocr, "create_ocr_engine", factory)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    with pytest.raises(RuntimeError, match=expected) as error:
        OCRService.get_engine()
    assert error.value.__cause__ is cause
    assert "ONNX shared library missing" in str(error.value)
    assert "安装 paddleocr" not in str(error.value)
    assert not OCRService.is_initialized()
    assert OCRService.get_engine() is recovered
    assert OCRService.get_engine() is recovered
    assert factory.call_count == 2
