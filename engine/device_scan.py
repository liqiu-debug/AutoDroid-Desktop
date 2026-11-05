"""本机设备汇总：adb（Android）+ libimobiledevice（iOS USB，serial=ios:<udid>）。

iOS 工具缺失时只返回 Android 结果，不影响原链路。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from engine import adb_scan
from engine.core.fastbot_ios import tools as ios_tools

logger = logging.getLogger(__name__)


def scan_all_devices() -> List[Dict[str, str]]:
    infos = list(adb_scan.scan_devices())
    try:
        infos.extend(ios_tools.scan_ios_devices())
    except Exception as exc:
        logger.warning("iOS 设备扫描失败（忽略）: %s", exc)
    return infos


def all_local_serials() -> Optional[set]:
    """None 仅当两侧都未知；任一侧可知即返回其并集。"""
    android = adb_scan.local_serials()
    try:
        ios = ios_tools.local_ios_serials()
    except Exception:
        ios = None
    if android is None and ios is None:
        return None
    return set(android or ()) | set(ios or ())
