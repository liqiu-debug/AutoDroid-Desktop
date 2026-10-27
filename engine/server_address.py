"""服务器地址归一化（与 AutoDroid-Android net/ServerAddress.kt 语义对齐）。

规则（对应 ServerAddress.normalize）：
- 必须以 http:// 或 https:// 开头（scheme 大小写不敏感）；
- 禁止 userinfo（用户名/密码）、query、fragment；
- scheme/host 小写、省略默认端口、解析 . / .. 点段；
- 去掉尾部 / 与尾部 /api，保留其余反代路径前缀。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

# 与 Kotlin 版 require 失败时的文案保持一致（前端直接展示）
INVALID_MESSAGE = "请输入有效的平台地址，例如 http://192.168.1.10:8000"

_DEFAULT_PORTS = {"http": 80, "https": 443}


class ServerAddressError(ValueError):
    """非法的服务器地址。"""


def _resolve_path(path: str) -> str:
    """按 OkHttp HttpUrl 的方式解析点段（. / ..），保留空段与尾部斜杠。"""
    if not path:
        return "/"
    trailing_slash = path.endswith("/")
    segments = path.split("/")[1:]  # 绝对路径首段恒为空
    out = []
    for seg in segments:
        if seg == ".":
            continue
        if seg == "..":
            if out and out[-1] != "..":
                out.pop()
            continue
        out.append(seg)
    resolved = "/" + "/".join(out)
    if trailing_slash and not resolved.endswith("/"):
        resolved += "/"
    return resolved


def normalize(value: Optional[str]) -> str:
    """归一化服务器地址；非法输入抛 ServerAddressError（文案同 Kotlin 版）。"""
    text = (value or "").strip().rstrip("/")
    lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ServerAddressError(INVALID_MESSAGE)

    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port  # 非法端口会抛 ValueError
    except ValueError as exc:
        raise ServerAddressError(INVALID_MESSAGE) from exc

    if not host or not parts.netloc:
        raise ServerAddressError(INVALID_MESSAGE)
    if parts.username is not None or parts.password is not None:
        raise ServerAddressError(INVALID_MESSAGE)
    # query/fragment 一律拒绝：urlsplit 对空 query/fragment（"?"、"#" 结尾）解析为空串，
    # 只看 parts.query/fragment 会漏判；对齐 ServerAddress.kt/OkHttp 直接拒原文。
    if "?" in text or "#" in text:
        raise ServerAddressError(INVALID_MESSAGE)
    if parts.query or parts.fragment:
        raise ServerAddressError(INVALID_MESSAGE)

    scheme = parts.scheme.lower()
    host = host.lower()
    # 主机/netloc 含反斜杠拒绝（OkHttp 拒绝集；urlsplit 会把 \ 当普通字符吞进 host）
    if "\\" in parts.netloc or "\\" in host:
        raise ServerAddressError(INVALID_MESSAGE)
    if any(ch in host for ch in " /?#@"):
        raise ServerAddressError(INVALID_MESSAGE)

    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    url = f"{scheme}://{netloc}{_resolve_path(parts.path or '/')}"
    return url.rstrip("/").removesuffix("/api")
