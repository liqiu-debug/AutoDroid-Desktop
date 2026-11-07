"""录制服务与模板本地化单元测试（设备层全部 mock，不触真机）。"""
from __future__ import annotations

import base64
import io

import pytest

from engine.core import recording_service
from engine.core.recording_service import RecordingError


def _png_base64(width: int = 40, height: int = 30) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 30)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def test_crop_template_returns_png_bytes():
    image_bytes, name = recording_service.crop_template_image(_png_base64(), 2, 3, 20, 15)
    assert name.startswith("element_") and name.endswith(".png")
    assert image_bytes[:2] == b"\x89P"  # PNG 魔数


def test_crop_template_rejects_invalid_region():
    with pytest.raises(RecordingError) as exc:
        recording_service.crop_template_image(_png_base64(), 10, 10, 5, 20)
    assert exc.value.status_code == 400
    assert "裁剪区域无效" in str(exc.value)


def test_crop_template_rejects_bad_screenshot():
    with pytest.raises(RecordingError) as exc:
        recording_service.crop_template_image("not-base64!!", 0, 0, 5, 5)
    assert exc.value.status_code == 400
    assert "无法解析截图" in str(exc.value)


def test_crop_template_clamps_out_of_bounds():
    # 越界坐标被钳制到图像内（同上游语义）
    image_bytes, _ = recording_service.crop_template_image(_png_base64(10, 10), -5, -5, 100, 100)
    assert image_bytes[:2] == b"\x89P"


def test_localize_step_images_rewrites_relative_template_paths():
    seen = []

    def fake_ensure(path: str) -> str:
        seen.append(path)
        return f"/cache/{path.rsplit('/', 1)[-1]}"

    step = {"action": "click_image", "args": {"image_path": "static/images/element_ab.png"}}
    out = recording_service.localize_step_images(step, fake_ensure)
    assert seen == ["static/images/element_ab.png"]
    assert out["args"]["image_path"] == "/cache/element_ab.png"


def test_localize_step_images_keeps_absolute_paths():
    def fake_ensure(path: str) -> str:  # pragma: no cover - 不应被调用
        raise AssertionError("绝对路径不应触发本地化")

    step = {"action": "click_image", "args": {"image_path": "/already/local.png"}}
    out = recording_service.localize_step_images(step, fake_ensure)
    assert out["args"]["image_path"] == "/already/local.png"


_FAKE_HIERARCHY_XML = (
    '<hierarchy rotation="0"><node bounds="[0,0][100,100]" text="登录" '
    'resource-id="com.demo:id/login" class="android.widget.Button" content-desc="" '
    'clickable="true" /></hierarchy>'
)


class _FakeDevice:
    """u2 Device 的最小替身：层级/截图/点击。"""

    def __init__(self):
        self.clicked = []
        self.info = {"displayWidth": 1080, "displayHeight": 2400}
        self.hierarchy_calls = 0
        self.screenshot_calls = 0

    def dump_hierarchy(self):
        self.hierarchy_calls += 1
        return _FAKE_HIERARCHY_XML

    def screenshot(self):
        from PIL import Image

        self.screenshot_calls += 1
        return Image.new("RGB", (1080, 2400), color=(10, 10, 10))

    def click(self, x, y):
        self.clicked.append((x, y))

    def press(self, key):
        self.clicked.append(key)

    def app_start(self, pkg):
        self.clicked.append(("start", pkg))

    def app_stop(self, pkg):
        self.clicked.append(("stop", pkg))


@pytest.fixture()
def fake_device(monkeypatch):
    device = _FakeDevice()
    monkeypatch.setattr(
        recording_service._recording_android_device_pool,
        "acquire",
        lambda serial: device,
    )
    monkeypatch.setattr(
        recording_service._recording_android_device_pool,
        "invalidate",
        lambda serial, device=None: None,
    )
    # 稳定轮询走真实循环，但去掉等待（层级固定的替身设备一轮确认即稳定）
    monkeypatch.setattr(recording_service, "_get_recording_post_action_delay", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(recording_service, "UI_STABLE_POLL_GAP_SECONDS", 0.0)
    return device


def test_dump_device_payload_shape(fake_device):
    payload = recording_service.dump_device(serial="USB1")
    assert payload["hierarchy_xml"].startswith("<hierarchy")
    assert payload["hierarchy_hash"]
    assert payload["screenshot"]
    assert payload["screenshot_format"] == "jpeg"
    assert payload["device_info"]["displayWidth"] == 1080


def test_dump_device_can_omit_parts(fake_device):
    payload = recording_service.dump_device(
        serial="USB1", include_device_info=False, include_hierarchy=False, include_screenshot=True
    )
    assert "device_info" not in payload
    assert "hierarchy_xml" not in payload
    assert payload["screenshot"]


def test_inspect_device_builds_click_step(fake_device):
    result = recording_service.inspect_device(x=50, y=50, serial="USB1")
    assert result["strategy"] in ("text", "description", "resourceId", "class_name", "bounds")
    assert result["step"]["action"] == "click"


def test_interact_device_performs_click_and_returns_dump(fake_device):
    result = recording_service.interact_device(
        x=50,
        y=50,
        operation="click",
        device_serial="USB1",
        include_screenshot=True,
        include_device_info=False,
    )
    assert (50, 50) in fake_device.clicked
    assert "dump" in result and "hierarchy_xml" in result["dump"]
    assert "device_info" not in result["dump"]  # include_device_info=False 生效
    if result["step"] is not None:
        assert result["step"]["action"] == "click"


def test_interact_device_global_action(fake_device):
    result = recording_service.interact_device(
        x=0, y=0, operation="back", device_serial="USB1", include_screenshot=False
    )
    assert "back" in fake_device.clicked
    assert result["step"]["action"] == "back"
    assert "screenshot" not in result["dump"]


def test_post_action_dump_reuses_stable_frame(fake_device):
    """稳定帧即响应 dump：点击后只探测 2 次层级（首帧+确认帧）、截图恰 1 次。

    旧实现每次轮询整图截图、稳定后再补一次 dump（一次点击 ≥3 次整图 + 1 次层级）。
    """
    result = recording_service.interact_device(
        x=50,
        y=50,
        operation="click",
        xml_dump=_FAKE_HIERARCHY_XML,
        device_serial="USB1",
        include_screenshot=True,
        include_device_info=True,
    )
    assert fake_device.hierarchy_calls == 2
    assert fake_device.screenshot_calls == 1
    assert result["dump"]["hierarchy_xml"] == _FAKE_HIERARCHY_XML
    assert result["dump"]["screenshot"]
    assert result["dump"]["device_info"]["displayWidth"] == 1080


def test_post_action_dump_waits_until_hierarchy_settles(fake_device, monkeypatch):
    """界面连续变化时轮询到稳定：返回稳定帧，不稳定轮的截图作废不冒充结果。"""

    class _ChangingHierarchyDevice(_FakeDevice):
        def __init__(self, stabilize_after: int):
            super().__init__()
            self.stabilize_after = stabilize_after

        def dump_hierarchy(self):
            self.hierarchy_calls += 1
            if self.hierarchy_calls >= self.stabilize_after:
                return _FAKE_HIERARCHY_XML
            return f'<hierarchy rotation="0"><node bounds="[0,0][10,10]" text="第{self.hierarchy_calls}帧" /></hierarchy>'

    device = _ChangingHierarchyDevice(stabilize_after=4)
    monkeypatch.setattr(recording_service._recording_android_device_pool, "acquire", lambda serial: device)
    result = recording_service.interact_device(
        x=50,
        y=50,
        operation="click",
        xml_dump=_FAKE_HIERARCHY_XML,
        device_serial="USB1",
        include_screenshot=True,
        include_device_info=False,
    )
    assert result["dump"]["hierarchy_xml"] == _FAKE_HIERARCHY_XML
    assert device.hierarchy_calls >= 5  # 3 帧变化 + 稳定帧 + 确认帧
    assert device.screenshot_calls >= 2  # 不稳定轮作废后重拍


def test_post_action_dump_network_device_skips_stability_polling(fake_device):
    """网络设备（ip:port）跳过稳定轮询：单次 dump 直接返回（同既有语义）。"""
    result = recording_service.interact_device(
        x=50,
        y=50,
        operation="click",
        xml_dump=_FAKE_HIERARCHY_XML,
        device_serial="192.168.1.9:5555",
        include_screenshot=False,
        include_device_info=False,
    )
    assert fake_device.hierarchy_calls == 1
    assert "screenshot" not in result["dump"]


def test_post_action_dump_timeout_uses_last_frame(fake_device, monkeypatch):
    """持续不稳定（如动画）超时：以最后一帧层级 + 现拍截图兜底，不无限等待。"""

    class _NeverStableDevice(_FakeDevice):
        def dump_hierarchy(self):
            self.hierarchy_calls += 1
            return f'<hierarchy rotation="0"><node bounds="[0,0][10,10]" text="帧{self.hierarchy_calls}" /></hierarchy>'

    device = _NeverStableDevice()
    monkeypatch.setattr(recording_service._recording_android_device_pool, "acquire", lambda serial: device)
    monkeypatch.setattr(recording_service, "UI_STABLE_TIMEOUT_SECONDS", 0.05)
    result = recording_service.interact_device(
        x=50,
        y=50,
        operation="click",
        xml_dump=_FAKE_HIERARCHY_XML,
        device_serial="USB1",
        include_screenshot=True,
        include_device_info=False,
    )
    assert "帧" in result["dump"]["hierarchy_xml"]  # 最后一帧
    assert result["dump"]["screenshot"]
    assert device.screenshot_calls >= 1


def test_interact_illegal_ios_serial_is_a_readable_400():
    with pytest.raises(RecordingError) as exc:
        recording_service.interact_device(x=1, y=1, operation="click", device_serial="ios:udid")
    assert exc.value.status_code == 400
    assert "UDID" in str(exc.value)


def test_execute_single_step_requires_device():
    with pytest.raises(RecordingError) as exc:
        recording_service.execute_single_step(step={"action": "home"})
    assert exc.value.status_code == 400


def test_execute_single_step_runs_runner(fake_device, monkeypatch):
    class _FakeRunner:
        def __init__(self, platform=None, device_id=None, **kwargs):
            self.driver = fake_device
            self.runtime_variables = {}

        def run_step(self, step):
            return {
                "status": "PASS",
                "duration": 0.02,
                "attempts": 1,
                "step": step,
                "output": {"echo": step.get("action")},
            }

        def disconnect(self):
            pass

    monkeypatch.setattr(recording_service, "CrossPlatformRunner", _FakeRunner)
    result = recording_service.execute_single_step(
        step={"action": "home", "selector": "", "selector_type": "text", "error_strategy": "ABORT"},
        device_serial="USB1",
        include_screenshot=False,
    )
    assert result["result"]["success"] is True
    assert result["result"]["status"] == "PASS"
    assert result["result"]["output"] == {"echo": "home"}
    assert "dump" in result


def test_execute_single_step_localizes_template_image(fake_device, monkeypatch):
    captured = {}

    class _FakeRunner:
        def __init__(self, platform=None, device_id=None, **kwargs):
            self.driver = fake_device
            self.runtime_variables = {}

        def run_step(self, step):
            captured["step"] = step
            return {"status": "PASS", "duration": 0.01, "attempts": 1, "step": step}

        def disconnect(self):
            pass

    monkeypatch.setattr(recording_service, "CrossPlatformRunner", _FakeRunner)
    recording_service.execute_single_step(
        step={"action": "click_image", "args": {"image_path": "static/images/element_zz.png"}},
        device_serial="USB1",
        include_screenshot=False,
        ensure_local=lambda p: f"/cache/{p.rsplit('/', 1)[-1]}",
    )
    assert captured["step"]["args"]["image_path"] == "/cache/element_zz.png"
