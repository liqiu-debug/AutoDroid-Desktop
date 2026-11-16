"""Desktop runner preserves image evidence and never replays a submitted tap."""
import copy
from pathlib import Path
from unittest.mock import patch, Mock

import pytest

from engine.core.drivers.cross_platform_runner import DriverFactory, TestCaseRunner as CaseRunner
from engine.core.execution_errors import ExecutionStepRuntimeError
from engine.core.step_contract import legacy_step_to_standard, standard_step_to_legacy
from engine.core import recording_service
from engine.core.cloud_bridge import CloudBridge


CONFIG = {"template_sha256": "a" * 64, "tap_anchor": [0.1, 0.9], "source_size": [1080, 2400]}


class ImageDriver:
    def __init__(self):
        self.calls = []
        self.error = None
        self.assertion_calls = []

    def click_image(self, path, image_options=None, timeout=10):
        self.calls.append((path, image_options, timeout))
        if self.error:
            raise self.error
        return {"image_match": {"status": "matched", "point": [10, 20]}}

    def assert_image(self, path, match_mode="exists", image_options=None, timeout=10, abort_event=None):
        self.assertion_calls.append((path, image_options, match_mode))
        return {"image_match": {"status": "matched" if match_mode == "exists" else "not_found"}}

    def disconnect(self):
        pass


def runner(driver):
    with patch("engine.core.drivers.cross_platform_runner.is_driver_pool_enabled", return_value=False), patch.object(DriverFactory, "create", return_value=driver):
        return CaseRunner(platform="android", device_id="offline")


def test_metadata_round_trip_and_selected_platform_path():
    step = {"action": "click_image", "timeout": 6, "args": {"image_path": "global.png", "image_options": {"android": CONFIG}},
            "platform_overrides": {"android": {"by": "image", "selector": "android.png"}}}
    restored = legacy_step_to_standard(standard_step_to_legacy(step), 1, 1)
    assert restored["args"]["image_options"]["android"] == CONFIG
    driver = ImageDriver()
    result = runner(driver).run_step(step)
    assert result["status"] == "PASS"
    assert driver.calls[0][:2] == ("android.png", CONFIG)
    assert 5.9 < driver.calls[0][2] <= 6
    assert result["output"]["image_match"]["status"] == "matched"


def test_invalid_inactive_metadata_is_not_ignored():
    driver = ImageDriver()
    result = runner(driver).run_step({"action": "click_image", "args": {"image_path": "global.png", "image_options": {"android": CONFIG, "ios": None}}})
    assert result["error_code"] == "P1006_INVALID_ARGS"
    assert driver.calls == []


def test_failed_tap_evidence_survives_without_retry():
    driver = ImageDriver()
    diagnostic = {"status": "matched", "reason": "tap_failed", "tap_submitted": True}
    driver.error = ExecutionStepRuntimeError("E2009_CLICK_NO_EFFECT", "tap failed", context={"image_match": diagnostic})
    result = runner(driver).run_step({"action": "click_image", "retry_count": 3, "args": {"image_path": "old.png"}})
    assert result["attempts"] == 1
    assert len(driver.calls) == 1
    assert result["output"]["image_match"] == diagnostic


@pytest.mark.parametrize("platform", ["android", "ios"])
@pytest.mark.parametrize("action,match_mode", [("click_image", "exists"), ("assert_image", "exists"), ("assert_image", "not_exists")])
def test_recorded_platform_template_reaches_driver_from_user_cache(tmp_path, monkeypatch, platform, action, match_mode):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path))
    templates = tmp_path / "templates"
    templates.mkdir()
    cached = templates / "element_17dde1983ee1d797.png"
    cached.write_bytes(b"cached template")
    selected = "static/images/" + cached.name
    # The inactive and generic references deliberately do not exist locally.
    step = {"action": action, "args": {"image_path": "static/images/generic.png", "image_options": {platform: CONFIG}, "match_mode": match_mode},
            "platform_overrides": {platform: {"selector": selected, "by": "image"},
                                   ("ios" if platform == "android" else "android"): {"selector": "static/images/other.png", "by": "image"}}}
    original = copy.deepcopy(step)
    driver = ImageDriver()
    monkeypatch.setattr(recording_service, "resolve_device_platform", lambda serial: platform)
    monkeypatch.setattr(recording_service.ios_device, "execution_wda_url", lambda *a, **kw: "http://offline.invalid")
    monkeypatch.setattr(recording_service, "_post_action_dump_payload", lambda *a, **kw: {})
    with patch("engine.core.drivers.cross_platform_runner.is_driver_pool_enabled", return_value=False), patch.object(DriverFactory, "create", return_value=driver):
        result = recording_service.execute_single_step(step, device_serial="offline", ensure_local=CloudBridge().ensure_template_local)
    assert result["result"]["success"], result
    calls = driver.assertion_calls if action == "assert_image" else driver.calls
    assert calls[0][0] == str(cached)
    assert Path(calls[0][0]).read_bytes() == b"cached template"
    assert calls[0][1] == CONFIG
    if action == "assert_image":
        assert calls[0][2] == match_mode
        assert driver.calls == [], "assertions must never click"
    assert step == original, "local execution must not persist machine-specific paths"


@pytest.mark.parametrize("action", ["click_image", "assert_image"])
def test_missing_template_stops_before_driver_creation(monkeypatch, action):
    factory = Mock(side_effect=AssertionError("must not connect a device"))
    monkeypatch.setattr(recording_service, "CrossPlatformRunner", factory)
    with pytest.raises(recording_service.RecordingError, match="模板下载或缓存读取失败") as error:
        recording_service.execute_single_step(
            {"action": action, "args": {"image_path": "static/images/missing.png", "match_mode": "not_exists"}},
            device_serial="offline", ensure_local=Mock(side_effect=FileNotFoundError("missing")))
    assert error.value.status_code == 502
    factory.assert_not_called()


def test_platform_skipped_image_does_not_download_an_unused_template():
    ensure = Mock(side_effect=AssertionError("skipped template must not be downloaded"))
    step = {"action": "click_image", "execute_on": ["ios"], "args": {"image_path": "missing.png"}}
    assert recording_service.localize_step_images(step, ensure, "android") == step
    ensure.assert_not_called()
