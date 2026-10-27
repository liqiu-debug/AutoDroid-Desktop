"""步骤保存乐观锁（P4-1）桌面保存链路契约测试。

桌面端没有本地步骤写入面（engine 只读步骤：runner 拉取 / 录制不写回）——保存链路
= 反向代理透传。已裁决契约（与前端/平台一致，平台 backend/version_lock.py 为准）：
- 请求体是**裸 steps 数组**（List[TestCaseStepWrite]，前端 api.replaceCaseStandardSteps
  直接 put(steps)；对象包装 {"version", "steps"} 对真实平台是 422）；
- 版本令牌不进 body：数值 version 走 Header `If-Match: <token>`（不带引号）与
  Query `version=<int>`，实体有 updated_at 时再带 Query `expected_updated_at=<ISO>`;
- 代理必须原样转发 body 与三件套（剥离/改写会破坏云端乐观校验 → 静默互相覆盖）；
- 409 冲突响应 detail 必须是冻结文案 STEPS_VERSION_CONFLICT_DETAIL
  「保存冲突：内容已被其他端修改，请重新拉取后再保存」，原样回到前端，
  不得被代理剥离/改写；桌面侧收到 409 不重试覆盖。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app

# 409 detail 冻结文案（平台 backend/version_lock.py STEPS_VERSION_CONFLICT_DETAIL 同文，
# 三端对账以此为准；断言用精确等值，防文案漂移）。
STEPS_VERSION_CONFLICT_DETAIL = "保存冲突：内容已被其他端修改，请重新拉取后再保存"


class _VersionMockCloud:
    """云端 steps 乐观锁替身：契约形状对齐真实平台。

    - body 必须是裸步骤数组（List[TestCaseStepWrite]），对象包装等错形状 → 422
      （与 FastAPI 对真实端点的校验先于处理器一致）；
    - 三件套任一令牌不匹配 → 409 + 冻结文案，命中 → 200 + version+1。
    """

    CURRENT_VERSION = 7
    CURRENT_UPDATED_AT = "2026-10-26T10:00:00"

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _read_body(self) -> bytes:
                length = int(self.headers.get("content-length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):  # noqa: N802
                body = self._read_body()
                path, _, query = self.path.partition("?")
                try:
                    parsed = json.loads(body) if body else None
                except ValueError:
                    parsed = None
                owner.requests.append(
                    {
                        "method": "PUT",
                        "path": path,
                        "query": query,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                        "json": parsed,
                    }
                )
                if "/steps" not in path:
                    self._send(404, {"detail": "Not found"})
                    return
                # 真实契约：body 是裸 steps 数组；{"version","steps"} 包装 → 422
                if not isinstance(parsed, list) or not all(
                    isinstance(step, dict) and isinstance(step.get("action"), str)
                    for step in parsed
                ):
                    self._send(422, {"detail": "steps 请求体必须是裸步骤数组"})
                    return
                if_match = (self.headers.get("if-match") or "").strip()
                params = parse_qs(query)
                q_version = (params.get("version") or [None])[0]
                q_updated = (params.get("expected_updated_at") or [None])[0]
                # 契约语义：任一令牌存在即校验（都缺失跳过校验、照常 200 且 version+1，
                # 兼容旧客户端——与 backend/version_lock.py 全缺失行为一致）
                tokens = [t for t in (if_match, q_version, q_updated) if t not in (None, "")]
                expected = str(owner.CURRENT_VERSION)

                def _token_hits(token: str) -> bool:
                    # 数值令牌只对 version（WHERE version），ISO 令牌只对 updated_at
                    if token.isdigit():
                        return token == expected
                    return token == owner.CURRENT_UPDATED_AT

                matched = all(_token_hits(t) for t in tokens) if tokens else True
                if not matched:
                    self._send(
                        409,
                        {
                            "detail": STEPS_VERSION_CONFLICT_DETAIL,
                            "current_version": owner.CURRENT_VERSION,
                        },
                    )
                    return
                self._send(
                    200,
                    {
                        "ok": True,
                        "version": owner.CURRENT_VERSION + 1,
                        "updated_at": "2026-10-26T11:00:00",
                        "path": path,
                    },
                )

            def do_GET(self):  # noqa: N802
                path, _, query = self.path.partition("?")
                owner.requests.append(
                    {
                        "method": "GET",
                        "path": path,
                        "query": query,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                if "/steps" in path:
                    self._send(
                        200,
                        {
                            "id": 5,
                            "version": owner.CURRENT_VERSION,
                            "updated_at": owner.CURRENT_UPDATED_AT,
                            "steps": [],
                        },
                    )
                    return
                self._send(404, {"detail": "Not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def version_cloud():
    server = _VersionMockCloud()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch, version_cloud):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    config_store.reset_for_tests()
    presence.reset_for_tests()
    config_store.set_upstream(version_cloud.base_url)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    presence.reset_for_tests()
    config_store.reset_for_tests()


# 真实平台 body：裸 steps 数组（前端 replaceCaseStandardSteps(id, steps) 直接 put(steps)）。
_STEPS_BODY: List[Dict[str, Any]] = [{"order": 1, "action": "click", "args": {}}]


def _put_steps(
    client: TestClient,
    path: str,
    *,
    if_match: str = "",
    version: Any = None,
    expected_updated_at: str = "",
    body: Any = None,
):
    query: List[str] = []
    if version is not None:
        query.append(f"version={version}")
    if expected_updated_at:
        query.append(f"expected_updated_at={expected_updated_at}")
    url = path + ("?" + "&".join(query) if query else "")
    return client.put(
        url,
        json=_STEPS_BODY if body is None else body,
        headers={"If-Match": if_match} if if_match else {},
    )


def _assert_bare_steps_body(upstream: Dict[str, Any]) -> None:
    """云端收到的 body 必须是裸步骤数组（对象包装对真实平台 422）。"""
    assert isinstance(upstream["json"], list)
    assert upstream["json"] == _STEPS_BODY


# --------------------------- 用例步骤 --------------------------- #

def test_put_case_steps_forwards_unquoted_if_match_and_query_tokens(client, version_cloud):
    """契约：裸 steps 数组 body + If-Match 不带引号与 ?version= 同达云端 + expected_updated_at。"""
    resp = _put_steps(
        client,
        "/api/cases/5/steps",
        if_match="7",
        version=7,
        expected_updated_at="2026-10-26T10:00:00",
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 8
    upstream = [r for r in version_cloud.requests if r["method"] == "PUT"]
    assert len(upstream) == 1
    # body 是裸步骤数组（version 令牌不进 body）
    _assert_bare_steps_body(upstream[0])
    # If-Match 头与 ?version= 查询串同达云端（不带引号；带引号会导致云端字符串比对失配 → 误判 409）
    assert upstream[0]["headers"].get("if-match") == "7"
    assert "version=7" in upstream[0]["query"]
    assert "expected_updated_at=2026-10-26T10:00:00" in upstream[0]["query"]


def test_put_case_steps_wrapped_body_shape_rejected_by_contract(client, version_cloud):
    """对象包装 {"version","steps"} 是错形状：真实平台 422，契约替身同口径（代理不改写 body）。"""
    resp = _put_steps(
        client,
        "/api/cases/5/steps",
        if_match="7",
        version=7,
        body={"version": 7, "steps": _STEPS_BODY},
    )
    assert resp.status_code == 422
    upstream = [r for r in version_cloud.requests if r["method"] == "PUT"]
    assert len(upstream) == 1
    # 代理原样透传错形状（不替调用方改写 body），云端按契约 422
    assert upstream[0]["json"] == {"version": 7, "steps": _STEPS_BODY}


def test_put_case_steps_conflict_409_passes_through_without_retry(client, version_cloud):
    """409 原样上抛（detail=冻结文案）；代理不得重试/改写——仅 1 次 PUT 到达云端。"""
    resp = _put_steps(
        client,
        "/api/cases/5/steps",
        if_match="3",
        version=3,
        expected_updated_at="2026-10-26T09:00:00",
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == STEPS_VERSION_CONFLICT_DETAIL
    assert resp.json()["current_version"] == 7
    upstream = [r for r in version_cloud.requests if r["method"] == "PUT"]
    assert len(upstream) == 1


def test_put_case_steps_updated_at_only_token_passes_through(client, version_cloud):
    """updated_at 回退令牌（无数值 version）：If-Match 原样 ISO，不被代理编码/加引号。"""
    resp = _put_steps(
        client,
        "/api/cases/5/steps",
        if_match="2026-10-26T10:00:00",
        expected_updated_at="2026-10-26T10:00:00",
    )
    assert resp.status_code == 200
    upstream = [r for r in version_cloud.requests if r["method"] == "PUT"]
    _assert_bare_steps_body(upstream[0])
    assert upstream[0]["headers"].get("if-match") == "2026-10-26T10:00:00"
    assert "version=" not in upstream[0]["query"]
    assert "expected_updated_at=2026-10-26T10:00:00" in upstream[0]["query"]


def test_put_case_steps_without_tokens_skips_lock_and_bumps_version(client, version_cloud):
    """三件套令牌全缺失：真实平台跳过校验（旧客户端兼容），照常 200 且 version+1。"""
    resp = _put_steps(client, "/api/cases/5/steps")
    assert resp.status_code == 200
    assert resp.json()["version"] == 8  # 7 + 1：无令牌也递增版本
    upstream = [r for r in version_cloud.requests if r["method"] == "PUT"]
    _assert_bare_steps_body(upstream[0])
    assert not upstream[0]["headers"].get("if-match")
    assert "version=" not in upstream[0]["query"]


def test_get_case_steps_version_field_passes_through(client, version_cloud):
    resp = client.get("/api/cases/5/steps")
    assert resp.status_code == 200
    assert resp.json()["version"] == 7
    assert resp.json()["updated_at"]


# --------------------------- 场景步骤 --------------------------- #

def test_put_scenario_steps_forwards_tokens(client, version_cloud):
    resp = _put_steps(
        client,
        "/api/scenarios/7/steps",
        if_match="7",
        version=7,
        expected_updated_at="2026-10-26T10:00:00",
    )
    assert resp.status_code == 200
    upstream = [r for r in version_cloud.requests if r["method"] == "PUT"]
    assert upstream[0]["path"] == "/api/scenarios/7/steps"
    _assert_bare_steps_body(upstream[0])
    # If-Match 头与 ?version= 查询串同达云端
    assert upstream[0]["headers"].get("if-match") == "7"
    assert "version=7" in upstream[0]["query"]
    assert "expected_updated_at=2026-10-26T10:00:00" in upstream[0]["query"]


def test_put_scenario_steps_conflict_409_passes_through(client, version_cloud):
    resp = _put_steps(
        client,
        "/api/scenarios/7/steps",
        if_match="1",
        version=1,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == STEPS_VERSION_CONFLICT_DETAIL
