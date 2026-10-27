"""ADB 协程工具：shell 执行、文件推送/拉取、进程终止等基础能力。

T5：全部改 create_subprocess_exec 参数数组（不经宿主 shell），并对
device_serial/包名做严格白名单校验——杜绝命令注入面。
"""
import asyncio
import re
from typing import Dict, Optional

from engine.adb_scan import resolve_adb_binary

ADB_MONITOR_COMMAND_TIMEOUT_SECONDS = 8

# serial 白名单（adb/网络隧道形态）；包名白名单（Android 包命名规范）
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")


def validate_device_serial(device_serial: str) -> str:
    """严格校验设备 serial（白名单），非法即抛 ValueError。"""
    value = str(device_serial or "").strip()
    if not value or not _SERIAL_PATTERN.match(value):
        raise ValueError(f"非法设备 serial: {device_serial!r}")
    return value


def validate_package_name(package_name: str) -> str:
    """严格校验 Android 包名（白名单），非法即抛 ValueError。"""
    value = str(package_name or "").strip()
    if not value or not _PACKAGE_PATTERN.match(value):
        raise ValueError(f"非法包名: {package_name!r}")
    return value


async def _adb_shell(device_serial: str, cmd: str, timeout: Optional[float] = None) -> str:
    result = await _adb_shell_result(device_serial, cmd, timeout=timeout)
    return result["stdout"]


async def _terminate_subprocess(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=1)
        return
    except Exception:
        pass

    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=1)
    except Exception:
        pass


async def _adb_shell_result(
    device_serial: str,
    cmd: str,
    timeout: Optional[float] = None,
) -> Dict[str, object]:
    serial = validate_device_serial(device_serial)
    # exec 参数数组：cmd 整串作为单个 argv 交给设备侧 shell 解释（与原
    # `adb -s x shell {cmd}` 语义一致），宿主 shell 不再参与拼接。
    proc = await asyncio.create_subprocess_exec(
        resolve_adb_binary(), "-s", serial, "shell", str(cmd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        return {
            "stdout": "",
            "stderr": f"adb shell timeout after {timeout}s: {cmd}",
            "returncode": -1,
        }
    return {
        "stdout": stdout.decode(errors="ignore").strip(),
        "stderr": stderr.decode(errors="ignore").strip(),
        "returncode": proc.returncode,
    }


async def _adb_push(device_serial: str, local: str, remote: str):
    serial = validate_device_serial(device_serial)
    proc = await asyncio.create_subprocess_exec(
        resolve_adb_binary(), "-s", serial, "push", str(local), str(remote),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _adb_pull(device_serial: str, remote: str, local: str):
    serial = validate_device_serial(device_serial)
    proc = await asyncio.create_subprocess_exec(
        resolve_adb_binary(), "-s", serial, "pull", str(remote), str(local),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        raise RuntimeError(message or f"adb pull failed: {remote}")


async def _check_remote_file(device_serial: str, remote_path: str) -> bool:
    """检查设备上文件是否存在"""
    result = await _adb_shell(
        device_serial,
        f"ls {remote_path} 2>/dev/null",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    return bool(result and "No such file" not in result)


async def _get_device_sdk_int(device_serial: str) -> int:
    sdk_output = await _adb_shell(
        device_serial,
        "getprop ro.build.version.sdk",
        timeout=ADB_MONITOR_COMMAND_TIMEOUT_SECONDS,
    )
    return int(sdk_output.strip()) if sdk_output.strip().isdigit() else 0
