"""OCR exhaustion preserves its structured error instead of raising TypeError."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from engine.core.drivers import cross_platform_runner
from engine.core.execution_errors import E2006_OCR_NO_RESULT, ExecutionStepRuntimeError


@pytest.mark.parametrize("raises", [False, True])
def test_empty_ocr_exhaustion_preserves_error_code_and_context(monkeypatch, raises):
    clock = [0.0]
    monkeypatch.setattr(cross_platform_runner, "time", SimpleNamespace(time=lambda: clock[0]))
    driver = SimpleNamespace(extract_by_ocr=Mock(return_value=""))
    if raises:
        driver.extract_by_ocr.side_effect = RuntimeError("extract_by_ocr 未识别到文本")
    runner = cross_platform_runner.TestCaseRunner.__new__(cross_platform_runner.TestCaseRunner)
    runner.driver = driver

    def advance(seconds):
        clock[0] += seconds
        return False

    runner._sleep_or_abort = advance
    region = "[0.1, 0.2, 0.5, 0.6]"
    with pytest.raises(ExecutionStepRuntimeError) as raised:
        runner._extract_by_ocr_with_retry(region=region, extract_rule={}, timeout=1)
    assert raised.value.code == E2006_OCR_NO_RESULT
    assert "未识别到文本" in str(raised.value)
    assert raised.value.context == {
        "action": "extract_by_ocr", "region": region, "timeout": 1,
        "attempts": driver.extract_by_ocr.call_count,
    }
    assert driver.extract_by_ocr.call_count > 1
    assert isinstance(raised.value.__cause__, RuntimeError)
