# 来源：语义对齐 AutoDroid backend/api/recording.py（_RecordingIOSSessionPool、iOS 层级/设备信息）。
"""桌面端 iOS 真机能力（USB + WebDriverAgent）：WDA 就绪、录制驱动池、层级、设备信息、截图预览。

WDA 由 engine.core.fastbot_ios.wda.registry 统一管理（每台设备一个常驻 iproxy +
按需拉起的 xcodebuild），录制 / 实时画面 / 截图 / 用例执行 / 稳定性测试共用。
实时画面（MJPEG 中继）见 engine.core.ios_mjpeg。

会话注意点（2026-11-06 iOS 27.3 真机验证）：WDA 同一时刻只有一个活动会话，新建会话会让旧会话
失效（旧会话后续请求 404 Session does not exist，被测 App 不会被杀）。IOSDriver 的点击 / 滑动等
每次都新建会话：稳定性测试期间录制，测试会话会被反复顶掉，连续报错、反复重建会话（重新拉起 App），
最终以 WDA 不可恢复中止。所以：
- /source、/screenshot、/wda/screen 走无会话接口，不为读操作新建会话；
- 稳定性测试占用设备期间，拒绝任何会新建会话的入口（ensure_not_in_stability_run）。
"""
from __future__ import annotations

import base64
import io
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from engine.core import device_busy
from engine.core.fastbot_ios.tools import list_udids, udid_from_serial
from engine.core.fastbot_ios.wda import WDA_LAUNCH_TIMEOUT_SECONDS, registry

logger = logging.getLogger(__name__)

# 录制 WDA 会话空闲回收（对齐平台 RECORDING_IOS_IDLE_TTL_SECONDS）
RECORDING_IOS_IDLE_TTL_SECONDS = 45.0
# 录制/截图请求等待 WDA 就绪的上限：前端请求超时 30s，而 xcodebuild 冷启动要 30~90s，
# 请求先返回「正在启动」，启动在后台继续
WDA_REQUEST_WAIT_SECONDS = 20.0
# 用例执行没有前端请求超时的约束，等到冷启动结束
WDA_RUN_WAIT_SECONDS = WDA_LAUNCH_TIMEOUT_SECONDS + 30.0
# WDA 探活超时要宽：路由都在 XCTest 主线程处理，别的请求（/actions、完整层级）在途时 /status
# 会排队数秒；误判「WDA 挂了」的代价是重建转发并重启 xcodebuild，打断在途操作
WDA_STATUS_PROBE_TIMEOUT_SECONDS = 8.0
# 驱动连续使用时不必每次探活；闲置超过这个时间（可能拔过线 / WDA 被回收）才重新确认
WDA_RECHECK_IDLE_SECONDS = 30.0
# 驱动按设备串行使用；等锁上限同样小于前端请求超时
DRIVER_LOCK_WAIT_SECONDS = 25.0
# 设备中心截图预览规格（对齐 local_api._to_preview_jpeg 的 Android 口径）
PREVIEW_MAX_EDGE = 1280
PREVIEW_JPEG_QUALITY = 80
PREVIEW_SCREENSHOT_TIMEOUT_SECONDS = 15.0

WDA_STARTING_DETAIL = "iOS 设备 WDA 正在启动（首次约 30~90 秒），请稍后重试"
DEVICE_NOT_CONNECTED_DETAIL = "iOS 设备未通过 USB 连接或未信任本机"
STABILITY_RUNNING_DETAIL = "该设备正在跑稳定性测试，请等测试结束后再操作"


class IOSDeviceError(RuntimeError):
    """iOS 设备能力不可用；status_code 供路由映射（503 = WDA 未就绪 / 设备不可用）。"""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def _udid(serial: str) -> str:
    try:
        return udid_from_serial(serial)
    except ValueError as exc:
        raise IOSDeviceError(str(exc), status_code=400) from exc


# --------------------------------------------------------------------------- #
#  WDA 就绪（每台设备至多一个后台启动；请求只等有限时间）
# --------------------------------------------------------------------------- #

class _Startup:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Optional[BaseException] = None


_startups: Dict[str, _Startup] = {}
_startups_lock = threading.Lock()


def _run_startup(udid: str, startup: _Startup) -> None:
    try:
        registry.ensure_ready(udid)
    except BaseException as exc:  # noqa: BLE001 - 原因由等待方透出
        startup.error = exc
        logger.warning("iOS WDA 启动失败: udid=%s error=%s", udid, exc)
    finally:
        startup.done.set()


def _start_wda(udid: str) -> _Startup:
    with _startups_lock:
        startup = _startups.get(udid)
        if startup is not None and not startup.done.is_set():
            return startup
        startup = _Startup()
        _startups[udid] = startup
    threading.Thread(target=_run_startup, args=(udid, startup), name=f"ios-wda-start-{udid}", daemon=True).start()
    return startup


def wda_url(serial: str, wait_seconds: Optional[float] = None) -> str:
    """确保该设备 WDA 就绪并返回本机转发地址；超过等待上限仍未就绪抛 503（启动在后台继续）。

    wait_seconds 缺省为 WDA_REQUEST_WAIT_SECONDS（录制 / 截图请求）。
    """
    if wait_seconds is None:
        wait_seconds = WDA_REQUEST_WAIT_SECONDS
    udid = _udid(serial)
    host = registry.get(udid)
    if host.status_ok(timeout=WDA_STATUS_PROBE_TIMEOUT_SECONDS):
        return host.base_url
    connected = list_udids()  # None = 工具不可用（未知），交给启动流程报错
    if connected is not None and udid not in connected:
        raise IOSDeviceError(DEVICE_NOT_CONNECTED_DETAIL)
    startup = _start_wda(udid)
    if not startup.done.wait(wait_seconds):
        raise IOSDeviceError(WDA_STARTING_DETAIL)
    if startup.error is not None:
        raise IOSDeviceError(str(startup.error)) from startup.error
    return host.base_url


def ensure_not_in_stability_run(serial: str) -> None:
    """稳定性测试独占 WDA 会话：此时新建会话会顶掉测试会话（见模块说明）。"""
    if device_busy.is_busy(serial) or device_busy.is_busy(_udid(serial)):
        raise IOSDeviceError(STABILITY_RUNNING_DETAIL, status_code=409)


def execution_wda_url(serial: str, wait_seconds: Optional[float] = None) -> str:
    """用例执行 / 单步执行入口：拒绝跑测中的设备，并等 WDA 就绪（缺省等到冷启动结束）。"""
    ensure_not_in_stability_run(serial)
    return wda_url(serial, wait_seconds=WDA_RUN_WAIT_SECONDS if wait_seconds is None else wait_seconds)


# --------------------------------------------------------------------------- #
#  录制驱动池（IOSDriver = facebook-wda 封装）
# --------------------------------------------------------------------------- #

class _Entry:
    def __init__(self, udid: str) -> None:
        self.udid = udid
        self.driver: Any = None  # 持有 lock 时惰性创建：连接 WDA 有网络往返，不占池锁
        self.lock = threading.Lock()
        self.last_used_at = time.time()


class IOSDriverPool:
    """每台设备一个 IOSDriver，按设备串行使用；其他设备取用时回收空闲条目。"""

    def __init__(self, driver_factory=None) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._factory = driver_factory

    def _new_driver(self, udid: str, url: str) -> Any:
        if self._factory is not None:
            return self._factory(udid, url)
        try:
            from engine.core.drivers.ios_driver import IOSDriver
        except ImportError as exc:  # pragma: no cover - 打包缺依赖时给出可读原因
            raise IOSDeviceError(f"iOS 驱动不可用（facebook-wda 未安装）：{exc}") from exc
        return IOSDriver(device_id=udid, wda_url=url)

    def _entry_for(self, udid: str) -> _Entry:
        stale: List[_Entry] = []
        with self._lock:
            now = time.time()
            for key, item in list(self._entries.items()):
                if key != udid and not item.lock.locked() and now - item.last_used_at > RECORDING_IOS_IDLE_TTL_SECONDS:
                    stale.append(self._entries.pop(key))
            entry = self._entries.get(udid)
            if entry is None:
                entry = _Entry(udid)
                self._entries[udid] = entry
        for item in stale:
            _close_driver(item.driver)
        return entry

    def acquire(self, serial: str) -> Any:
        """取该设备的驱动并持有其锁；用完必须 release（或 invalidate）。"""
        ensure_not_in_stability_run(serial)
        udid = _udid(serial)
        deadline = time.monotonic() + DRIVER_LOCK_WAIT_SECONDS
        while True:
            entry = self._entry_for(udid)
            if not entry.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise IOSDeviceError("iOS 设备正忙，请稍后重试")
            with self._lock:
                alive = self._entries.get(udid) is entry
            if alive:
                break
            entry.lock.release()  # 等锁期间条目已失效（invalidate / 上一个持有者连接失败）
        try:
            if entry.driver is None:
                entry.driver = self._new_driver(udid, wda_url(serial))
            elif time.time() - entry.last_used_at > WDA_RECHECK_IDLE_SECONDS:
                wda_url(serial)
        except BaseException as exc:
            with self._lock:
                if self._entries.get(udid) is entry and entry.driver is None:
                    self._entries.pop(udid, None)
            entry.lock.release()
            if isinstance(exc, IOSDeviceError) or not isinstance(exc, Exception):
                raise
            raise IOSDeviceError(f"连接 iOS 设备失败：{exc}") from exc
        entry.last_used_at = time.time()
        return entry.driver

    def release(self, serial: str, driver: Any = None) -> None:
        udid = _udid(serial)
        with self._lock:
            entry = self._entries.get(udid)
        if entry is None or entry.driver is None or (driver is not None and entry.driver is not driver):
            return  # 已被 invalidate：锁在那时释放过
        entry.last_used_at = time.time()
        if entry.lock.locked():
            try:
                entry.lock.release()
            except RuntimeError:
                pass

    def invalidate(self, serial: str, driver: Any) -> None:
        """持有者发现驱动异常时丢弃它；driver 必传，否则可能关掉别的请求正在用的驱动。"""
        if driver is None:
            return
        udid = _udid(serial)
        with self._lock:
            entry = self._entries.get(udid)
            if entry is None or entry.driver is not driver:
                return
            self._entries.pop(udid, None)
        if entry.lock.locked():
            try:
                entry.lock.release()
            except RuntimeError:
                pass
        _close_driver(driver)

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            _close_driver(entry.driver)


def _close_driver(driver: Any) -> None:
    if driver is None:
        return
    try:
        driver.disconnect()
    except Exception:
        logger.debug("关闭 iOS 录制会话失败", exc_info=True)


driver_pool = IOSDriverPool()


# --------------------------------------------------------------------------- #
#  层级 / 设备信息 / 截图（语义对齐平台 recording.py 的 iOS 分支）
# --------------------------------------------------------------------------- #

def source_xml(driver: Any) -> str:
    """当前前台页面层级 XML（无会话、去掉慢属性的快速层级，见 IOSDriver._page_source_fast）。"""
    getters = (
        lambda: driver._page_source_fast(),
        lambda: driver.client.source(),
    )
    for getter in getters:
        try:
            text = str(getter() or "").strip()
            if text:
                return text
        except Exception:
            logger.debug("iOS 层级获取失败，尝试下一种方式", exc_info=True)
    raise IOSDeviceError("iOS 页面层级获取失败")


def window_size(driver: Any) -> Tuple[int, int]:
    """屏幕逻辑尺寸（点）。优先无会话的 /wda/screen。"""
    try:
        size = driver.client.http.get("/wda/screen").value["screenSize"]
        return int(size["width"]), int(size["height"])
    except Exception:
        logger.debug("/wda/screen 不可用，回退 window_size", exc_info=True)
    try:
        size = driver.client.window_size()
    except Exception:
        logger.debug("iOS 窗口尺寸获取失败", exc_info=True)
        return 0, 0
    if isinstance(size, dict):
        return int(size.get("width") or 0), int(size.get("height") or 0)
    if isinstance(size, (tuple, list)) and len(size) >= 2:
        return int(size[0] or 0), int(size[1] or 0)
    return int(getattr(size, "width", 0) or 0), int(getattr(size, "height", 0) or 0)


def device_info(driver: Any, serial: Optional[str]) -> Dict[str, Any]:
    width_points, height_points = window_size(driver)
    scale = float(getattr(driver, "scale", 1.0) or 1.0)
    width_px = int(round(width_points * scale)) if width_points else 0
    height_px = int(round(height_points * scale)) if height_points else 0
    return {
        "platform": "ios",
        "serial": serial or getattr(driver, "device_id", ""),
        "udid": getattr(driver, "device_id", serial or ""),
        "wda_url": getattr(driver, "wda_url", ""),
        "scale": scale,
        "resolution": f"{width_px}x{height_px}" if width_px and height_px else "",
        "window_size": {"width": width_points, "height": height_points},
    }


def coordinate_scale(driver: Any) -> float:
    scale = float(getattr(driver, "scale", 1.0) or 1.0)
    return scale if scale > 0 else 1.0


def preview_screenshot(serial: str) -> Dict[str, str]:
    """设备中心「查看截图」：WDA /screenshot（无需会话）→ 最长边 ≤1280 的 JPEG。"""
    import httpx
    from PIL import Image

    url = wda_url(serial)
    try:
        resp = httpx.get(f"{url}/screenshot", timeout=PREVIEW_SCREENSHOT_TIMEOUT_SECONDS)
        resp.raise_for_status()
        raw = base64.b64decode(str((resp.json() or {}).get("value") or ""))
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise IOSDeviceError(f"iOS 截图失败：{exc}") from exc
    image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE))
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
    return {"base64_img": base64.b64encode(buffered.getvalue()).decode("ascii"), "image_format": "jpeg"}


def shutdown() -> None:
    """引擎退出：断开录制会话并关闭共享 WDA（iproxy / 本引擎拉起的 xcodebuild）。"""
    driver_pool.close_all()
    registry.close_all()
