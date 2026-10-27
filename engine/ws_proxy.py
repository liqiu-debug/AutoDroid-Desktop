"""WebSocket 反向代理（/ws/* → upstream）。

P0 契约允许 /ws/* 先返回 501（P1）；这里直接实现消息双向透传，
投屏等依赖链路可直连 upstream。握手失败（upstream 不可达等）即刻拒绝连接。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Dict, List, Tuple

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

# 透传到 upstream 的握手头
_FORWARDED_HEADERS = ("authorization", "cookie", "user-agent")


def ws_target_url(base: str, path: str, query: str = "") -> str:
    """http(s) upstream → ws(s) 目标地址。"""
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = base
    target = ws_base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    target = target + path
    if query:
        target = target + "?" + query
    return target


def _connect_kwargs(headers: List[Tuple[str, str]], subprotocols: List[str]) -> Dict[str, Any]:
    """兼容 websockets 新旧客户端 API（extra_headers / additional_headers）。"""
    import websockets

    kwargs: Dict[str, Any] = {"open_timeout": 15, "max_size": None}  # 投屏帧可能超过默认 1MB 上限
    if subprotocols:
        kwargs["subprotocols"] = subprotocols
    if headers:
        params = inspect.signature(websockets.connect).parameters
        key = "additional_headers" if "additional_headers" in params else "extra_headers"
        kwargs[key] = headers
    return kwargs


async def _recv_from_client(websocket: WebSocket):
    """Starlette WebSocket 收消息（ASGI 消息 → 文本/字节）。"""
    message = await websocket.receive()
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    if message.get("text") is not None:
        return message["text"]
    return message.get("bytes") or b""


async def _send_to_client(websocket: WebSocket, payload) -> None:
    if isinstance(payload, (bytes, bytearray)):
        await websocket.send_bytes(bytes(payload))
    else:
        await websocket.send_text(payload)


async def _pump(src, dst, to_client: bool) -> None:
    """单向转发消息，任一端断开即抛出结束。"""
    while True:
        if to_client:
            message = await src.recv()  # websockets 客户端
            await _send_to_client(dst, message)
        else:
            message = await _recv_from_client(src)
            await dst.send(message)


async def proxy_websocket(websocket: WebSocket, base: str, path: str) -> None:
    """把 /ws/* 反代到 upstream；upstream 不可达时拒绝握手（协议级失败）。"""
    import websockets

    target = ws_target_url(base, path, websocket.url.query)
    headers = [
        (key, value)
        for key, value in websocket.headers.items()
        if key.lower() in _FORWARDED_HEADERS and value
    ]
    subprotocols = [s.strip() for s in (websocket.scope.get("subprotocols") or []) if s.strip()]

    try:
        async with websockets.connect(target, **_connect_kwargs(headers, subprotocols)) as upstream:
            chosen = getattr(upstream, "subprotocol", None)
            await websocket.accept(subprotocol=chosen)
            pumps = [
                asyncio.create_task(_pump(upstream, websocket, True)),
                asyncio.create_task(_pump(websocket, upstream, False)),
            ]
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(
                    exc, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed, asyncio.CancelledError)
                ):
                    logger.warning("WebSocket 代理消息转发异常: %s", exc)
    except WebSocketDisconnect:
        logger.info("客户端 WebSocket 断开: %s", path)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("WebSocket 代理失败: %s -> %s (%s)", path, target, exc)
        try:
            # 未 accept 直接 close = 拒绝握手；已 accept = 以 1011 关闭
            await websocket.close(code=1011, reason=str(exc)[:120])
        except Exception:
            pass
        return

    if websocket.client_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
