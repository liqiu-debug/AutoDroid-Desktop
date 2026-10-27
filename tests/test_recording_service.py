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


class _FakeDevice:
    """u2 Device 的最小替身：层级/截图/点击。"""

    def __init__(self):
        self.clicked = []
        self.info = {"displayWidth": 1080, "displayHeight": 2400}

    def dump_hierarchy(self):
        return (
            '<hierarchy rotation="0"><node bounds="[0,0][100,100]" text="登录" '
            'resource-id="com.demo:id/login" class="android.widget.Button" content-desc="" '
            'clickable="true" /></hierarchy>'
        )

    def screenshot(self):
        from PIL import Image

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
    # 跳过截图哈希轮询（测试里不等待 UI 稳定）
    monkeypatch.setattr(recording_service, "_wait_ui_stable", lambda *args, **kwargs: None)
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


def test_interact_ios_rejected():
    with pytest.raises(RecordingError) as exc:
        recording_service.interact_device(x=1, y=1, operation="click", device_serial="ios:udid")
    assert exc.value.status_code == 400
    assert "Android" in str(exc.value)


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
