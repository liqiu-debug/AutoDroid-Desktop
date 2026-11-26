"""本机占用中的设备（桌面引擎进程内）。

稳定性测试在客户端本机执行，云端不知道设备被占用：设备列表回源云端后显示「空闲」，
于是同一台设备还能再选去跑稳定性 / UI 自动化。引擎在回源的设备列表上覆盖本机占用：
- 稳定性测试（Android / iOS）→ FASTBOT_RUNNING「跑测中」；
- 本地用例 / 场景执行（run_registry 登记中）→ BUSY「执行中」（只覆盖 IDLE）。
"""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List

FASTBOT = "fastbot"
STARTUP = "startup"
_lock = threading.Lock()
_busy: Dict[str, str] = {}
_owners: Dict[str, str] = {}


def canonical_serial(serial: str) -> str:
    value = str(serial or "").strip()
    # The USB scanner reports ios:<udid>, while drivers may use the bare UDID.
    return value[4:] if value.startswith("ios:") else value


def try_acquire(serial: str, kind: str = FASTBOT, *, owner: str = None) -> bool:
    key = canonical_serial(serial)
    if not key:
        return False
    with _lock:
        if key in _busy:
            return False
        _busy[key] = kind
        _owners[key] = owner or kind
        return True


def release(serial: str, kind: str = FASTBOT, *, owner: str = None) -> None:
    key = canonical_serial(serial)
    with _lock:
        if _busy.get(key) == kind and _owners.get(key) == (owner or kind):
            _busy.pop(key, None)
            _owners.pop(key, None)


def is_busy(serial: str) -> bool:
    with _lock:
        return canonical_serial(serial) in _busy


def exclusive_kind(serial: str) -> str:
    """Only collectors own the WDA session; a case must not reject its own lease."""
    with _lock:
        kind = _busy.get(canonical_serial(serial), "")
    return kind if kind in {FASTBOT, STARTUP} else ""


@contextmanager
def interaction(serial: str):
    """Make recording/interactive I/O atomic with collecting and execution."""
    owner = str(uuid.uuid4())
    if not try_acquire(serial, "interaction", owner=owner):
        kind = exclusive_kind(serial)
        if kind == FASTBOT:
            raise ValueError("该设备正在跑稳定性测试，请等测试结束后再操作")
        if kind == STARTUP:
            raise ValueError("该设备正在执行冷热启动测试，请等测试结束后再操作")
        raise ValueError("该设备正在执行任务，请等执行结束后再操作")
    try:
        yield
    finally:
        release(serial, "interaction", owner=owner)


def snapshot() -> Dict[str, str]:
    with _lock:
        return dict(_busy)


def _running_serials() -> set:
    try:
        from engine.core import run_registry

        return {canonical_serial(r.device_serial) for r in run_registry.registry.active() if r.device_serial}
    except Exception:
        return set()


def has_local_run(serial: str) -> bool:
    """本地用例 / 场景是否正占用该设备。"""
    return canonical_serial(serial) in _running_serials()


def overlay(devices: List[Dict[str, Any]]) -> List[str]:
    """就地覆盖设备状态，返回被改写的 serial。"""
    busy = snapshot()
    running = _running_serials()
    changed: List[str] = []
    for device in devices:
        serial = canonical_serial(device.get("serial"))
        if not serial:
            continue
        status = str(device.get("status") or "").strip().upper()
        if busy.get(serial) == FASTBOT:
            if status != "FASTBOT_RUNNING":
                device["status"] = "FASTBOT_RUNNING"
                changed.append(serial)
        elif busy.get(serial) == STARTUP:
            device["status"] = "BUSY"
            device["busy_kind"] = STARTUP
            changed.append(serial)
        elif serial in running and status == "IDLE":
            device["status"] = "BUSY"
            changed.append(serial)
    return changed


def reset_for_tests() -> None:
    with _lock:
        _busy.clear()
        _owners.clear()
