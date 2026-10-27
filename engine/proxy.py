"""HTTP 反向代理工具（httpx 流式透传）。

- 请求体 / 响应体均为流式转发，不整体缓冲；
- 剥离 hop-by-hop 头，Host 由 httpx 按 upstream 重算；
- Content-Encoding（gzip 等）原样透传：响应使用 iter_raw，不做解压；
- Authorization 等业务头原样透传（含多值头，如 set-cookie）。
"""
from __future__ import annotations

import logging
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
) -> Response:
    """把 request 反代到 base+path（缺省沿用原路径），流式返回上游响应。"""
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
    if method not in ("GET", "HEAD"):
        length: Optional[int] = None
        raw_length = request.headers.get("content-length")
        if raw_length and raw_length.isdigit():
            length = int(raw_length)
        if length is not None and length <= _BUFFER_LIMIT_BYTES:
            content = await request.body()
        else:
            content = _stream_body(request)

    try:
        upstream_request = http.build_request(method, target, headers=headers, content=content)
        upstream_response = await http.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        logger.warning("上游超时: %s %s -> %s", method, target, exc)
        return JSONResponse(status_code=504, content={"detail": f"上游服务器超时: {exc}"})
    except httpx.HTTPError as exc:
        logger.warning("上游连接失败: %s %s -> %s", method, target, exc)
        return JSONResponse(status_code=502, content={"detail": f"上游服务器连接失败: {exc}"})

    response_headers = filter_headers(upstream_response.headers.items(), HOP_BY_HOP | drop_response_headers)

    async def _body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
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
