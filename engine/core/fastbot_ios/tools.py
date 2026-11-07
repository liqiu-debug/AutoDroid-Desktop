"""外部工具解析与 iOS 设备发现（libimobiledevice，可选 xcrun/pymobiledevice3）。"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ios: 前缀是 AutoDroid-iOS App 形态（IDFV）的口径；USB 真机与平台 ios_scanner 一致用裸 UDID。
IOS_SERIAL_PREFIX = "ios:"
IOS_USB_CONNECTION_TYPE = "usb"
_UDID_RE = re.compile(r"^[0-9A-Fa-f-]{20,64}$")
# 真机 UDID 形态：新式 8-16 位十六进制、旧式 40 位十六进制（与 Android serial 可区分）
_USB_UDID_RE = re.compile(r"^(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40})$")
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def which(name: str) -> Optional[str]:
    env_key = f"AUTODROID_{name.upper().replace('-', '_')}"
    override = os.environ.get(env_key)
    if override and os.path.isfile(override):
        return override
    found = shutil.which(name)
    if found:
        return found
    for base in _EXTRA_BIN_DIRS:  # Electron 启动的引擎 PATH 常不含 Homebrew
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ios_tooling_available() -> bool:
    return bool(which("idevice_id"))


def is_ios_serial(serial: str) -> bool:
    text = str(serial or "").strip()
    return text.startswith(IOS_SERIAL_PREFIX) or bool(_USB_UDID_RE.match(text))


def udid_from_serial(serial: str) -> str:
    udid = str(serial or "").strip()
    if udid.startswith(IOS_SERIAL_PREFIX):
        udid = udid[len(IOS_SERIAL_PREFIX):].strip()
    if not _UDID_RE.match(udid):
        raise ValueError(f"非法 iOS UDID: {udid!r}")
    return udid


def _run(args: List[str], timeout: float = 10) -> Optional[str]:
    try:
        out = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("命令失败 %s: %s", args[0], exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def list_udids() -> Optional[List[str]]:
    """USB 连接的 iOS UDID 列表；工具缺失返回 None（未知），无设备返回 []。"""
    if os.environ.get("AUTODROID_DISABLE_IOS_SCAN") == "1":
        return None
    exe = which("idevice_id")
    if not exe:
        return None
    text = _run([exe, "-l"], timeout=8)
    if text is None:
        return None
    return [line.strip() for line in text.splitlines() if _UDID_RE.match(line.strip())]


def local_ios_serials() -> Optional[set]:
    udids = list_udids()
    if udids is None:
        return None
    return set(udids)


def device_info(udid: str) -> Dict[str, str]:
    exe = which("ideviceinfo")
    info: Dict[str, str] = {}
    if not exe:
        return info
    text = _run([exe, "-u", udid], timeout=10) or ""
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            info[key.strip()] = value.strip()
    return info


def devicectl_marketing_names() -> Dict[str, str]:
    """xcrun devicectl 报告的 {UDID: 市场名}（需 Xcode；不可用返回 {}，由机型表兜底）。"""
    import json
    import tempfile

    xcrun = which("xcrun") or ("/usr/bin/xcrun" if os.path.exists("/usr/bin/xcrun") else None)
    if not xcrun:
        return {}
    with tempfile.TemporaryDirectory(prefix="autodroid-devicectl-") as tmp:
        out_path = os.path.join(tmp, "devices.json")
        if _run([xcrun, "devicectl", "list", "devices", "--json-output", out_path], timeout=15) is None:
            return {}
        try:
            with open(out_path, encoding="utf-8") as handle:
                devices = (json.load(handle).get("result") or {}).get("devices") or []
        except (OSError, ValueError):
            return {}
    names: Dict[str, str] = {}
    for item in devices:
        hardware = item.get("hardwareProperties") or {}
        udid, name = str(hardware.get("udid") or ""), str(hardware.get("marketingName") or "")
        if udid and name:
            names[udid] = name
    return names


def scan_ios_devices() -> List[Dict[str, str]]:
    """register 上报载荷（形状同 adb_scan.scan_devices）。

    model 上报市场名（如 iPhone 14 Pro），而不是机型标识 ProductType（iPhone15,2）。
    """
    from engine.core.fastbot_ios.models import marketing_name

    udids = list_udids() or []
    result: List[Dict[str, str]] = []
    market_names = devicectl_marketing_names() if udids else {}
    for udid in udids:
        info = device_info(udid)
        product_type = info.get("ProductType") or ""
        result.append(
            {
                "platform": "ios",
                "serial": udid,
                "model": market_names.get(udid) or marketing_name(product_type) or "iPhone",
                "brand": "APPLE",
                "android_version": "",
                "os_version": info.get("ProductVersion") or "",
                "resolution": "",
                "custom_name": None,  # 不覆盖用户在设备中心的改名
                "connection_type": IOS_USB_CONNECTION_TYPE,
            }
        )
    return result
