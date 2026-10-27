"""ServerAddress 归一化语义（对齐 AutoDroid-Android net/ServerAddress.kt）。"""
import pytest

from engine.server_address import INVALID_MESSAGE, ServerAddressError, normalize


def test_basic_host():
    assert normalize("http://192.168.1.10:8000") == "http://192.168.1.10:8000"


def test_trailing_slash_removed():
    assert normalize("http://192.168.1.10:8000/") == "http://192.168.1.10:8000"
    assert normalize("http://192.168.1.10:8000//") == "http://192.168.1.10:8000"


def test_trailing_api_removed():
    assert normalize("http://192.168.1.10:8000/api") == "http://192.168.1.10:8000"
    assert normalize("http://192.168.1.10:8000/api/") == "http://192.168.1.10:8000"


def test_reverse_proxy_prefix_preserved():
    assert normalize("https://host.example.com/autodroid/api/") == "https://host.example.com/autodroid"
    assert normalize("https://host.example.com/autodroid") == "https://host.example.com/autodroid"
    assert normalize("https://host.example.com/autodroid/api/v2") == "https://host.example.com/autodroid/api/v2"


def test_scheme_and_host_lowercased():
    assert normalize("HTTP://Example.COM") == "http://example.com"
    assert normalize("HTTPS://Example.COM:443") == "https://example.com"


def test_default_port_dropped_non_default_kept():
    assert normalize("http://example.com:80") == "http://example.com"
    assert normalize("https://example.com:443") == "https://example.com"
    assert normalize("http://example.com:443") == "http://example.com:443"


def test_dot_segments_resolved():
    assert normalize("http://example.com/a/../api") == "http://example.com"
    assert normalize("http://example.com/./pfx/api") == "http://example.com/pfx"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "example.com",
        "ftp://example.com",
        "http://",
        "http://user:pass@example.com",
        "http://user@example.com",
        "http://example.com?x=1",
        "http://example.com#frag",
        "http://example.com:port",
        "javascript:alert(1)",
    ],
)
def test_invalid_rejected(bad):
    with pytest.raises(ServerAddressError) as exc:
        normalize(bad)
    assert str(exc.value) == INVALID_MESSAGE


@pytest.mark.parametrize(
    "bad",
    [
        # 空 query / 空 fragment 同样拒绝（urlsplit 解析为空串，必须查原文）
        "http://example.com?",
        "http://example.com#",
        "http://example.com/?",
        "http://example.com/#",
        "http://example.com/api?",
        "http://example.com:8000/#",
        # 主机/授权段含反斜杠拒绝（对齐 OkHttp 拒绝集）
        "http://example.com\\evil.com",
        "http://example.com\\evil.com/",
        "https://ho\\st.example.com",
    ],
)
def test_empty_or_marked_query_fragment_and_backslash_rejected(bad):
    with pytest.raises(ServerAddressError) as exc:
        normalize(bad)
    assert str(exc.value) == INVALID_MESSAGE


def test_whitespace_trimmed():
    assert normalize("  http://example.com/  ") == "http://example.com"
