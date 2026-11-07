"""WDA 主机侧生命周期：iproxy 转发 + xcodebuild 拉起 + 会话 + 看门狗。

实测（iOS 27.3 / WDA 16.15.3）：apps/state 13ms、W3C /actions 5 连击 311ms/事件；
设置 waitForIdleTimeout=0 / animationCoolOffTimeout=0 必须在会话内下发。
"""
from __future__ import annotations

import glob
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from engine.core.fastbot_ios.tools import which

logger = logging.getLogger(__name__)

WDA_DEVICE_PORT = 8100
MJPEG_DEVICE_PORT = 9100
WDA_LAUNCH_TIMEOUT_SECONDS = 90
ACTIONS_TIMEOUT_SECONDS = 60.0
XCTESTRUN_ENV = "AUTODROID_WDA_XCTESTRUN"

# 应用状态（XCUIApplicationState）
APP_NOT_RUNNING = 1
APP_RUNNING_FOREGROUND = 4


class WdaError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_xctestrun() -> Optional[str]:
    """xctestrun：环境变量优先，其次开发态 AutoDroid-WDA 构建产物（取最新）。"""
    override = os.environ.get(XCTESTRUN_ENV)
    if override and os.path.isfile(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [
        os.path.abspath(os.path.join(here, *([".."] * 4), "AutoDroid-WDA")),
        os.path.expanduser("~/Desktop/x/uiauto/AutoDroid-WDA"),
    ]
    for root in roots:
        found = glob.glob(os.path.join(root, "autodroid", ".derivedData", "Build", "Products", "*.xctestrun"))
        if found:
            return max(found, key=os.path.getmtime)
    return None


class WdaHost:
    """单台设备的 WDA 主机侧资源（iproxy ×2 + 可选 xcodebuild）。"""

    def __init__(self, udid: str) -> None:
        self.udid = udid
        self.wda_port = _free_port()
        self.mjpeg_port = _free_port()
        self._procs: Dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self.launched_by_us = False
        self.restarts = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.wda_port}"

    # ------------------------------------------------------------ processes
    def _spawn(self, key: str, args: List[str]) -> None:
        old = self._procs.pop(key, None)
        if old and old.poll() is None:
            old.terminate()
        self._procs[key] = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )

    def _start_forwarding(self) -> None:
        iproxy = which("iproxy")
        if not iproxy:
            raise WdaError("未找到 iproxy：请安装 libimobiledevice（brew install libimobiledevice）")
        self._spawn("wda", [iproxy, "-u", self.udid, f"{self.wda_port}:{WDA_DEVICE_PORT}"])
        self._spawn("mjpeg", [iproxy, "-u", self.udid, f"{self.mjpeg_port}:{MJPEG_DEVICE_PORT}"])

    def _launch_wda(self) -> None:
        xctestrun = resolve_xctestrun()
        xcrun = which("xcrun") or "/usr/bin/xcrun"
        if not xctestrun:
            raise WdaError(
                f"设备上 WDA 未运行且找不到 xctestrun（可设置 {XCTESTRUN_ENV}），请先在设备上启动 WebDriverAgent"
            )
        logger.info("拉起 WDA: udid=%s xctestrun=%s", self.udid, xctestrun)
        self._spawn(
            "xcodebuild",
            [xcrun, "xcodebuild", "test-without-building", "-xctestrun", xctestrun,
             "-destination", f"id={self.udid}"],
        )
        self.launched_by_us = True

    def status_ok(self, timeout: float = 3.0) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/status", timeout=timeout)
            return resp.status_code == 200 and bool((resp.json().get("value") or {}).get("ready", True))
        except Exception:
            return False

    def _wait_ready(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.status_ok():
                return True
            xb = self._procs.get("xcodebuild")
            if xb is not None and xb.poll() is not None:
                return False  # xcodebuild 已退出（签名过期 / 设备锁屏等）
            time.sleep(1.0)
        return False

    def ensure_ready(self) -> None:
        with self._lock:
            if self.status_ok():
                return
            self._start_forwarding()
            if self._wait_ready(4):
                return
            self._launch_wda()
            if not self._wait_ready(WDA_LAUNCH_TIMEOUT_SECONDS):
                raise WdaError(
                    "WDA 启动失败：请确认设备已解锁、信任本机，且 WebDriverAgent 描述文件未过期（免费团队 7 天）"
                )

    def recover(self) -> bool:
        """看门狗：先重建转发，再重启 WDA。"""
        with self._lock:
            self.restarts += 1
            self._start_forwarding()
            if self._wait_ready(5):
                return True
            try:
                self._launch_wda()
            except WdaError as exc:
                logger.warning("WDA 恢复失败: %s", exc)
                return False
            return self._wait_ready(WDA_LAUNCH_TIMEOUT_SECONDS)

    def close(self) -> None:
        for key, proc in list(self._procs.items()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._procs.clear()


class WdaSession:
    """单个 WDA 会话的薄 HTTP 封装。"""

    def __init__(self, host: WdaHost, bundle_id: str, http: Optional[httpx.Client] = None) -> None:
        self.host = host
        self.bundle_id = bundle_id
        self.http = http or httpx.Client(timeout=60.0)
        self.session_id: Optional[str] = None
        self.window: Dict[str, float] = {"width": 390.0, "height": 844.0}

    def _call(self, method: str, path: str, body: Any = None, timeout: float = 60.0) -> Any:
        resp = self.http.request(method, self.host.base_url + path, json=body, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            value = data.get("value") if isinstance(data, dict) else None
            message = value.get("message") if isinstance(value, dict) else resp.text[:200]
            raise WdaError(f"WDA {method} {path} -> {resp.status_code}: {message}")
        return data.get("value") if isinstance(data, dict) else None

    def _s(self, path: str) -> str:
        if not self.session_id:
            raise WdaError("WDA 会话未建立")
        return f"/session/{self.session_id}{path}"

    def open(self, mjpeg_fps: int = 15, mjpeg_scale: int = 50) -> None:
        resp = self.http.post(
            self.host.base_url + "/session",
            json={"capabilities": {"alwaysMatch": {
                "bundleId": self.bundle_id, "shouldWaitForQuiescence": False,
                "defaultAlertAction": "accept",
            }}},
            timeout=90.0,
        )
        data = resp.json() if resp.content else {}
        sid = data.get("sessionId") or (data.get("value") or {}).get("sessionId")
        if resp.status_code >= 400 or not sid:
            message = (data.get("value") or {}).get("message") if isinstance(data.get("value"), dict) else resp.text[:200]
            raise WdaError(f"创建 WDA 会话失败（Bundle ID 是否已安装？）：{message}")
        self.session_id = sid
        self._call("POST", self._s("/appium/settings"), {"settings": {
            "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
            "defaultAlertAction": "accept",
            "mjpegServerFramerate": mjpeg_fps, "mjpegScalingFactor": mjpeg_scale,
            "mjpegServerScreenshotQuality": 50,
        }})
        try:
            size = self._call("GET", self._s("/window/size")) or {}
            if size.get("width") and size.get("height"):
                self.window = {"width": float(size["width"]), "height": float(size["height"])}
        except WdaError:
            pass

    def close(self) -> None:
        if self.session_id:
            try:
                self._call("DELETE", f"/session/{self.session_id}", timeout=10)
            except Exception:
                pass
        self.session_id = None

    def app_state(self, bundle_id: Optional[str] = None) -> int:
        return int(self._call("POST", self._s("/wda/apps/state"), {"bundleId": bundle_id or self.bundle_id}, timeout=15) or 0)

    def active_bundle(self) -> str:
        info = self._call("GET", "/wda/activeAppInfo", timeout=15) or {}
        return str(info.get("bundleId") or "")

    def launch(self) -> None:
        self._call("POST", self._s("/wda/apps/launch"), {"bundleId": self.bundle_id, "shouldWaitForQuiescence": False})

    def activate(self) -> None:
        self._call("POST", self._s("/wda/apps/activate"), {"bundleId": self.bundle_id})

    def terminate(self) -> None:
        self._call("POST", self._s("/wda/apps/terminate"), {"bundleId": self.bundle_id}, timeout=30)

    def source_json(self) -> Any:
        # 去掉 visible/accessible：WDA 逐元素计算这两个属性最慢（真机设置页 1.9s → 0.3s，
        # 取到的点击目标几乎不变），这段时间被测 App 是闲着的
        return self._call("GET", self._s("/source?format=json&excluded_attributes=visible,accessible"), timeout=30)

    def perform(self, actions: List[Dict[str, Any]]) -> None:
        # 实测：App 在批内崩溃时 /actions 会挂起 ≈25s 再 500。不能提前断开：WDA 仍在处理
        # 挂起请求，后续调用会连锁超时；滞后由回放按 .ips 时间戳回溯兜底。
        self._call("POST", self._s("/actions"), {"actions": actions}, timeout=ACTIONS_TIMEOUT_SECONDS)

    def press_button(self, name: str) -> None:
        self._call("POST", self._s("/wda/pressButton"), {"name": name}, timeout=15)

    def accept_alert(self) -> bool:
        """接受当前系统弹窗；没有弹窗时 WDA 返回错误，这里返回 False。"""
        try:
            self._call("POST", self._s("/alert/accept"), {}, timeout=15)
            return True
        except WdaError:
            return False


class WdaHostRegistry:
    """每台设备一个常驻 WdaHost（iproxy 转发 + 必要时拉起的 xcodebuild）。

    录制、实时画面、截图、稳定性测试共用同一个 WDA：各自起停会反复花 30~90s 拉起
    xcodebuild，且同一台设备上两个 iproxy/xcodebuild 会互相抢占。引擎退出时统一关闭。
    """

    def __init__(self, factory=WdaHost) -> None:
        self._factory = factory
        self._hosts: Dict[str, WdaHost] = {}
        self._lock = threading.Lock()

    def get(self, udid: str) -> WdaHost:
        with self._lock:
            host = self._hosts.get(udid)
            if host is None:
                host = self._factory(udid)
                self._hosts[udid] = host
        return host

    def ensure_ready(self, udid: str) -> WdaHost:
        host = self.get(udid)
        host.ensure_ready()
        return host

    def close_all(self) -> None:
        with self._lock:
            hosts = list(self._hosts.values())
            self._hosts.clear()
        for host in hosts:
            try:
                host.close()
            except Exception:  # pragma: no cover - 退出清理尽力而为
                logger.debug("关闭 WDA 主机资源失败", exc_info=True)


registry = WdaHostRegistry()
