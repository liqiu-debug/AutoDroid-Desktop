"""有线 iPhone 录制链路（engine.core.ios_device + recording_service iOS 分支）。

不碰真机：WDA 主机 / 驱动用替身；真机验证记录见 2026-11-06 会话（iPhone 14 Pro / iOS 27.3）。
"""
from __future__ import annotations

import base64
import io
import threading
import time

import pytest

from engine.core import device_busy, ios_device, recording_service
from engine.core.execution_service import resolve_device_platform
from engine.core.recording_service import RecordingError
from engine.core.utils import calculate_element_from_coordinates

UDID = "00008120-00146CA4110BC01E"

# 计算器页面的简化层级（iOS 逻辑坐标：点）
IOS_SOURCE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="计算器" label="计算器" '
    'x="0" y="0" width="393" height="852">'
    '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="393" height="852">'
    '<XCUIElementTypeToolbar type="XCUIElementTypeToolbar" name="Toolbar" label="工具栏" '
    'x="0" y="0" width="393" height="852">'
    '<XCUIElementTypeKey type="XCUIElementTypeKey" name="Seven" label="7" x="16" y="429" width="85" height="85"/>'
    '</XCUIElementTypeToolbar>'
    '<XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" '
    'name="_TtGC7SwiftUI32NavigationStackHosting" x="0" y="59" width="393" height="54"/>'
    '</XCUIElementTypeWindow></XCUIElementTypeApplication>'
)


def _png(width=1179, height=2556) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


class _Value:
    def __init__(self, value):
        self.value = value


class _FakeHttp:
    def __init__(self, driver):
        self.driver = driver

    def get(self, path, timeout=None):
        self.driver.calls.append(("GET", path))
        if path.startswith("source"):
            return _Value(self.driver.source)
        if path == "/wda/screen":
            return _Value({"screenSize": {"width": 393, "height": 852}, "scale": 3})
        raise AssertionError(path)


class _FakeClient:
    def __init__(self, driver):
        self.http = _FakeHttp(driver)
        self.driver = driver

    def source(self):
        self.driver.calls.append(("full-source",))
        return self.driver.source

    def session(self):  # pragma: no cover - 读操作不应新建会话
        raise AssertionError("读层级 / 窗口尺寸不应新建 WDA 会话")


class FakeIOSDriver:
    """IOSDriver 替身：层级 / 截图 / 坐标点击 / 应用操作。"""

    instances = []

    def __init__(self, device_id, wda_url):
        from engine.core.drivers.ios.support import IOSDriverSupportMixin

        self.device_id = device_id
        self.wda_url = wda_url
        self.scale = 3.0
        self.source = IOS_SOURCE
        self.calls = []
        self.client = _FakeClient(self)
        self.disconnected = False
        self._page_source_fast = IOSDriverSupportMixin._page_source_fast.__get__(self)
        FakeIOSDriver.instances.append(self)

    def screenshot(self):
        self.calls.append(("screenshot",))
        return _png()

    def click_by_coordinates(self, x, y):
        self.calls.append(("tap", x, y))

    def start_app(self, app_id):
        self.calls.append(("start_app", app_id))

    def stop_app(self, app_id):
        self.calls.append(("stop_app", app_id))

    def back(self):
        self.calls.append(("back",))

    def home(self):
        self.calls.append(("home",))

    def swipe(self, direction):
        self.calls.append(("swipe", direction))

    def disconnect(self):
        self.disconnected = True


class FakeHost:
    def __init__(self, udid, ready=True):
        self.udid = udid
        self.base_url = "http://127.0.0.1:18100"
        self.mjpeg_port = 19100
        self.ready = ready
        self.ensure_calls = 0
        self.release = threading.Event()
        self.block = False
        self.error = None

    def status_ok(self, timeout=3.0):
        return self.ready

    def ensure_ready(self):
        self.ensure_calls += 1
        if self.block:
            self.release.wait(5)
        if self.error:
            raise self.error
        self.ready = True


class FakeRegistry:
    def __init__(self):
        self.hosts = {}

    def get(self, udid):
        return self.hosts.setdefault(udid, FakeHost(udid))

    def ensure_ready(self, udid):
        host = self.get(udid)
        host.ensure_ready()
        return host

    def close_all(self):
        self.hosts.clear()


@pytest.fixture()
def fake_wda(monkeypatch):
    registry = FakeRegistry()
    FakeIOSDriver.instances = []
    monkeypatch.setattr(ios_device, "registry", registry)
    monkeypatch.setattr(ios_device, "list_udids", lambda: [UDID])
    monkeypatch.setattr(ios_device, "_startups", {})
    pool = ios_device.IOSDriverPool(driver_factory=FakeIOSDriver)
    monkeypatch.setattr(ios_device, "driver_pool", pool)
    monkeypatch.setattr(recording_service, "_get_recording_post_action_delay", lambda *a, **k: 0.0)
    monkeypatch.setattr(recording_service, "UI_STABLE_POLL_GAP_SECONDS", 0.0)
    return registry


def _lock_free(pool=None) -> bool:
    pool = pool or ios_device.driver_pool
    entry = pool._entries.get(UDID)
    return entry is None or not entry.lock.locked()


# --------------------------- 平台判定 --------------------------- #

def test_bare_udid_resolves_to_ios_and_hint_wins():
    assert resolve_device_platform(UDID) == "ios"
    assert resolve_device_platform("ios:" + UDID) == "ios"
    assert resolve_device_platform("3B6F68E6SA7TA0Q5") == "android"
    assert resolve_device_platform("192.168.1.8:5555") == "android"
    assert resolve_device_platform(UDID, "android") == "android"


# --------------------------- 录制：dump / inspect / interact --------------------------- #

def test_dump_ios_returns_same_size_jpeg_hierarchy_and_device_info(fake_wda):
    from PIL import Image

    payload = recording_service.dump_device(serial=UDID)
    assert payload["screenshot_format"] == "jpeg"
    image = Image.open(io.BytesIO(base64.b64decode(payload["screenshot"])))
    assert image.size == (1179, 2556), "截图像素不能变：点击坐标与模板裁剪都按截图像素计算"
    assert payload["hierarchy_xml"].startswith("<?xml")
    assert payload["device_info"]["platform"] == "ios"
    assert payload["device_info"]["resolution"] == "1179x2556"
    driver = FakeIOSDriver.instances[0]
    assert ("GET", "source?format=xml&excluded_attributes=visible,accessible") in driver.calls
    assert ("full-source",) not in driver.calls
    assert _lock_free(), "请求结束必须归还驱动"


def test_inspect_ios_scales_pixels_to_points(fake_wda):
    # 「7」键：逻辑 (16,429)-(101,514) → 像素 ×3
    result = recording_service.inspect_device(x=175, y=1414, serial=UDID)
    assert result["selector"] == "7" and result["strategy"] == "text"
    assert result["element"]["bounds"] == "[48,1287][303,1542]"
    assert _lock_free()


def test_inspect_ios_blank_area_does_not_record_app_or_swiftui_names(fake_wda):
    """空白处：根节点（App 名）/ 铺满屏的工具栏 / SwiftUI 类名都不能录成点击步骤。"""
    with pytest.raises(RecordingError) as exc:
        recording_service.inspect_device(x=600, y=2000, serial=UDID)
    assert exc.value.status_code == 400
    with pytest.raises(RecordingError) as exc:
        recording_service.inspect_device(x=600, y=250, serial=UDID)  # 导航栏：name 是 Swift 运行时类名
    assert exc.value.status_code == 400
    assert _lock_free()


def test_element_tools_skips_fullscreen_ios_containers_but_keeps_android_root():
    # 空白处只命中铺满屏的容器 → 未识别到元素（录制侧提示改用图像点击），而不是「点击 计算器」
    res = calculate_element_from_coordinates(IOS_SOURCE, 600, 2000, coordinate_scale=3.0)
    assert "error" in res, res
    # 导航栏 name 是 Swift 运行时类名：不当语义定位
    res = calculate_element_from_coordinates(IOS_SOURCE, 600, 250, coordinate_scale=3.0)
    assert res["strategy"] == "xpath", res
    android = (
        '<hierarchy><node bounds="[0,0][1080,2400]" text="首页" class="android.widget.FrameLayout"/></hierarchy>'
    )
    assert calculate_element_from_coordinates(android, 10, 10)["selector"] == "首页"


def test_interact_ios_click_taps_pixels_and_returns_new_dump(fake_wda):
    result = recording_service.interact_device(x=175, y=1414, operation="click", device_serial=UDID)
    assert result["step"]["selector"] == "7"
    driver = FakeIOSDriver.instances[0]
    assert ("tap", 175, 1414) in driver.calls, "IOSDriver.click_by_coordinates 收像素、内部除以 scale"
    assert result["dump"]["hierarchy_xml"] and result["dump"]["screenshot_format"] == "jpeg"
    assert _lock_free()


@pytest.mark.parametrize(
    "operation,action_data,expected",
    [
        ("start_app", "com.apple.Preferences", ("start_app", "com.apple.Preferences")),
        ("stop_app", "com.apple.Preferences", ("stop_app", "com.apple.Preferences")),
        ("back", None, ("back",)),
        ("home", None, ("home",)),
        ("swipe", "down", ("swipe", "down")),
    ],
)
def test_interact_ios_global_operations(fake_wda, operation, action_data, expected):
    result = recording_service.interact_device(
        x=0, y=0, operation=operation, action_data=action_data, device_serial=UDID, include_screenshot=False
    )
    assert expected in FakeIOSDriver.instances[0].calls
    assert result["step"]["action"] == operation
    assert "screenshot" not in result["dump"]


def test_interact_ios_failure_invalidates_driver_and_releases_lock(fake_wda):
    recording_service.dump_device(serial=UDID, include_screenshot=False)
    first = FakeIOSDriver.instances[0]

    def boom():
        raise RuntimeError("iOS back 执行失败：页面未变化")

    first.back = boom
    with pytest.raises(RecordingError) as exc:
        recording_service.interact_device(x=0, y=0, operation="back", device_serial=UDID)
    assert exc.value.status_code == 500 and "页面未变化" in str(exc.value)
    assert first.disconnected and _lock_free()
    recording_service.dump_device(serial=UDID, include_screenshot=False)
    assert len(FakeIOSDriver.instances) == 2, "失效后下次请求重建驱动"


def test_ios_post_action_waits_for_stable_hierarchy_before_screenshot(fake_wda, monkeypatch):
    """iOS 会话串行使用：稳定轮询期间不并行截图，确认稳定后只截一张。"""
    recording_service.dump_device(serial=UDID, include_screenshot=False)
    driver = FakeIOSDriver.instances[0]
    sources = iter([IOS_SOURCE.replace("工具栏", f"帧{i}") for i in range(2)] + [IOS_SOURCE] * 10)
    monkeypatch.setattr(ios_device, "source_xml", lambda d: next(sources))
    driver.calls.clear()
    result = recording_service.interact_device(x=0, y=0, operation="home", device_serial=UDID)
    assert result["dump"]["screenshot"]
    assert [c for c in driver.calls if c[0] == "screenshot"] == [("screenshot",)]


# --------------------------- WDA 就绪 / 占用 --------------------------- #

def test_wda_starting_returns_503_quickly_and_starts_only_once(fake_wda, monkeypatch):
    host = fake_wda.get(UDID)
    host.ready = False
    host.block = True
    monkeypatch.setattr(ios_device, "WDA_REQUEST_WAIT_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(RecordingError) as exc:
        recording_service.dump_device(serial=UDID)
    assert exc.value.status_code == 503 and "正在启动" in str(exc.value)
    assert time.monotonic() - started < 3
    with pytest.raises(RecordingError):
        recording_service.dump_device(serial=UDID)
    assert host.ensure_calls == 1, "启动在后台进行，期间的请求不重复拉起 xcodebuild"
    host.release.set()
    for _ in range(50):
        if host.ready:
            break
        time.sleep(0.05)
    assert recording_service.dump_device(serial=UDID, include_screenshot=False)["hierarchy_xml"]


def test_wda_start_failure_surfaces_reason(fake_wda):
    from engine.core.fastbot_ios.wda import WdaError

    host = fake_wda.get(UDID)
    host.ready = False
    host.error = WdaError("WDA 启动失败：请确认设备已解锁、信任本机")
    with pytest.raises(RecordingError) as exc:
        recording_service.dump_device(serial=UDID)
    assert exc.value.status_code == 503 and "已解锁" in str(exc.value)


def test_unplugged_device_is_readable_503(fake_wda, monkeypatch):
    fake_wda.get(UDID).ready = False
    monkeypatch.setattr(ios_device, "list_udids", lambda: [])
    with pytest.raises(RecordingError) as exc:
        recording_service.dump_device(serial=UDID)
    assert exc.value.status_code == 503 and "USB" in str(exc.value)


def test_recording_refused_while_stability_run_holds_device(fake_wda):
    assert device_busy.try_acquire(UDID)
    with pytest.raises(RecordingError) as exc:
        recording_service.interact_device(x=1, y=1, operation="click", device_serial=UDID)
    assert exc.value.status_code == 409 and "稳定性测试" in str(exc.value)
    assert not FakeIOSDriver.instances, "被拒时不能碰 WDA（新建会话会顶掉测试会话）"
    with pytest.raises(RecordingError) as exc:
        recording_service.execute_single_step(step={"action": "home"}, device_serial=UDID)
    assert exc.value.status_code == 409


def test_pool_serializes_requests_per_device(fake_wda, monkeypatch):
    monkeypatch.setattr(ios_device, "DRIVER_LOCK_WAIT_SECONDS", 0.2)
    driver = ios_device.driver_pool.acquire(UDID)
    with pytest.raises(ios_device.IOSDeviceError) as exc:
        ios_device.driver_pool.acquire(UDID)
    assert "正忙" in str(exc.value)
    ios_device.driver_pool.release(UDID, driver)
    assert ios_device.driver_pool.acquire(UDID) is driver, "同一设备复用驱动"
    ios_device.driver_pool.release(UDID, driver)


def test_pool_driver_creation_failure_releases_entry(fake_wda):
    calls = []

    def flaky(udid, url):
        calls.append(udid)
        if len(calls) == 1:
            raise ConnectionError("iOS WDA 连接失败")
        return FakeIOSDriver(udid, url)

    pool = ios_device.IOSDriverPool(driver_factory=flaky)
    with pytest.raises(ios_device.IOSDeviceError) as exc:
        pool.acquire(UDID)
    assert "连接 iOS 设备失败" in str(exc.value)
    assert UDID not in pool._entries
    driver = pool.acquire(UDID)
    pool.release(UDID, driver)
    assert _lock_free(pool)


def test_pool_rechecks_wda_after_idle(fake_wda, monkeypatch):
    driver = ios_device.driver_pool.acquire(UDID)
    ios_device.driver_pool.release(UDID, driver)
    probes = []
    monkeypatch.setattr(ios_device, "wda_url", lambda serial, wait_seconds=0: probes.append(serial) or "u")
    ios_device.driver_pool.release(UDID, ios_device.driver_pool.acquire(UDID))
    assert probes == [], "连续使用不探活"
    ios_device.driver_pool._entries[UDID].last_used_at -= ios_device.WDA_RECHECK_IDLE_SECONDS + 1
    ios_device.driver_pool.release(UDID, ios_device.driver_pool.acquire(UDID))
    assert probes == [UDID]


def test_preview_screenshot_downscales_to_jpeg(fake_wda, monkeypatch):
    from PIL import Image

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"value": base64.b64encode(_png()).decode("ascii")}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp())
    payload = ios_device.preview_screenshot(UDID)
    assert payload["image_format"] == "jpeg"
    image = Image.open(io.BytesIO(base64.b64decode(payload["base64_img"])))
    assert max(image.size) == ios_device.PREVIEW_MAX_EDGE


# --------------------------- 单步执行 / 整条用例 --------------------------- #

def test_execute_single_step_ios_builds_runner_with_local_wda(fake_wda, monkeypatch):
    captured = {}

    class _Runner:
        def __init__(self, platform=None, device_id=None, **kwargs):
            captured.update(platform=platform, device_id=device_id, **kwargs)
            self.driver = FakeIOSDriver(device_id, kwargs.get("wda_url"))
            self.runtime_variables = {}

        def run_step(self, step):
            return {"status": "PASS", "step": step, "platform": "ios"}

        def disconnect(self):
            captured["disconnected"] = True

    monkeypatch.setattr(recording_service, "CrossPlatformRunner", _Runner)
    result = recording_service.execute_single_step(
        step={"action": "home", "execute_on": ["android", "ios"], "platform_overrides": {}},
        device_serial=UDID,
        include_screenshot=False,
    )
    assert captured["platform"] == "ios" and captured["wda_url"] == "http://127.0.0.1:18100"
    assert result["result"]["success"] is True and result["dump"]["hierarchy_xml"]
    assert captured["disconnected"]


def test_cross_platform_runner_resolves_wda_url_for_ios(fake_wda, monkeypatch):
    from engine.core.drivers import cross_platform_runner

    if cross_platform_runner.IOSDriver is None:  # pragma: no cover - 依赖缺失
        pytest.skip("facebook-wda 未安装")
    created = {}
    monkeypatch.delenv("AUTODROID_DRIVER_POOL", raising=False)
    monkeypatch.setattr(
        cross_platform_runner.DriverFactory,
        "create",
        classmethod(lambda cls, platform, device_id, **kw: created.update(kw) or FakeIOSDriver(device_id, kw.get("wda_url"))),
    )
    cross_platform_runner.TestCaseRunner(platform="ios", device_id=UDID)
    assert created["wda_url"] == "http://127.0.0.1:18100"

    assert device_busy.try_acquire(UDID)
    with pytest.raises(ios_device.IOSDeviceError) as exc:
        cross_platform_runner.TestCaseRunner(platform="ios", device_id=UDID)
    assert exc.value.status_code == 409


# --------------------------- IOSDriver 桌面端剥离点 --------------------------- #

def test_page_source_fast_falls_back_to_full_source():
    from engine.core.drivers.ios.support import IOSDriverSupportMixin

    class _Broken:
        def get(self, path, timeout=None):
            raise RuntimeError("unknown query parameter")

    class _Client:
        http = _Broken()

        @staticmethod
        def source():
            return "<full/>"

    driver = type("D", (IOSDriverSupportMixin,), {})()
    driver.client = _Client()
    assert driver._page_source_fast() == "<full/>"


def test_back_button_search_is_two_quick_predicate_queries():
    """页面没有返回入口时，旧实现逐个 click 13 个候选（每个等 5s），真机上 back 要 200s+。"""
    from engine.core.drivers.ios.app_control import IOSAppControlMixin

    queries = []

    class _Selector:
        def __init__(self, predicate):
            self.predicate = predicate

        def wait(self, timeout=None, raise_error=False):
            queries.append((self.predicate, timeout))
            return False

    class _Session:
        def __call__(self, predicate=None):
            return _Selector(predicate)

    class _Client:
        def session(self):
            return _Session()

    from engine.core.drivers.ios.locator import IOSLocatorMixin

    driver = type("D", (IOSAppControlMixin, IOSLocatorMixin), {})()
    driver.client = _Client()
    started = time.monotonic()
    assert driver._tap_common_back_buttons() is False
    assert len(queries) == 2 and all(timeout <= 0.5 for _, timeout in queries)
    assert time.monotonic() - started < 1


def test_ios_driver_disconnect_does_not_open_a_session():
    from engine.core.drivers import ios_driver

    if getattr(ios_driver, "IOSDriver", None) is None:  # pragma: no cover
        pytest.skip("facebook-wda 未安装")

    class _Client:
        def session(self):  # pragma: no cover - 断言用
            raise AssertionError("disconnect 不能新建 WDA 会话（会顶掉同设备上的稳定性测试会话）")

    driver = ios_driver.IOSDriver.__new__(ios_driver.IOSDriver)
    driver.device_id = UDID
    driver.client = _Client()
    driver.disconnect()
