"""HTTP 反向代理工具（httpx 流式透传）。

- 请求体 / 响应体均为流式转发，不整体缓冲；
- 剥离 hop-by-hop 头，Host 由 httpx 按 upstream 重算；
- Content-Encoding（gzip 等）原样透传：响应使用 iter_raw，不做解压；
- Authorization 等业务头原样透传（含多值头，如 set-cookie）；
- 反代超时分级：普通 API 短读超时快速失败，流式/上传等长请求保留长超时；
- 客户端断连（request.is_disconnected / 通道关闭）时取消上游请求与读取，
  避免无主连接长期占用同域连接池。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any, AsyncIterator, FrozenSet, Iterable, List, Optional, Tuple

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# RFC 2616 hop-by-hop 头 + 由代理重算的头（content-length 走 chunked 转发）
HOP_BY_HOP: FrozenSet[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

HeaderList = List[Tuple[str, str]]

# 客户端模式专有头：只允许本地引擎读取（login 分支显式读取后剥离），
# 一律不得透传给上游，防 upstream 侧被注入/混淆服务器地址。
SENSITIVE_REQUEST_HEADERS: FrozenSet[str] = frozenset({"x-autodroid-server"})

# 小于该阈值的请求体整体缓冲转发（保留 content-length），大包仍流式上传
_BUFFER_LIMIT_BYTES = 8 * 1024 * 1024

# ==================== 反代超时分级 ====================
# 普通 API：短读超时快速失败（上游卡死时尽快归还同域连接，避免轮询堆积
# 打满 HTTP/1.1 连接池后把用户点击的请求饿死）；
# 流式/上传/下载等长请求：保留长超时，不能被误杀。
NORMAL_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=30.0, pool=10.0)
LONG_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

# 请求体超过该大小按「大包上传」处理（长超时）
_UPLOAD_LONG_BODY_BYTES = 1 * 1024 * 1024

# 走长超时的路径段特征：流式 / 上传 / 导入导出 / 下载 / 录制 / 视频 / 附件 / SSE。
# 按「路径段」匹配（前后为 /、-、_ 或结尾），避免 pro-file 之类子串误命中。
_LONG_PATH_SEGMENT_RE = re.compile(
    r"/(?:streams?|streaming|uploads?|imports?|exports?|downloads?"
    r"|records?|recordings?|videos?|mjpeg|attachments?|sse)"
    r"(?:/|$|[-_])",
    re.IGNORECASE,
)


def pick_request_timeout(
    method: str,
    path: str,
    content_length: Optional[int] = None,
    accept: str = "",
) -> httpx.Timeout:
    """反代超时分级：普通 API 短读超时；流式/上传/下载等长请求长超时。

    判为长请求（LONG_TIMEOUT）的条件：
    - 请求体超过 _UPLOAD_LONG_BODY_BYTES（大包上传）；
    - 非 GET/HEAD 且未声明 content-length（chunked/未知大小上传）；
    - Accept 声明 text/event-stream（SSE）；
    - 路径命中流式/上传/下载/录制等特征段。
    其余（普通 API）走 NORMAL_TIMEOUT。
    """
    upper_method = (method or "").upper()
    if content_length is not None and int(content_length) > _UPLOAD_LONG_BODY_BYTES:
        return LONG_TIMEOUT
    if upper_method not in ("GET", "HEAD", "OPTIONS") and content_length is None:
        return LONG_TIMEOUT
    if "text/event-stream" in (accept or "").lower():
        return LONG_TIMEOUT
    if _LONG_PATH_SEGMENT_RE.search(path or ""):
        return LONG_TIMEOUT
    return NORMAL_TIMEOUT


def _request_content_length(request: Request) -> Optional[int]:
    """解析 content-length；缺省/非法返回 None（视为未知大小）。"""
    raw_length = request.headers.get("content-length")
    if raw_length and raw_length.isdigit():
        return int(raw_length)
    return None


def filter_headers(headers: Iterable[Tuple[str, str]], drop: FrozenSet[str] = HOP_BY_HOP) -> HeaderList:
    """剔除 hop-by-hop 头（drop 追加剔除项）。"""
    return [(k, v) for k, v in headers if k.lower() not in drop]


def build_target_url(base: str, path: str, query: str = "") -> str:
    """拼接 upstream 与请求路径（base 可带反代路径前缀）。"""
    url = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    url = url + path
    if query:
        url = url + "?" + query
    return url


def apply_headers(response: Response, headers: HeaderList) -> Response:
    """把（可能多值的）头列表写回响应 raw_headers。"""
    raw: List[Tuple[bytes, bytes]] = []
    for key, value in headers:
        raw.append((key.encode("latin-1", "replace"), value.encode("latin-1", "replace")))
    response.raw_headers = raw
    return response


async def _stream_body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk


async def proxy_request(
    http: httpx.AsyncClient,
    request: Request,
    base: str,
    path: Optional[str] = None,
    drop_request_headers: FrozenSet[str] = frozenset(),
    drop_response_headers: FrozenSet[str] = frozenset(),
    timeout: Optional[httpx.Timeout] = None,
) -> Response:
    """把 request 反代到 base+path（缺省沿用原路径），流式返回上游响应。

    timeout 缺省按请求形态分级（见 pick_request_timeout）；客户端断连时
    取消上游请求/读取，不把无主连接留在连接池里。
    """
    target_path = path if path is not None else request.url.path
    target = build_target_url(base, target_path, request.url.query)

    headers = filter_headers(
        request.headers.items(), HOP_BY_HOP | SENSITIVE_REQUEST_HEADERS | drop_request_headers
    )
    # 原样透传客户端 Accept-Encoding；客户端未声明时显式 identity，
    # 避免 httpx 自行协商压缩导致与响应头不一致。
    if not any(k.lower() == "accept-encoding" for k, _ in headers):
        headers.append(("accept-encoding", "identity"))

    method = request.method.upper()
    content: Any = None
    length: Optional[int] = None
    # 请求体完整读入内存后，剩余 receive 消息只剩 http.disconnect，此时才允许
    # is_disconnected 轮询（流式请求体未读完时轮询会吞掉 body 分片）。
    body_fully_read = method in ("GET", "HEAD")
    if method not in ("GET", "HEAD"):
        length = _request_content_length(request)
        if length is not None and length <= _BUFFER_LIMIT_BYTES:
            content = await request.body()
            body_fully_read = True
        else:
            content = _stream_body(request)

    effective_timeout = timeout or pick_request_timeout(
        method,
        target_path,
        length,
        accept=request.headers.get("accept", ""),
    )

    send_task = asyncio.create_task(
        http.send(
            http.build_request(
                method, target, headers=headers, content=content, timeout=effective_timeout
            ),
            stream=True,
        )
    )
    try:
        while True:
            done, _pending = await asyncio.wait(
                {send_task}, timeout=0.25, return_when=asyncio.FIRST_COMPLETED
            )
            if send_task in done:
                break
            if body_fully_read and await request.is_disconnected():
                logger.info("客户端已断开，取消上游请求: %s %s", method, target)
                send_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await send_task
                return JSONResponse(status_code=499, content={"detail": "客户端已断开"})
        upstream_response = send_task.result()
    except httpx.TimeoutException as exc:
        logger.warning("上游超时: %s %s -> %s", method, target, exc)
        return JSONResponse(status_code=504, content={"detail": f"上游服务器超时: {exc}"})
    except httpx.HTTPError as exc:
        logger.warning("上游连接失败: %s %s -> %s", method, target, exc)
        return JSONResponse(status_code=502, content={"detail": f"上游服务器连接失败: {exc}"})
    except asyncio.CancelledError:
        # 本协程被取消（服务器关闭等）时同样收尾上游请求
        send_task.cancel()
        raise

    response_headers = filter_headers(upstream_response.headers.items(), HOP_BY_HOP | drop_response_headers)

    async def _body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
                # 客户端中途断连：停止读上游并尽快关闭，释放连接
                if await request.is_disconnected():
                    logger.info("客户端已断开，终止上游响应读取: %s %s", method, target)
                    break
                yield chunk
        finally:
            await upstream_response.aclose()

    response = StreamingResponse(_body(), status_code=upstream_response.status_code)
    return apply_headers(response, response_headers)


def passthrough_response(response: httpx.Response) -> Response:
    """把已完整读取的 httpx 响应原样转成 FastAPI 响应（错误分支用）。"""
    headers = filter_headers(response.headers.items())
    body = response.content
    headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
    headers.append(("content-length", str(len(body))))
    result = Response(content=body, status_code=response.status_code)
    return apply_headers(result, headers)
