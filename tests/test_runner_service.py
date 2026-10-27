"""本地执行 + client-runs 上报链路测试（mock 云端桥 + mock 驱动）。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from engine.core import runner_service


class FakeBridge:
    """CloudBridge 替身：记录 create/report/finish 载荷。"""

    def __init__(self, steps=None, fail_create=False):
        self.created: List[Dict[str, Any]] = []
        self.step_payloads: List[tuple] = []
        self.finished: List[tuple] = []
        self.fail_create = fail_create
        self._steps = steps or [
            {"order": 1, "action": "click", "args": {},
             "platform_overrides": {"android": {"selector": "确定", "by": "text"}},
             "description": "点确定", "timeout": 10, "error_strategy": "ABORT",
             "execute_on": ["android"]},
        ]

    def fetch_case(self, case_id):
        return {"id": case_id, "name": "登录用例", "variables": [{"key": "USER", "value": "u1"}]}

    def fetch_case_steps(self, case_id):
        return list(self._steps)

    def fetch_env_variables(self, env_id):
        return {"ENV_K": "v"}

    def fetch_app_key_mapping(self):
        return {}

    def ensure_template_local(self, path):
        return f"/cache/{path.rsplit('/', 1)[-1]}"

    def create_run(self, payload):
        if self.fail_create:
            raise RuntimeError("cloud down")
        self.created.append(payload)
        return {"id": 77, "run_id": payload.get("run_id")}

    def report_steps(self, run_key, payload):
        self.step_payloads.append((run_key, payload))
        return {}

    def finish_run(self, run_key, payload):
        self.finished.append((run_key, payload))
        return {"report_id": "rep-1"}


class FakeDriver:
    def screenshot(self):
        return b"\x89PNG-fake"


class FakeRunner:
    results: Dict[str, Dict[str, Any]] = {}
    instances: List["FakeRunner"] = []

    def __init__(self, platform=None, device_id=None, abort_event=None, **kwargs):
        self.driver = FakeDriver()
        self.runtime_variables = {}
        self.run_steps: List[Dict[str, Any]] = []
        FakeRunner.instances.append(self)

    def run_step(self, step):
        self.run_steps.append(step)
        action = str(step.get("action") or "")
        # 模拟 cross_platform_runner 的平台拦截（execute_on 不含当前平台 → SKIP）
        execute_on = [str(x).lower() for x in (step.get("execute_on") or ["android"])]
        if "android" not in execute_on:
            return {
                "status": "SKIP",
                "duration": 0.0,
                "attempts": 1,
                "step": step,
                "error": "platform mismatch",
                "error_strategy": step.get("error_strategy", "ABORT"),
            }
        preset = FakeRunner.results.get(action)
        if preset:
            result = dict(preset)
        else:
            result = {
                "status": "PASS",
                "duration": 0.01,
                "attempts": 1,
                "step": step,
                "output": {"echo": action},
            }
        # 真实 Runner 的结果会带 error_strategy（ws_run 依赖它判断 ABORT/IGNORE）
        result.setdefault("error_strategy", step.get("error_strategy", "ABORT"))
        return result

    def disconnect(self):
        pass


@pytest.fixture()
def fake_runner(monkeypatch):
    FakeRunner.results = {}
    FakeRunner.instances = []
    monkeypatch.setattr(runner_service, "CrossPlatformRunner", FakeRunner)
    monkeypatch.setattr(runner_service.recording_service, "localize_step_images", lambda step, ensure: step)
    return FakeRunner


def _run(case_id=5, env_id=3, device_serial="USB1", **kwargs):
    bridge = kwargs.pop("bridge", None) or FakeBridge()
    messages: List[Dict[str, Any]] = []

    async def emit(message):
        messages.append(message)

    result = asyncio.run(
        runner_service.run_case_local(
            case_id=case_id,
            env_id=env_id,
            device_serial=device_serial,
            emit=emit,
            bridge=bridge,
            **kwargs,
        )
    )
    return result, messages, bridge


def test_run_case_local_success_reports_and_emits(fake_runner):
    result, messages, bridge = _run()

    assert result["success"] is True
    assert result["report_id"] == "rep-1"
    assert result["passed"] == 1 and result["failed"] == 0

    types = [m["type"] for m in messages]
    assert types[0] == "run_start"
    assert types[-1] == "run_complete"
    assert "step_update" in types

    start = messages[0]
    assert start["case_name"] == "登录用例"
    assert start["total_steps"] == 1
    assert start["device_serial"] == "USB1"

    complete = messages[-1]
    assert complete["success"] is True
    assert complete["status"] == "PASS"
    assert complete["report_id"] == "rep-1"

    # 上报链路：create（幂等键 run_id）→ steps → finish
    assert len(bridge.created) == 1
    created = bridge.created[0]
    assert created["run_id"] == result["run_id"]
    assert created["kind"] == "case"
    assert created["case_id"] == 5
    assert created["device_serial"] == "USB1"
    assert created["platform"] == "android"
    assert created["run_source"] == "platform"
    # R2：create 载荷字段对齐 ClientRunCreate（started_at=ISO datetime，env_id 透传）
    assert created["env_id"] == 3
    assert isinstance(created["started_at"], str) and "T" in created["started_at"]

    key, steps_payload = bridge.step_payloads[0]
    assert key == 77
    assert steps_payload["name"] == "登录用例"
    assert steps_payload["case_name"] == "登录用例"
    assert steps_payload["success"] is True
    assert steps_payload["steps"][0]["status"] == "PASS"
    assert steps_payload["steps"][0]["action"] == "click"

    key, finish_payload = bridge.finished[0]
    assert key == 77
    # R2：finish 只发平台契约字段（status/error/exported_variables…），不再发 success/logs
    assert finish_payload["status"] == "PASS"
    assert finish_payload["error"] is None
    # 手动执行默认不发通知（与 Web 对齐：仅定时任务勾选后才通知）
    assert finish_payload["notify"] is False
    assert finish_payload["aborted"] is False
    assert set(finish_payload) <= {"status", "aborted", "duration", "error", "notify", "exported_variables"}


def test_run_case_local_notify_only_when_explicitly_enabled(fake_runner):
    """通知策略与 Web 对齐：手动执行默认不发；仅显式开启（定时任务勾选）才发。"""
    _, _, bridge_manual = _run()
    assert bridge_manual.finished[0][1]["notify"] is False

    _, _, bridge_sched = _run(notify=True)
    assert bridge_sched.finished[0][1]["notify"] is True


def test_run_case_local_failure_abort_reports_fail(fake_runner):
    FakeRunner.results["click"] = {
        "status": "FAIL",
        "duration": 0.5,
        "attempts": 2,
        "error": "元素未找到",
        "error_code": "P2002_ELEMENT_NOT_FOUND",
        "suggestion": "检查选择器",
        "step": {"action": "click"},
    }
    result, messages, bridge = _run()

    assert result["success"] is False
    assert result["status"] == "FAIL"
    complete = messages[-1]
    assert complete["type"] == "run_complete"
    assert complete["success"] is False

    failed_updates = [m for m in messages if m.get("status") == "failed"]
    assert failed_updates and failed_updates[0]["error"] == "元素未找到"

    _, steps_payload = bridge.step_payloads[0]
    assert steps_payload["success"] is False
    assert steps_payload["steps"][0]["status"] == "FAIL"
    assert steps_payload["steps"][0]["attempts"] == 2
    assert steps_payload["steps"][0]["error_code"] == "P2002_ELEMENT_NOT_FOUND"

    # 失败收尾必须显式 status=FAIL（不能靠步骤推导）
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "FAIL"
    assert "元素未找到" in (finish_payload["error"] or "")


def test_run_case_local_ignore_strategy_warns_not_fails(fake_runner):
    FakeBridgeSteps = [
        {"order": 1, "action": "click", "args": {"selector": "#x"}, "error_strategy": "IGNORE",
         "execute_on": ["android"]},
        {"order": 2, "action": "home", "args": {}, "error_strategy": "ABORT", "execute_on": ["android"]},
    ]
    FakeRunner.results["click"] = {"status": "FAIL", "duration": 0.1, "attempts": 1,
                                   "error": "找不到", "step": {"action": "click"}}
    bridge = FakeBridge(steps=FakeBridgeSteps)
    result, messages, _ = _run(bridge=bridge)

    assert result["success"] is True  # IGNORE 失败不计入 failed
    _, steps_payload = bridge.step_payloads[0]
    statuses = [s["status"] for s in steps_payload["steps"]]
    assert statuses == ["WARNING", "PASS"]
    # S2：IGNORE-only 聚合 WARNING——finish 显式透传（不得压成 PASS/FAIL）
    assert result["status"] == "WARNING"
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "WARNING"


def test_run_case_local_all_skipped_finishes_warning(fake_runner):
    """S2：全跳过用例（平台不匹配 SKIP）云端 status==WARNING。"""
    bridge = FakeBridge(
        steps=[
            {"order": 1, "action": "click", "args": {"selector": "#a"}, "execute_on": ["ios"]},
            {"order": 2, "action": "home", "args": {}, "execute_on": ["ios"]},
        ]
    )
    result, messages, _ = _run(bridge=bridge)
    assert result["success"] is True
    assert result["status"] == "WARNING"
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "WARNING"
    complete = messages[-1]
    assert complete["type"] == "run_complete" and complete["status"] == "WARNING"


def test_run_case_local_unexpected_exception_reports_error(fake_runner):
    """S5：执行体意外异常 → run_complete{status:ERROR} + finish(status=ERROR)。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("runner crashed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_service, "execute_prepared_steps", _boom)
        bridge = FakeBridge()
        result, messages, _ = _run(bridge=bridge)

    assert result["success"] is False and result["status"] == "ERROR"
    assert "runner crashed" in result["error"]
    _, finish_payload = bridge.finished[0]
    assert finish_payload["status"] == "ERROR"
    complete = messages[-1]
    assert complete["type"] == "run_complete" and complete["status"] == "ERROR"


def test_run_case_local_skips_platform_mismatch(fake_runner):
    bridge = FakeBridge(
        steps=[
            {"order": 1, "action": "click", "args": {"selector": "#a"}, "execute_on": ["ios"]},
            {"order": 2, "action": "home", "args": {}, "execute_on": ["android"]},
        ]
    )
    result, messages, _ = _run(bridge=bridge)
    assert result["success"] is True
    skip_updates = [m for m in messages if m.get("status") == "skipped"]
    assert skip_updates
    _, steps_payload = bridge.step_payloads[0]
    assert [s["status"] for s in steps_payload["steps"]] == ["SKIP", "PASS"]


def test_run_case_local_continues_when_cloud_report_fails(fake_runner):
    """上报失败不阻断本地执行与消息推送。"""
    bridge = FakeBridge(fail_create=True)
    result, messages, _ = _run(bridge=bridge)
    assert result["success"] is True
    assert result["report_id"] is None
    assert messages[-1]["type"] == "run_complete"


def test_run_case_local_pull_failure_emits_error(fake_runner):
    class BrokenBridge(FakeBridge):
        def fetch_case(self, case_id):
            raise RuntimeError("网络断开")

    result, messages, _ = _run(bridge=BrokenBridge())
    assert result["success"] is False
    assert messages and messages[0]["type"] == "error"
    assert "拉取用例数据失败" in messages[0]["message"]


def test_precheck_for_device_flags_ios_warn_only(fake_runner):
    """R4：iOS 预检不因 WDA/能力探测失败而阻断——仅告警（WARNING），ok 不受影响。"""
    bridge = FakeBridge(
        steps=[
            {"order": 1, "action": "home", "args": {}, "execute_on": ["ios"],
             "description": "回桌面", "timeout": 10, "error_strategy": "ABORT", "retry_count": 0},
        ]
    )
    result = runner_service.precheck_for_device(bridge, 5, "ios:udid", None)
    assert result["platform"] == "ios"
    ios_checks = [c for c in result["global_checks"] if c["code"] in ("IOS_EXECUTION_PENDING", "IOS_WDA_PROBE_SKIPPED")]
    assert ios_checks and all(c["status"] == "WARNING" for c in ios_checks)
    assert result["has_runnable_steps"] is True
    assert result["ok"] is True


def test_precheck_for_device_ok(fake_runner):
    bridge = FakeBridge()
    result = runner_service.precheck_for_device(bridge, 5, "USB1", None)
    assert result["ok"] is True
    assert result["platform"] == "android"


def test_broker_publish_sync_is_noop_without_loop():
    broker = runner_service.RunBroker()
    broker.publish_sync(1, {"type": "log"})  # 不应抛异常
