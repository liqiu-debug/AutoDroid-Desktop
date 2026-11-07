"""本机占用中的设备（桌面引擎进程内）。

稳定性测试在客户端本机执行，云端不知道设备被占用：设备列表回源云端后显示「空闲」，
于是同一台设备还能再选去跑稳定性 / UI 自动化。引擎在回源的设备列表上覆盖本机占用：
- 稳定性测试（Android / iOS）→ FASTBOT_RUNNING「跑测中」；
- 本地用例 / 场景执行（run_registry 登记中）→ BUSY「执行中」（只覆盖 IDLE）。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List

FASTBOT = "fastbot"
_lock = threading.Lock()
_busy: Dict[str, str] = {}


def try_acquire(serial: str, kind: str = FASTBOT) -> bool:
    key = str(serial or "").strip()
    if not key:
        return False
    with _lock:
        if key in _busy:
            return False
        _busy[key] = kind
        return True


def release(serial: str, kind: str = FASTBOT) -> None:
    key = str(serial or "").strip()
    with _lock:
        if _busy.get(key) == kind:
            _busy.pop(key, None)


def is_busy(serial: str) -> bool:
    with _lock:
        return str(serial or "").strip() in _busy


def snapshot() -> Dict[str, str]:
    with _lock:
        return dict(_busy)


def _running_serials() -> set:
    try:
        from engine.core import run_registry

        return {r.device_serial for r in run_registry.registry.active() if r.device_serial}
    except Exception:
        return set()


def has_local_run(serial: str) -> bool:
    """本地用例 / 场景是否正占用该设备。"""
    return str(serial or "").strip() in _running_serials()


def overlay(devices: List[Dict[str, Any]]) -> List[str]:
    """就地覆盖设备状态，返回被改写的 serial。"""
    busy = snapshot()
    running = _running_serials()
    changed: List[str] = []
    for device in devices:
        serial = str(device.get("serial") or "").strip()
        if not serial:
            continue
        status = str(device.get("status") or "").strip().upper()
        if busy.get(serial) == FASTBOT:
            if status != "FASTBOT_RUNNING":
                device["status"] = "FASTBOT_RUNNING"
                changed.append(serial)
        elif serial in running and status == "IDLE":
            device["status"] = "BUSY"
            changed.append(serial)
    return changed


def reset_for_tests() -> None:
    with _lock:
        _busy.clear()
