"""场景批量执行本地化测试（P2）。

覆盖：
- 场景预检形状（cases[] 键含 alias，对齐 scenarios.precheck_scenario_execution）
- run_scenario_local：kind=scenario 上报（同 case 多 alias 分条）+ WS 协议字段
- 变量链：env → case 变量覆盖 → 上一 case 导出变量注入下一 case
- 失败策略 CONTINUE/ABORT
- POST /api/scenarios/{id}/run 批量响应形状 + S1001 预检失败
- WS /api/scenarios/ws/run/{id} 消息协议（log/run_start/run_complete）
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app
from engine.core import run_registry, runner_service, scenario_service


class FakeScenarioBridge:
    """场景链路云端替身：scenario/cases/steps + client-runs 记录。"""

    def __init__(self, case_steps=None, scenario_steps=None, fail_case_ids=None, case_vars=None):
        self.created: List[Dict[str, Any]] = []
        self.step_payloads: List[tuple] = []
        self.finished: List[tuple] = []
        self.fail_case_ids = set(fail_case_ids or [])
        self.case_vars = case_vars or {}
        self._case_steps = case_steps or {
            11: [
                {"order": 1, "action": "click", "args": {},
                 "platform_overrides": {"android": {"selector": "#a", "by": "id"}},
                 "execute_on": ["android"], "timeout": 10, "error_strategy": "ABORT",
                 "description": "点A", "retry_count": 0},
            ],
            22: [
                {"order": 1, "action": "home", "args": {},
                 "execute_on": ["android"], "timeout": 10, "error_strategy": "ABORT",
                 "description": "回桌面", "retry_count": 0},
            ],
        }
        self._scenario_steps = scenario_steps or [
            {"id": 1, "scenario_id": 7, "case_id": 11, "order": 1, "alias": "登录-A"},
            {"id": 2, "scenario_id": 7, "case_id": 11, "order": 2, "alias": "登录-B"},
            {"id": 3, "scenario_id": 7, "case_id": 22, "order": 3, "alias": "收尾"},
        ]

    def fetch_scenario(self, scenario_id):
        return {"id": scenario_id, "name": "巡检场景"}

    def fetch_scenario_steps(self, scenario_id):
        return list(self._scenario_steps)

    def fetch_case(self, case_id):
        if case_id in self.fail_case_ids:
            raise RuntimeError(f"404 用例不存在: {case_id}")
        return {"id": case_id, "name": f"用例{case_id}", "variables": self.case_vars.get(case_id, [])}

    def fetch_case_steps(self, case_id):
        return list(self._case_steps.get(case_id, []))

    def fetch_env_variables(self, env_id):
        return {"ENV_K": "env-v", "SHARED": "from-env"}

    def fetch_app_key_mapping(self):
        return {}

    def ensure_template_local(self, path):
        return f"/cache/{path.rsplit('/', 1)[-1]}"

    def create_run(self, payload):
        self.created.append(payload)
        return {"id": 77, "run_id": payload.get("run_id")}

    def report_steps(self, run_key, payload):
        self.step_payloads.append((run_key, payload))
        return {}

    def finish_run(self, run_key, payload):
        self.finished.append((run_key, payload))
        return {"report_id": "rep-sc"}


class FakeRunner:
    results: Dict[str, Dict[str, Any]] = {}
    export_vars: Dict[str, str] = {}

    def __init__(self, platform=None, device_id=None, abort_event=None, **kwargs):
        self.runtime_variables = {}
        self.abort_event = abort_event

        class _D:
            def screenshot(self):
                return b"\x89PNG-fake"

        self.driver = _D()

    def run_step(self, step):
        action = str(step.get("action") or "")
        preset = dict(FakeRunner.results.get(action) or {})
        preset.setdefault("status", "PASS")
        preset.setdefault("duration", 0.01)
        preset.setdefault("attempts", 1)
        preset.setdefault("step", step)
        preset.setdefault("error_strategy", step.get("error_strategy", "ABORT"))
        if FakeRunner.export_vars:
            self.runtime_variables.update(FakeRunner.export_vars)
            preset.setdefault("runtime_variables", dict(self.runtime_variables))
        return preset

    def disconnect(self):
        pass


@pytest.fixture()
def fake_runner(monkeypatch):
    FakeRunner.results = {}
    FakeRunner.export_vars = {}
    monkeypatch.setattr(runner_service, "CrossPlatformRunner", FakeRunner)
    monkeypatch.setattr(
        runner_service.recording_service, "localize_step_images", lambda step, ensure: step
    )
    return FakeRunner


def _run_scenario(bridge=None, **kwargs):
    bridge = bridge or FakeScenarioBridge()
    messages: List[Dict[str, Any]] = []

    async def emit(message):
        messages.append(message)

    result = asyncio.run(
        scenario_service.run_scenario_local(
            scenario_id=7,
            env_id=3,
            device_serial=kwargs.pop("device_serial", "USB1"),
            run_id="run-sc",
            batch_id="batch-sc",
            emit=emit,
            bridge=bridge,
            **kwargs,
        )
    )
    return result, messages, bridge


# --------------------------- 执行核心 --------------------------- #

def test_scenario_run_reports_kind_scenario_with_alias_keys(fake_runner):
    result, messages, bridge = _run_scenario()

    assert result["success"] is True
    assert result["status"] == "PASS"

    # 单条 kind=scenario 执行记录（scenario_id 透传）
    assert len(bridge.created) == 1
    created = bridge.created[0]
    assert created["kind"] == "scenario"
    assert created["scenario_id"] == 7
    assert created["case_id"] is None
    assert created["run_source"] == "platform"

    # 同一 case 多 alias → 分条上报（平台 _case_state_key 含 alias）
    assert len(bridge.step_payloads) == 3
    aliases = [payload.get("alias") for _, payload in bridge.step_payloads]
    assert aliases == ["登录-A", "登录-B", "收尾"]
    assert bridge.step_payloads[0][1]["case_id"] == 11
    assert bridge.step_payloads[2][1]["case_id"] == 22
    assert all(payload["success"] is True for _, payload in bridge.step_payloads)

    # 一次收尾
    assert len(bridge.finished) == 1
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "PASS"


def test_scenario_ws_message_protocol_fields(fake_runner):
    """WS 协议逐字段对齐 ScenarioEditor.vue 消费面。"""
    result, messages, bridge = _run_scenario()

    logs = [m for m in messages if m.get("type") == "log"]
    for log in logs:
        assert set(log) >= {"type", "level", "message", "timestamp"}
        assert log["level"] in ("info", "success", "warning", "error")

    starts = [m for m in messages if m.get("type") == "run_start"]
    assert len(starts) == 1
    start = starts[0]
    assert start["status"] == "RUNNING"
    assert start["scenario_id"] == 7
    assert start["batch_id"] == "batch-sc"
    assert start["device_serial"] == "USB1"
    assert start["total_steps"] == 3
    assert isinstance(start.get("execution_id"), int)  # 云端 client-runs id

    completes = [m for m in messages if m.get("type") == "run_complete"]
    assert len(completes) == 1
    complete = completes[0]
    assert complete["success"] is True
    assert complete["status"] == "PASS"
    assert complete["report_id"] == "rep-sc"
    assert complete["execution_id"] == 77
    assert complete["summary"].startswith("🏁 执行结束")


def test_scenario_variable_chain_env_case_and_runtime(fake_runner):
    """env 变量 → case 变量覆盖 → 一 case 导出变量注入后续 case。"""
    FakeRunner.export_vars = {"TOK": "abc123"}
    bridge = FakeScenarioBridge(
        case_vars={11: [{"key": "SHARED", "value": "from-case"}], 22: []},
    )
    result, messages, bridge = _run_scenario(bridge=bridge)

    assert result["success"] is True
    # case11：case 变量覆盖 env 同名
    first_exports = bridge.step_payloads[0][1]["exported_variables"]
    assert first_exports["SHARED"] == "from-case"
    assert first_exports["ENV_K"] == "env-v"
    assert first_exports["TOK"] == "abc123"
    # case22（后续 case）：拿到前序 case 的运行时导出
    third_exports = bridge.step_payloads[2][1]["exported_variables"]
    assert third_exports["TOK"] == "abc123"


def test_scenario_abort_strategy_stops_batch(fake_runner):
    """末条失败步骤策略 ABORT → 停止后续 case；CONTINUE → 继续。"""
    FakeRunner.results["click"] = {"status": "FAIL", "error": "点不到", "duration": 0.1}
    result, messages, bridge = _run_scenario()
    assert result["success"] is False
    assert result["status"] == "FAIL"
    assert len(bridge.step_payloads) == 1  # 第一个 case 失败即停（ABORT）
    assert bridge.step_payloads[0][1]["success"] is False
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "FAIL"

    # CONTINUE：失败后继续下一 case
    FakeRunner.results = {}
    bridge2 = FakeScenarioBridge(
        case_steps={
            11: [
                {"order": 1, "action": "click", "args": {},
                 "platform_overrides": {"android": {"selector": "#a", "by": "id"}},
                 "execute_on": ["android"], "timeout": 10, "error_strategy": "CONTINUE",
                 "description": "点A", "retry_count": 0},
            ],
            22: [{"order": 1, "action": "home", "args": {}, "execute_on": ["android"],
                  "timeout": 10, "error_strategy": "ABORT", "description": "回桌面", "retry_count": 0}],
        },
        scenario_steps=[
            {"id": 1, "scenario_id": 7, "case_id": 11, "order": 1, "alias": "登录-A"},
            {"id": 2, "scenario_id": 7, "case_id": 22, "order": 2, "alias": "收尾"},
        ],
    )
    FakeRunner.results["click"] = {"status": "FAIL", "error": "点不到", "duration": 0.1}
    result2, _, bridge2 = _run_scenario(bridge=bridge2)
    assert result2["success"] is False
    assert len(bridge2.step_payloads) == 2  # CONTINUE 后继续上报第二个 case


def test_scenario_missing_case_continues_with_later_cases(fake_runner):
    """S3：缺失用例记失败后 continue（对齐平台 scenario_execution.py），后续用例仍执行。"""
    bridge = FakeScenarioBridge(fail_case_ids={11})  # 步骤 1/2 = case 11 缺失，步骤 3 = case 22
    result, messages, bridge = _run_scenario(bridge=bridge)
    assert result["success"] is False
    # 缺失用例不上报 steps（云端 404），但后续 case 22 必须继续执行并上报
    assert [payload.get("case_id") for _, payload in bridge.step_payloads] == [22]
    assert bridge.step_payloads[0][1]["success"] is True
    assert result["case_statuses"] == ["failed", "failed", "success"]
    warnings = [m for m in messages if m.get("type") == "log" and m.get("level") == "warning"]
    assert any("用例不存在" in m["message"] for m in warnings)
    # 聚合：含失败 case → FAIL，finish 显式 status 透传
    assert result["status"] == "FAIL"
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "FAIL"


def test_scenario_fetch_error_distinct_message_and_continues(fake_runner):
    """S3：取用例瞬时失败（非 404 异常）与「用例不存在」文案区分，同样 continue。"""

    class FlakyBridge(FakeScenarioBridge):
        def fetch_case(self, case_id):
            if case_id == 11:
                raise RuntimeError("503 upstream unavailable")
            return super().fetch_case(case_id)

    bridge = FlakyBridge()
    result, messages, bridge = _run_scenario(bridge=bridge)
    assert result["success"] is False
    assert [payload.get("case_id") for _, payload in bridge.step_payloads] == [22], "后续用例仍执行"
    warnings = [m for m in messages if m.get("type") == "log" and m.get("level") == "warning"]
    assert any("拉取用例失败" in m["message"] and "503" in m["message"] for m in warnings)
    assert not any("用例不存在" in m["message"] for m in warnings)


def test_scenario_all_skipped_finishes_with_warning_status(fake_runner):
    """S2：全跳过场景聚合 WARNING——finish 显式透传（此前被压成 FAIL）。"""
    FakeRunner.results["click"] = {"status": "SKIP", "error": "platform mismatch", "duration": 0.0}
    FakeRunner.results["home"] = {"status": "SKIP", "error": "platform mismatch", "duration": 0.0}
    bridge = FakeScenarioBridge()
    result, messages, bridge = _run_scenario(bridge=bridge)
    assert result["status"] == "WARNING"
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "WARNING"
    completes = [m for m in messages if m.get("type") == "run_complete"]
    assert completes and completes[0]["status"] == "WARNING"


def test_scenario_ignore_only_finishes_with_warning_status(fake_runner):
    """S2：IGNORE-only（步骤失败但 error_strategy=IGNORE）聚合 WARNING。"""
    bridge = FakeScenarioBridge(
        case_steps={
            11: [
                {"order": 1, "action": "click", "args": {},
                 "platform_overrides": {"android": {"selector": "#a", "by": "id"}},
                 "execute_on": ["android"], "timeout": 10, "error_strategy": "IGNORE",
                 "description": "点A", "retry_count": 0},
            ],
            22: [{"order": 1, "action": "home", "args": {}, "execute_on": ["android"],
                  "timeout": 10, "error_strategy": "IGNORE", "description": "回桌面", "retry_count": 0}],
        },
    )
    FakeRunner.results["click"] = {"status": "FAIL", "error": "找不到", "duration": 0.1}
    FakeRunner.results["home"] = {"status": "FAIL", "error": "找不到", "duration": 0.1}
    result, messages, bridge = _run_scenario(bridge=bridge)
    assert result["status"] == "WARNING"
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "WARNING"


def test_scenario_unexpected_exception_finishes_with_error(fake_runner):
    """S5：执行体意外异常 → run_complete{status:ERROR} + finish(status=ERROR)，云端不留 RUNNING。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("runner crashed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scenario_service, "execute_prepared_steps", _boom)
        bridge = FakeScenarioBridge()
        result, messages, bridge = _run_scenario(bridge=bridge)

    assert result["success"] is False and result["status"] == "ERROR"
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "ERROR"
    completes = [m for m in messages if m.get("type") == "run_complete"]
    assert completes and completes[0]["status"] == "ERROR"
    assert "场景执行异常" in completes[0]["summary"]


def test_scenario_preset_fetch_failure_finishes_cloud_record(fake_runner):
    """T2：批量执行（preset_run_key 预创建）+ 拉取场景数据失败 → 云端记录必须被 finish(ERROR)。

    此前失败分支用 context.run_key 判定，而 run_key 在拉取成功后才从 preset_run_key 赋值，
    拉取失败时云端 TestExecution 永远停留 RUNNING。
    """
    bridge = FakeScenarioBridge()

    def _fetch_boom(scenario_id):
        raise RuntimeError("cloud 502")

    bridge.fetch_scenario = _fetch_boom
    result, messages, bridge = _run_scenario(bridge=bridge, preset_run_key=501)

    assert result["success"] is False and result["status"] == "ERROR"
    assert bridge.finished, "预创建的云端执行记录未收尾（停留在 RUNNING）"
    run_key, finish_payload = bridge.finished[0]
    assert run_key == 501
    assert finish_payload["status"] == "ERROR"
    completes = [m for m in messages if m.get("type") == "run_complete"]
    assert completes and completes[0]["status"] == "ERROR"
    assert completes[0]["execution_id"] == 501


def test_scenario_batch_fetch_failure_finishes_preset_record(fake_runner, monkeypatch):
    """T2 批量链路：start_background_scenario_run(preset_run_key) 拉取失败 → 云端 finish。"""
    from engine.core import cloud_bridge

    finished = []
    done = threading.Event()

    def _fetch_boom(self, scenario_id):
        raise RuntimeError("cloud 502")

    def _finish_run(self, run_key, payload):
        finished.append((run_key, payload))
        done.set()
        return {"report_id": None}

    monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_scenario", _fetch_boom)
    monkeypatch.setattr(cloud_bridge.CloudBridge, "finish_run", _finish_run)

    scenario_service.start_background_scenario_run(
        scenario_id=7,
        env_id=3,
        device_serial="USB1",
        run_id="run-t2",
        batch_id="batch-t2",
        auth_headers={"Authorization": "Bearer t"},
        preset_run_key=502,
    )
    assert done.wait(timeout=5), "后台线程未收尾"
    assert finished and finished[0][0] == 502
    assert finished[0][1]["status"] == "ERROR"


def test_scenario_precheck_shape_with_alias(fake_runner):
    bridge = FakeScenarioBridge()
    payload = scenario_service.precheck_scenario_for_device(bridge, 7, "USB1", 3)

    assert payload["scenario_id"] == 7
    assert payload["scenario_name"] == "巡检场景"
    assert payload["ok"] is True
    assert payload["has_runnable_cases"] is True
    assert set(payload["summary"]) == {
        "total_cases", "pass_cases", "warning_cases", "skip_cases", "fail_cases", "all_cases_skipped"
    }
    assert [c["alias"] for c in payload["cases"]] == ["登录-A", "登录-B", "收尾"]
    assert payload["cases"][0]["case_id"] == 11
    assert payload["cases"][0]["status"] == "PASS"


# --------------------------- HTTP/WS 端点 --------------------------- #

@pytest.fixture()
def mock_cloud():
    from test_local_api import _MockCloud

    server = _MockCloud()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(tmp_path, monkeypatch, mock_cloud):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    config_store.reset_for_tests()
    presence.reset_for_tests()
    config_store.set_upstream(mock_cloud.base_url)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    presence.reset_for_tests()
    config_store.reset_for_tests()


def test_scenario_run_batch_endpoint_shape(client, monkeypatch):
    started = []
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {
            "ok": serial != "BAD",
            "cases": [] if serial != "BAD" else [{"alias": "a", "case_name": "c", "status": "FAIL", "reason": "boom"}],
            "has_runnable_cases": serial != "BAD",
        },
    )
    monkeypatch.setattr(
        scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs)
    )

    # 端点用真实 CloudBridge 拉场景存在性/预创建执行记录 → 类级替换
    from engine.core import cloud_bridge

    monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_scenario", lambda self, sid: {"id": sid, "name": "巡检场景"})
    monkeypatch.setattr(cloud_bridge.CloudBridge, "create_run", lambda self, payload: {"id": 101, "run_id": payload["run_id"]})

    resp = client.post(
        "/api/scenarios/7/run",
        json={"env_id": 3, "device_serials": ["USB1", "BAD", None]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "Batch execution started"
    assert data["batch_id"]
    assert data["queued_count"] == 0
    assert data["execution_ids"] == [101]
    assert [r["device_serial"] for r in data["runs"]] == ["USB1"]
    assert data["runs"][0]["execution_id"] == 101
    assert data["runs"][0]["queued"] is False
    blocked = data["blocked_prechecks"]
    assert {b["device_serial"] for b in blocked} == {"BAD", None}
    assert len(started) == 1
    assert started[0]["scenario_id"] == 7
    assert started[0]["preset_run_key"] == 101
    # 本地拦截：不应反代云端 /api/scenarios/7/run


def test_scenario_run_batch_execution_ids_exclude_nulls(client, monkeypatch):
    """S6：云端预创建失败不得 append(None)——execution_ids 只含有效 id（平台 List[int]）。"""
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": True, "cases": [], "has_runnable_cases": True},
    )
    started = []
    monkeypatch.setattr(
        scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs)
    )
    from engine.core import cloud_bridge

    monkeypatch.setattr(
        cloud_bridge.CloudBridge, "fetch_scenario", lambda self, sid: {"id": sid, "name": "巡检场景"}
    )

    def _create(self, payload):
        if payload.get("device_serial") == "USB2":
            raise RuntimeError("cloud down")
        return {"id": 101, "run_id": payload["run_id"]}

    monkeypatch.setattr(cloud_bridge.CloudBridge, "create_run", _create)

    resp = client.post("/api/scenarios/7/run", json={"device_serials": ["USB1", "USB2"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_ids"] == [101]
    assert [r["device_serial"] for r in data["runs"]] == ["USB1", "USB2"]
    assert [r["execution_id"] for r in data["runs"]] == [101, None]
    assert len(started) == 2
    # 未预创建成功的设备仍本地执行（preset_run_key=None → 运行中自建云端记录）
    assert started[1]["preset_run_key"] is None


def test_scenario_run_batch_all_precheck_failed(client, monkeypatch):
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": False, "cases": [], "has_runnable_cases": False},
    )
    from engine.core import cloud_bridge

    monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_scenario", lambda self, sid: {"id": sid, "name": "巡检场景"})

    resp = client.post("/api/scenarios/7/run", json={"device_serials": ["USB1"]})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "S1001_SCENARIO_PRECHECK_FAILED"
    assert detail["items"]


def test_scenario_ws_precheck_failure_blocks_run(client, monkeypatch):
    """S5：WS 进执行体前服务端预检——失败发 log + run_complete{status:ERROR}，不执行。"""
    called = {"run": False}

    async def _fail_run(*args, **kwargs):
        called["run"] = True
        raise AssertionError("预检失败不应进入执行体")

    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {
            "ok": False,
            "cases": [{"alias": "A", "case_name": "c", "status": "FAIL", "reason": "设备不匹配"}],
            "has_runnable_cases": False,
        },
    )
    monkeypatch.setattr(scenario_service, "run_scenario_local", _fail_run)

    with client.websocket_connect("/api/scenarios/ws/run/7?device_serial=USB1") as ws:
        log = ws.receive_json()
        assert log["type"] == "log" and log["level"] == "error"
        assert "运行前预检未通过" in log["message"]
        complete = ws.receive_json()
        assert complete["type"] == "run_complete"
        assert complete["success"] is False
        assert complete["status"] == "ERROR"
        assert complete["summary"].startswith("运行前预检未通过")
    assert called["run"] is False


def test_scenario_ws_endpoint_sends_protocol(client, monkeypatch):
    async def fake_run_via_ws(websocket, scenario_id, env_id, device_serial, auth_headers):
        await websocket.accept()
        await websocket.send_json({"type": "log", "level": "info", "message": "🎬 开始执行场景: x (共 1 个步骤)", "timestamp": "t"})
        await websocket.send_json({"type": "run_start", "status": "RUNNING", "scenario_id": scenario_id,
                                   "execution_id": 77, "batch_id": "b", "device_serial": device_serial,
                                   "total_steps": 1, "timestamp": "t"})
        await websocket.send_json({"type": "run_complete", "success": True, "status": "PASS",
                                   "summary": "🏁 执行结束", "report_id": "rep-1", "execution_id": 77})
        await websocket.close()

    seen = {}

    async def spy(websocket, scenario_id, env_id, device_serial, auth_headers):
        seen["scenario_id"] = scenario_id
        seen["device_serial"] = device_serial
        seen["env_id"] = env_id
        await fake_run_via_ws(websocket, scenario_id, env_id, device_serial, auth_headers)

    monkeypatch.setattr(scenario_service, "run_scenario_via_websocket", spy)
    with client.websocket_connect("/api/scenarios/ws/run/7?env_id=3&device_serial=USB1") as ws:
        log = ws.receive_json()
        assert log["type"] == "log" and log["level"] == "info"
        start = ws.receive_json()
        assert start["type"] == "run_start" and start["execution_id"] == 77
        complete = ws.receive_json()
        assert complete["type"] == "run_complete" and complete["report_id"] == "rep-1"
    assert seen == {"scenario_id": 7, "device_serial": "USB1", "env_id": 3}


def test_scenario_ws_run_registers_and_can_be_cancelled(client, monkeypatch, mock_cloud):
    """场景执行登记 registry（kind=scenario），cancel 同 R4 语义中止。"""
    from engine.core import cloud_bridge

    started = threading.Event()

    class SlowRunner(FakeRunner):
        def run_step(self, step):
            started.set()
            if self.abort_event is not None:
                self.abort_event.wait(timeout=5)
            return super().run_step(step)

    monkeypatch.setattr(runner_service, "CrossPlatformRunner", SlowRunner)
    monkeypatch.setattr(
        runner_service.recording_service, "localize_step_images", lambda step, ensure: step
    )
    monkeypatch.setattr(
        cloud_bridge.CloudBridge,
        "fetch_scenario",
        lambda self, sid: {"id": sid, "name": "巡检场景"},
    )
    monkeypatch.setattr(
        cloud_bridge.CloudBridge,
        "fetch_scenario_steps",
        lambda self, sid: [
            {"id": 1, "scenario_id": sid, "case_id": 5, "order": 1, "alias": "A"},
        ],
    )

    result_holder = {}

    def _thread():
        async def _run():
            async def emit(message):
                result_holder.setdefault("messages", []).append(message)

            bridge = cloud_bridge.CloudBridge()
            result_holder["result"] = await scenario_service.run_scenario_local(
                scenario_id=7, env_id=None, device_serial="USB1", run_id="r-sc", batch_id="b-sc",
                emit=emit, bridge=bridge,
            )

        asyncio.run(_run())

    t = threading.Thread(target=_thread, daemon=True)
    t.start()

    for _ in range(100):
        active = client.get("/api/runs/active", params={"kind": "scenario", "target_id": 7}).json()["items"]
        if active and active[0].get("execution_id") is not None:
            break
        time.sleep(0.05)
    assert active and active[0]["run_id"] == "r-sc"
    # S1：create_cloud_run 回填 execution_id（mock 云端 id=42）——active 必须返回真实 id
    assert active[0]["execution_id"] == 42
    assert started.wait(timeout=5)

    # ScenarioEditor.terminateScenarioRun 真实载荷（batch_id+execution_ids+device_serials）
    cancel = client.post(
        "/api/runs/cancel",
        json={
            "kind": "scenario",
            "target_id": 7,
            "batch_id": "b-sc",
            "execution_ids": [42],
            "device_serials": ["USB1"],
        },
    )
    assert cancel.status_code == 200
    assert cancel.json()["cancelled_count"] == 1
    t.join(timeout=15)
    assert result_holder["result"]["status"] == "ABORTED"
