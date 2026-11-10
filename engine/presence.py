"""客户端设备存活链路：登录后心跳、退出时 logout。

契约（backend/api/client_devices.py）：
- POST {upstream}/api/client-devices/heartbeat  {serials: [...]}  刷新 last_seen_at
- POST {upstream}/api/client-devices/logout     {serials?: [...], client_instance_id?: str}
  设备置 OFFLINE（带 client_instance_id 时只置本实例登记的设备）

心跳只需要 serial 清单：走轻量 `adb devices`（adb_scan.local_serials，一次命令、
10s 超时），不读属性——云端判离线窗口只有 3 个心跳周期，心跳前跑全量属性扫描
（getprop/wm size/dumpsys，adbutils 默认 600s socket 超时）在设备被投屏/录制
挤占时会拖慢甚至卡住心跳，把插着的设备误判离线。

安全红线：token 只驻留内存（auth 登录响应里摘出），绝不落盘；
upstream 缺失或 token 缺失时整条链路静默跳过（不影响反代主链路）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from engine import config_store

logger = logging.getLogger(__name__)

# 心跳周期：默认 60s；云端 last_seen 容忍窗口 3×心跳周期 ≈ 180s
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0

_token: Optional[str] = None
_heartbeat_task: Optional[asyncio.Task] = None


def heartbeat_interval_seconds() -> float:
    """心跳周期（秒）：env AUTODROID_HEARTBEAT_INTERVAL，测试可调小。"""
    raw = os.environ.get("AUTODROID_HEARTBEAT_INTERVAL", "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    return value if value > 0 else DEFAULT_HEARTBEAT_INTERVAL_SECONDS


def set_token(token: Optional[str]) -> None:
    """登记登录 token（仅内存）。"""
    global _token
    _token = (token or "").strip() or None


def get_token() -> Optional[str]:
    return _token


def clear_token() -> None:
    set_token(None)


def extract_access_token(payload: Any) -> Optional[str]:
    """从登录响应 JSON 提取 access_token（形状对齐 OAuth2 token 响应）。"""
    if not isinstance(payload, dict):
        return None
    token = payload.get("access_token")
    return str(token).strip() if token else None


def _auth_headers() -> Dict[str, str]:
    token = get_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _post_json(
    http: httpx.AsyncClient,
    upstream: str,
    path: str,
    payload: Dict[str, Any],
    timeout: float = 10.0,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[httpx.Response]:
    url = f"{upstream.rstrip('/')}{path}"
    try:
        return await http.post(url, json=payload, headers=headers or _auth_headers(), timeout=timeout)
    except httpx.HTTPError as exc:
        logger.warning("设备存活上报失败: %s -> %s", path, exc)
        return None


def _local_serials_for_heartbeat() -> Optional[List[str]]:
    """本机在线 serial 清单（轻量 `adb devices`）；adb 不可用/失败返回 None。"""
    from engine.device_scan import all_local_serials

    found = all_local_serials()
    if found is None:
        return None
    return sorted(str(s).strip() for s in found if str(s).strip())


async def heartbeat_once(
    http: httpx.AsyncClient,
    serials: Optional[List[str]] = None,
    auth_headers: Optional[Dict[str, str]] = None,
) -> Optional[int]:
    """上报一次心跳；返回 updated 计数（失败/跳过返回 None）。

    auth_headers 显式传入时以其代替内存 token（列表自愈补心跳：以当前请求
    用户身份上报，不依赖登录时是否捕获到 token）。
    """
    upstream = config_store.get_upstream()
    if not upstream or not (auth_headers or get_token()):
        return None
    if serials is None:
        serials = await asyncio.to_thread(_local_serials_for_heartbeat)
        if serials is None:
            logger.warning("本机 adb 清单不可用，跳过本次心跳")
            return None
    if not serials:
        return 0
    resp = await _post_json(
        http, upstream, "/api/client-devices/heartbeat", {"serials": serials}, headers=auth_headers
    )
    if resp is None or resp.status_code // 100 != 2:
        if resp is not None:
            logger.warning("心跳被拒: status=%s body=%s", resp.status_code, resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    updated = data.get("updated") if isinstance(data, dict) else None
    return int(updated) if isinstance(updated, int) else None


async def logout_once(
    http: httpx.AsyncClient,
    serials: Optional[List[str]] = None,
    client_instance_id: Optional[str] = None,
) -> None:
    """退出/进程停止时把本客户端设备置 OFFLINE（best-effort）。

    client_instance_id 传入时云端只置本实例登记的设备：同一账号在另一台机器
    （或本机另一实例）仍在线的设备不受影响，否则会被一并打成离线、直到对方下一次
    心跳才恢复。
    """
    upstream = config_store.get_upstream()
    if not upstream or not get_token():
        return
    payload: Dict[str, Any] = {"serials": serials} if serials is not None else {}
    if client_instance_id:
        payload["client_instance_id"] = client_instance_id
    resp = await _post_json(http, upstream, "/api/client-devices/logout", payload, timeout=5.0)
    if resp is not None and resp.status_code // 100 != 2:
        logger.warning("logout 上报被拒: status=%s body=%s", resp.status_code, resp.text[:200])


async def _heartbeat_loop(http: httpx.AsyncClient) -> None:
    interval = heartbeat_interval_seconds()
    while True:
        try:
            await asyncio.sleep(interval)
            await heartbeat_once(http)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("设备心跳循环异常（继续运行）")


def start_heartbeat_loop(http: httpx.AsyncClient) -> Optional[asyncio.Task]:
    """登录后启动心跳循环（幂等：已有循环不重复启动）。"""
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return _heartbeat_task
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(http))
    return _heartbeat_task


def get_heartbeat_task() -> Optional[asyncio.Task]:
    """当前心跳循环任务（未启动/已结束返回 None）；存活判定与测试观测用。"""
    return _heartbeat_task


def stop_heartbeat_loop() -> None:
    global _heartbeat_task
    task = _heartbeat_task
    _heartbeat_task = None
    if task is not None and not task.done():
        task.cancel()


def reset_for_tests() -> None:
    global _heartbeat_task
    stop_heartbeat_loop()
    clear_token()
