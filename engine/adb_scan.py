"""本机 adb 设备扫描（adbutils）。

adb 路径发现逻辑与 AutoDroid/scripts/device_agent.py 的 find_adb_path 对齐：
显式指定 > PATH > %LOCALAPPDATA%\\Android\\Sdk\\platform-tools（Windows）
/ ~/Library/Android/sdk/platform-tools 等常见 SDK 安装路径。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_server_ready = False


def find_adb_path(explicit: Optional[str] = None) -> Optional[str]:
    """定位 adb 可执行文件：显式参数 > PATH > 常见 SDK 安装路径。"""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    found = shutil.which("adb")
    if found:
        candidates.append(found)
    home = os.path.expanduser("~")
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.extend(
            [
                os.path.join(local_appdata, "Android", "Sdk", "platform-tools", "adb.exe"),
                os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
                r"C:\platform-tools\adb.exe",
            ]
        )
    else:
        candidates.extend(
            [
                os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
                os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
                "/opt/homebrew/bin/adb",
                "/usr/local/bin/adb",
            ]
        )
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _ensure_adb(adb: Optional[str]) -> bool:
    """发现 adb 并确保 adb server 在位，返回是否可用。"""
    global _server_ready
    path = adb or find_adb_path()
    if not path:
        logger.warning("未找到 adb 可执行文件，跳过本机设备扫描")
        return False
    # adbutils 通过该环境变量选择 adb 路径
    os.environ.setdefault("ADBUTILS_ADB_PATH", path)
    if not _server_ready:
        try:
            subprocess.run([path, "start-server"], capture_output=True, timeout=15, check=False)
        except Exception as exc:
            logger.warning("启动 adb server 失败（继续尝试连接）: %s", exc)
        _server_ready = True
    return True


def _connection_type(serial: str) -> str:
    """远程隧道形态（127.0.0.1:<端口>）标记 remote_usb，其余为 usb。"""
    if ":" in serial and serial.split(":", 1)[0].replace(".", "").isdigit():
        return "remote_usb"
    return "usb"


def scan_devices(adb: Optional[str] = None) -> List[Dict[str, Any]]:
    """扫描本机 adb 设备，返回 register 上报所需的元数据列表。

    仅包含 state == device 的 Android 设备；单台属性读取失败不阻塞其余设备。
    """
    if not _ensure_adb(adb):
        return []

    try:
        import adbutils
    except ImportError as exc:
        logger.warning("adbutils 未安装，跳过本机设备扫描: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    try:
        devices = adbutils.adb.device_list()
    except Exception as exc:
        logger.warning("adb 设备列表获取失败: %s", exc)
        return []

    for device in devices:
        serial = str(device.serial or "").strip()
        if not serial:
            continue
        try:
            state = device.get_state()
        except Exception as exc:
            logger.warning("读取 %s 状态失败: %s", serial, exc)
            continue
        if state != "device":
            logger.info("跳过非在线设备 %s（state=%s）", serial, state)
            continue

        model = ""
        brand = ""
        version = ""
        resolution = ""
        try:
            prop = device.prop
            model = str(prop.model or "").strip()
            brand = str(prop.get("ro.product.brand") or "").strip().upper()
            version = str(prop.get("ro.build.version.release") or "").strip()
        except Exception as exc:
            logger.warning("读取 %s 属性失败: %s", serial, exc)
        try:
            size = device.window_size(landscape=False)
            resolution = f"{size.width}x{size.height}" if size else ""
        except Exception as exc:
            logger.warning("读取 %s 分辨率失败: %s", serial, exc)

        results.append(
            {
                "platform": "android",
                "serial": serial,
                "model": model or "Unknown",
                "brand": brand,
                "android_version": version,
                "os_version": version,
                "resolution": resolution,
                "connection_type": _connection_type(serial),
            }
        )
    return results


def local_serials(adb: Optional[str] = None) -> Optional[set]:
    """本机 adb 在线设备 serial 集合（轻量：只解析 `adb devices`，不读属性）。

    - 返回 set：解析成功（可能为空）——调用方可据此过滤本机在场设备；
    - 返回 None：adb 不可用/命令失败——调用方视为「未知」，不做设备过滤（fail-open），
      避免 adb 瞬时异常时整批定时任务被静默吞掉。
    """
    path = adb or find_adb_path()
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, "devices"], capture_output=True, timeout=10, check=False
        )
    except Exception as exc:
        logger.warning("adb devices 执行失败（视为未知清单，不做过滤）: %s", exc)
        return None
    if out.returncode != 0:
        logger.warning(
            "adb devices 返回 %s（视为未知清单，不做过滤）: %s",
            out.returncode,
            (out.stderr or b"").decode(errors="ignore")[:200],
        )
        return None
    serials = set()
    for line in (out.stdout or b"").decode(errors="ignore").splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.add(parts[0])
    return serials
