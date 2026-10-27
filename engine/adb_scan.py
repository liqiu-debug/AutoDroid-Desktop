"""本机 adb 设备扫描（adbutils）。

adb 路径发现（find_adb_path）是全链路唯一入口——subprocess/adbutils/uiautomator2
的 adb 可执行路径都从这里解析：

    显式参数 > env AUTODROID_ADB > env ADBUTILS_ADB_PATH
    > 内置 platform-tools（打包态 resourcesPath/platform-tools/adb[.exe]；
      开发态 assets/platform-tools/<darwin|windows>/（兼容旧扁平布局），
      由 scripts/fetch-platform-tools.sh 产出）
    > PATH > ANDROID_HOME / ANDROID_SDK_ROOT 的 platform-tools、
      %LOCALAPPDATA%\\Android\\Sdk\\platform-tools（Windows）
      / ~/Android/platform-tools、~/Library/Android/sdk/platform-tools
      等常见 SDK 安装路径（兜底）。

adbutils（uiautomator2 经由 adbutils）通过 ADBUTILS_ADB_PATH 选择 adb 路径，
apply_adb_path 负责把解析结果注入该环境变量，保证「一个 adb 管到底」。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

AUTODROID_ADB_ENV = "AUTODROID_ADB"
ADBUTILS_ADB_PATH_ENV = "ADBUTILS_ADB_PATH"

# G3：adb 解析失败的统一中文指引（resolve_adb_binary 抛错 / scan_devices 日志共用）
ADB_MISSING_HINT = (
    "未检测到 adb：请安装 Android platform-tools 或使用内置 adb"
    "（把 adb 所在目录加入 PATH，或设置 ANDROID_HOME / AUTODROID_ADB 指向 adb）"
)

_server_ready = False


def _adb_exe_name() -> str:
    return "adb.exe" if os.name == "nt" else "adb"


def _platform_tools_dir_name() -> str:
    """开发态分平台目录名（fetch-platform-tools.sh / build-windows.ps1 产出目录）。"""
    if os.name == "nt":
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def _bundled_platform_tools_candidates() -> List[str]:
    """内置 platform-tools 的 adb 候选路径（打包态 resourcesPath、开发态仓库 assets）。

    开发态按分平台目录（assets/platform-tools/<darwin|windows>/）优先，兼容
    旧扁平布局 assets/platform-tools/；打包态 extraResources 已把分平台目录
    内容平铺到 resourcesPath/platform-tools/（扁平优先）。
    """
    exe_name = _adb_exe_name()
    candidates: List[str] = []
    # 打包态：extraResources 把 assets/platform-tools/<平台>/ 平铺到
    # resourcesPath/platform-tools/，AUTODROID_ENGINE_ASSETS_DIR（resourcesPath/assets）
    # 的同级目录即同一位置的显式锚点。
    assets_dir = os.environ.get("AUTODROID_ENGINE_ASSETS_DIR", "").strip()
    if assets_dir:
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(assets_dir)), "platform-tools", exe_name)
        )
    # 打包态兜底：引擎 onedir 位于 resourcesPath/engine-bin/<name>/，从可执行文件向上
    # 回溯即可命中 resourcesPath/platform-tools（AUTODROID_ENGINE_ASSETS_DIR 缺席时）。
    if getattr(sys, "frozen", False):
        probe = os.path.dirname(os.path.abspath(sys.executable))
        for _ in range(4):
            candidates.append(os.path.join(probe, "platform-tools", exe_name))
            probe = os.path.dirname(probe)
    # 开发态：仓库 assets/platform-tools/<平台>/（fetch 脚本产物）优先，
    # 兼容旧扁平布局 assets/platform-tools/
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(
        os.path.join(repo_root, "assets", "platform-tools", _platform_tools_dir_name(), exe_name)
    )
    candidates.append(os.path.join(repo_root, "assets", "platform-tools", exe_name))
    return candidates


def _sdk_candidates() -> List[str]:
    """常见 Android SDK 安装路径的 adb 候选（最终兜底）。

    顺序：ANDROID_HOME / ANDROID_SDK_ROOT 指向的 platform-tools（用户显式配置）
    > 各平台常见安装路径（含 ~/Android/platform-tools 这类用户机器真实布局）。
    """
    exe_name = _adb_exe_name()
    home = os.path.expanduser("~")
    candidates: List[str] = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(env_name, "").strip()
        if value:
            candidates.append(os.path.join(value, "platform-tools", exe_name))
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.extend(
            [
                os.path.join(local_appdata, "Android", "Sdk", "platform-tools", exe_name),
                os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools", exe_name),
                r"C:\platform-tools\adb.exe",
            ]
        )
    else:
        candidates.extend(
            [
                os.path.join(home, "Android", "platform-tools", "adb"),
                os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
                os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
                "/opt/homebrew/bin/adb",
                "/usr/local/bin/adb",
            ]
        )
    return candidates


def find_adb_path(explicit: Optional[str] = None) -> Optional[str]:
    """定位 adb 可执行文件（全链路唯一入口）。

    优先级：显式参数 > env AUTODROID_ADB > env ADBUTILS_ADB_PATH
    > 内置 platform-tools > PATH > 常见 SDK 安装路径。
    """
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    for env_name in (AUTODROID_ADB_ENV, ADBUTILS_ADB_PATH_ENV):
        value = os.environ.get(env_name, "").strip()
        if value:
            candidates.append(value)
    candidates.extend(_bundled_platform_tools_candidates())
    found = shutil.which("adb")
    if found:
        candidates.append(found)
    candidates.extend(_sdk_candidates())
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def resolve_adb_binary() -> str:
    """subprocess argv[0] 用的 adb 路径。

    G3：解析不到时不再回退裸 "adb"（下游只会抛原始 FileNotFoundError，无指引），
    直接抛带统一中文指引的 FileNotFoundError（ADB_MISSING_HINT）。
    """
    resolved = find_adb_path()
    if not resolved:
        raise FileNotFoundError(ADB_MISSING_HINT)
    return resolved


def apply_adb_path(path: Optional[str] = None) -> Optional[str]:
    """把解析结果注入 ADBUTILS_ADB_PATH（adbutils/uiautomator2 全链路统一）。"""
    resolved = path or find_adb_path()
    if resolved:
        os.environ[ADBUTILS_ADB_PATH_ENV] = resolved
    return resolved


def ensure_server_started(adb: Optional[str] = None) -> bool:
    """发现 adb、注入 adbutils 环境并 start-server 一次自愈（进程内只跑一次）。"""
    global _server_ready
    path = apply_adb_path(adb)
    if not path:
        # G3：失败日志统一携带 adb 缺失指引
        logger.warning("%s，跳过本机设备扫描", ADB_MISSING_HINT)
        return False
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
    if not ensure_server_started(adb):
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
        # G3：扫描失败日志统一携带 adb 指引（排查时先确认 adb 可用）
        logger.warning("adb 设备列表获取失败: %s（%s）", exc, ADB_MISSING_HINT)
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
