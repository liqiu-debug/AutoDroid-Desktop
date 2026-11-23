"""Desktop runner preserves image evidence and never replays a submitted tap."""
import copy
from pathlib import Path
from unittest.mock import patch, Mock

import pytest


@pytest.fixture(autouse=True)
def isolate_descriptor_analysis_in_contract_tests(monkeypatch):
    # These contracts exercise metadata/path selection, not image decoding.
    monkeypatch.setattr("engine.core.utils.adaptive_template.ensure_descriptor",
                        lambda raw, config=None, **kwargs: config or {"profile": "adaptive-v2"})
    original_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: original_read(self) if self.exists() else b"cached")

from engine.core.drivers.cross_platform_runner import DriverFactory, TestCaseRunner as CaseRunner
from engine.core.execution_errors import ExecutionStepRuntimeError
from engine.core.step_contract import legacy_step_to_standard, standard_step_to_legacy
from engine.core import recording_service
from engine.core.cloud_bridge import CloudBridge
from engine.core.execution_service import prepare_steps_for_platform
from engine.core.utils.image_match_options import preserve_image_references


CONFIG = {"template_sha256": "a" * 64, "tap_anchor": [0.1, 0.9], "source_size": [1080, 2400]}


class ImageDriver:
    def __init__(self):
        self.calls = []
        self.error = None
        self.assertion_calls = []

    def click_image(self, path, image_options=None, timeout=10, image_match=None):
        self.calls.append((path, image_options, timeout))
        if self.error:
            raise self.error
        return {"image_match": {"status": "matched", "point": [10, 20]}}

    def assert_image(self, path, match_mode="exists", image_options=None, timeout=10, abort_event=None, image_match=None):
        self.assertion_calls.append((path, image_options, match_mode))
        return {"image_match": {"status": "matched" if match_mode == "exists" else "not_found"}}

    def disconnect(self):
        pass


def runner(driver, platform="android"):
    with patch("engine.core.drivers.cross_platform_runner.is_driver_pool_enabled", return_value=False), patch.object(DriverFactory, "create", return_value=driver):
        return CaseRunner(platform=platform, device_id="offline", wda_url="http://offline.invalid")


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


@pytest.mark.parametrize("platform,other", [("android", "ios"), ("ios", "android")])
@pytest.mark.parametrize("action", ["click_image", "assert_image"])
def test_single_platform_recording_borrows_template_binding_without_screen_geometry(platform, other, action):
    original = {**CONFIG, "search_region": [0, 0, 0.5, 0.5]}
    step = {"action": action, "args": {"image_path": "recorded.png", "image_options": {other: original}},
            "platform_overrides": {other: {"by": "image", "selector": "recorded.png"}}}
    driver = ImageDriver()
    result = runner(driver, platform).run_step(step)
    assert result["status"] == "PASS", result
    calls = driver.calls if action == "click_image" else driver.assertion_calls
    assert calls[0][:2] == ("recorded.png", {"template_sha256": CONFIG["template_sha256"], "tap_anchor": CONFIG["tap_anchor"]})
    assert step["args"]["image_options"][other] == original


def test_recropped_ios_preserves_unbound_legacy_android_template():
    step = {"action": "click_image", "args": {"image_path": "android-old.png", "image_options": {"ios": CONFIG}},
            "platform_overrides": {"android": {"by": "image", "selector": "android-old.png"},
                                   "ios": {"by": "image", "selector": "ios-new.png"}}}
    driver = ImageDriver()
    result = runner(driver).run_step(step)
    assert result["status"] == "PASS", result
    assert driver.calls[0][:2] == ("android-old.png", {})


@pytest.mark.parametrize("platform,other", [("android", "ios"), ("ios", "android")])
def test_localized_single_platform_template_keeps_its_fingerprint_and_anchor(tmp_path, platform, other):
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"cached")
    step = {"action": "click_image", "args": {"image_path": "static/images/recorded.png", "image_options": {other: CONFIG}},
            "platform_overrides": {other: {"selector": "static/images/recorded.png", "by": "image"}}}
    original = copy.deepcopy(step)
    localized = recording_service.localize_step_images(step, lambda _: str(cached), platform)
    assert localized["platform_overrides"][other]["selector"] == str(cached)
    driver = ImageDriver()
    assert runner(driver, platform).run_step(localized)["status"] == "PASS"
    assert driver.calls[0][:2] == (str(cached), {"template_sha256": CONFIG["template_sha256"], "tap_anchor": CONFIG["tap_anchor"]})
    assert step == original


def test_localization_keeps_an_independent_other_platform_template(tmp_path):
    step = {"action": "click_image", "args": {"image_path": "android.png", "image_options": {"ios": CONFIG}},
            "platform_overrides": {"android": {"by": "image", "selector": "android.png"},
                                   "ios": {"by": "image", "selector": "ios.png"}}}
    localized = recording_service.localize_step_images(step, lambda _: str(tmp_path / "android.png"), "android")
    assert localized["platform_overrides"]["ios"]["selector"] == "ios.png"
    driver = ImageDriver()
    assert runner(driver).run_step(localized)["status"] == "PASS"
    assert driver.calls[0][1] == {}


@pytest.mark.parametrize("platform,donor", [("android", "ios"), ("ios", "android")])
@pytest.mark.parametrize("action", ["click_image", "assert_image"])
@pytest.mark.parametrize("key", ["image_path", "path"])
def test_preparation_and_localization_preserve_an_implicit_donor_template(tmp_path, platform, donor, action, key):
    step = {"action": action, "args": {key: "donor-old.png", "image_options": {donor: CONFIG}},
            "platform_overrides": {platform: {"by": "image", "selector": "independent.png"}}}
    original = copy.deepcopy(step)
    prepared = prepare_steps_for_platform([step], platform)[0]
    assert prepared["platform_overrides"][donor] == {"by": "image", "selector": "donor-old.png"}
    cached = str(tmp_path / "independent.png")
    # Both direct single-step localization and the prepare-then-localize batch
    # path must retain the donor's original identity.
    for source in (step, prepared):
        localized = recording_service.localize_step_images(source, lambda _: cached, platform)
        assert localized["platform_overrides"][donor]["selector"] == "donor-old.png"
        driver = ImageDriver()
        result = runner(driver, platform).run_step(localized)
        assert result["status"] == "PASS", result
        calls = driver.calls if action == "click_image" else driver.assertion_calls
        assert calls[0][:2] == (cached, {})
    assert step == original


def test_reference_preservation_does_not_replace_an_existing_nonimage_override():
    step = {"action": "click_image", "args": {"path": "legacy.png", "image_options": {"ios": CONFIG}},
            "platform_overrides": {"ios": {"by": "text", "selector": "existing-label"}}}
    original = copy.deepcopy(step)
    result = preserve_image_references(step, step["args"])
    assert result["platform_overrides"]["ios"] == original["platform_overrides"]["ios"]
    assert step == original


@pytest.mark.parametrize("platform,donor", [("android", "ios"), ("ios", "android")])
@pytest.mark.parametrize("action", ["click_image", "assert_image"])
@pytest.mark.parametrize("reference", ["path", "selector"])
def test_runtime_variables_preserve_a_borrowed_noncenter_anchor(platform, donor, action, reference):
    step = {"action": action, "args": {"image_options": {donor: CONFIG}}}
    if reference == "path":
        step["args"]["path"] = "{{IMAGE}}"
        step["platform_overrides"] = {donor: {"by": "image", "selector": "{{IMAGE}}"}}
    else:
        step["selector"], step["selector_type"] = "{{IMAGE}}", "image"
    driver = ImageDriver()
    case_runner = runner(driver, platform)
    case_runner.runtime_variables = {"IMAGE": "static/images/same.png"}
    result = case_runner.run_step(step)
    assert result["status"] == "PASS", result
    calls = driver.calls if action == "click_image" else driver.assertion_calls
    assert calls[0][:2] == ("static/images/same.png", {"template_sha256": CONFIG["template_sha256"], "tap_anchor": CONFIG["tap_anchor"]})


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
