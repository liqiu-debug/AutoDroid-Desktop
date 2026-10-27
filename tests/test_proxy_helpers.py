"""反代/同步辅助函数单元测试。"""
from engine.app import _count_online, _list_devices_payload
from engine.proxy import build_target_url, filter_headers, HOP_BY_HOP
from engine.ws_proxy import ws_target_url


def test_build_target_url_keeps_prefix_and_query():
    assert build_target_url("https://host/pfx", "/api/devices/", "a=1") == "https://host/pfx/api/devices/?a=1"
    assert build_target_url("https://host/", "api/x") == "https://host/api/x"


def test_ws_target_url_schemes():
    assert ws_target_url("https://host/pfx", "/ws/stream", "t=1") == "wss://host/pfx/ws/stream?t=1"
    assert ws_target_url("http://127.0.0.1:8000", "/ws/stream") == "ws://127.0.0.1:8000/ws/stream"


def test_filter_headers_strips_hop_by_hop_and_keeps_auth():
    headers = [
        ("Connection", "keep-alive"),
        ("Authorization", "Bearer t"),
        ("Transfer-Encoding", "chunked"),
        ("X-Custom", "1"),
    ]
    kept = filter_headers(headers, HOP_BY_HOP)
    assert ("Authorization", "Bearer t") in kept
    assert ("X-Custom", "1") in kept
    assert not any(k.lower() in HOP_BY_HOP for k, _ in kept)


def test_count_online_matches_backend_semantics():
    devices = [
        {"status": "IDLE"},
        {"status": "OFFLINE"},
        {"status": "OFFLINE", "app_presence": {"status": "ONLINE"}},
        {"status": "BUSY"},
    ]
    assert _count_online(devices) == 3


def test_list_payload_accepts_array_and_wrapped():
    assert _list_devices_payload([{"id": 1}]) == [{"id": 1}]
    assert _list_devices_payload({"devices": [{"id": 2}], "total": 1}) == [{"id": 2}]
    assert _list_devices_payload({"detail": "err"}) is None
    assert _list_devices_payload("nope") is None
