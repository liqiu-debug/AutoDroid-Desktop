# 来源：语义对齐 AutoDroid backend/api/packages.py（install_package /
#       install_app_package_to_device 的 adb 安装分支）。
"""App 包安装本地化：云端下载包文件 → 本地 adb install。

拦截 POST /api/packages/{id}/install（形状对齐 packages.install_package 响应）：
- 元数据：GET /api/packages/（分页定位 AppPackageRead）
- 下载：GET /api/packages/{id}/download
- 安装：adb -s <serial> install -r -t -d（降级失败自动卸载重试，对齐平台语义）

iOS（devicectl）链路依赖 Xcode 工具链与本地配对，桌面引擎 P2 未本地化——
返回 501 明确提示（不静默反代云端：设备挂在本机，云端 adb/devicectl 均不可达）。
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from engine.adb_scan import ADB_MISSING_HINT, resolve_adb_binary
from engine.core.cloud_bridge import CloudBridge

logger = logging.getLogger(__name__)

_INSTALL_TIMEOUT_SECONDS = 480


class PackageInstallError(Exception):
    """安装失败（映射为 HTTP detail）。"""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def _run_adb_command(args: list, timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        # G3：统一中文指引（与 resolve_adb_binary 同一文案）
        raise PackageInstallError(ADB_MISSING_HINT, 503) from exc
    except subprocess.TimeoutExpired as exc:
        raise PackageInstallError("与设备通信超时（可能设备已断开）", 504) from exc
    output = completed.stdout.decode("utf-8", errors="replace") + completed.stderr.decode(
        "utf-8", errors="replace"
    )
    return output


def _parse_adb_install_error(output: str) -> str:
    failure_match = re.search(r"Failure\s*\[([^\]]+)\]", output or "")
    return failure_match.group(1) if failure_match else str(output or "").strip()[-200:]


def install_package_local(
    bridge: CloudBridge,
    package_id: int,
    serial: str,
) -> Dict[str, Any]:
    """下载云端安装包到本地并 adb install 到目标设备（响应形状对齐 packages.py）。"""
    serial = str(serial or "").strip()
    if not serial:
        raise PackageInstallError("请选择目标设备", 400)

    try:
        pkg = bridge.fetch_package(package_id)
    except FileNotFoundError:
        raise PackageInstallError("安装包不存在", 404)
    except Exception as exc:
        raise PackageInstallError(f"获取安装包信息失败: {exc}", 502)

    package_platform = str(pkg.get("platform") or "android").strip().lower()
    app_name = str(pkg.get("app_name") or "Unknown")
    package_name = str(pkg.get("package_name") or "")
    version_name = str(pkg.get("version_name") or "")
    version_code = str(pkg.get("version_code") or "")

    if package_platform != "android":
        raise PackageInstallError(
            "iOS IPA 一键安装暂未在桌面引擎本地化（P2 仅 Android adb install）", 501
        )

    try:
        file_bytes = bridge.download_package(package_id)
    except Exception as exc:
        raise PackageInstallError(f"安装包下载失败: {exc}", 502)
    if not file_bytes:
        raise PackageInstallError("安装包文件已被删除", 404)

    suffix = ".apk"
    with tempfile.TemporaryDirectory(prefix="autodroid-pkg-") as temp_dir:
        apk_path = Path(temp_dir) / f"package_{int(package_id)}{suffix}"
        apk_path.write_bytes(file_bytes)

        install_flags = ["-r", "-t", "-d"]
        # G3：adb 解析失败统一映射为 503 + 中文指引——resolve_adb_binary 在拼 argv
        # 处抛 FileNotFoundError，若不在此处接住会以 500 Internal Server Error 冒出，
        # ADB_MISSING_HINT 文案到不了用户（原先裸 "adb" 回退由 subprocess 抛出并被
        # _run_adb_command 换成 503，行为必须保持）。
        try:
            adb_bin = resolve_adb_binary()
        except FileNotFoundError as exc:
            raise PackageInstallError(str(exc) or ADB_MISSING_HINT, 503) from exc
        install_cmd = [adb_bin, "-s", serial, "install", *install_flags, str(apk_path)]
        logger.info("执行本地安装命令: %s", " ".join(shlex.quote(part) for part in install_cmd))
        output = _run_adb_command(install_cmd, timeout=_INSTALL_TIMEOUT_SECONDS)
        logger.info("ADB 安装输出: %s", output.strip())

        retried_after_uninstall = False
        if "INSTALL_FAILED_VERSION_DOWNGRADE" in output and package_name:
            retried_after_uninstall = True
            logger.warning("检测到系统拦截降级安装，自动卸载旧版本并重试 (%s)", package_name)
            _run_adb_command([adb_bin, "-s", serial, "uninstall", package_name], timeout=45)
            output = _run_adb_command(install_cmd, timeout=_INSTALL_TIMEOUT_SECONDS)
            logger.info("重试安装输出: %s", output.strip())

    if "Success" not in output:
        raise PackageInstallError(f"安装失败: {_parse_adb_install_error(output)}", 500)

    return {
        "success": True,
        "msg": f"{app_name} v{version_name} 安装成功",
        "package_id": int(package_id),
        "platform": "android",
        "installer": "adb",
        "package_name": package_name,
        "version_name": version_name,
        "version_code": version_code,
        "output": output,
        "retried_after_uninstall": retried_after_uninstall,
    }
