"""client-runs 载荷契约测试（R2：与平台 schema 两侧锁死）。

镜像 backend/api/client_runs.py 的 ClientRun* 模型（含 ClientRunCreate.started_at/env_id），
断言桌面端真实载荷可被平台 schema 解析且关键字段不被丢弃（pydantic 默认丢弃未知字段——
正是 R2 的字段漂移根因）。

S7：镜像不得手抄漂移——以跨仓库 model_fields 集合 diff 锁定（优先跨包 import
backend.api.client_runs；平台依赖不可导入时退化为 AST 解析平台源码字段集），
且 finish 状态域与平台 _normalize_finish_status 接受集对齐。
"""
from __future__ import annotations

import ast
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest
from pydantic import BaseModel, Field

from engine.core import runner_service
from engine.core.runner_service import LocalRunContext

# 平台仓库根（桌面仓库 ../AutoDroid）——跨包 import / AST 漂移锁共用。
PLATFORM_ROOT = Path(__file__).resolve().parents[2] / "AutoDroid"
PLATFORM_CLIENT_RUNS = PLATFORM_ROOT / "backend" / "api" / "client_runs.py"


# ---- 平台 schema 镜像（backend/api/client_runs.py） ---- #

class ClientRunCreate(BaseModel):
    run_id: str
    kind: str = "case"
    case_id: Optional[int] = None
    scenario_id: Optional[int] = None
    name: Optional[str] = None
    device_serial: Optional[str] = None
    platform: Optional[str] = None
    run_source: Optional[str] = None
    started_at: Optional[datetime] = None
    env_id: Optional[int] = None


class ClientRunStep(BaseModel):
    order: int
    action: Optional[str] = None
    status: str = "FAIL"
    duration: float = 0.0
    attempts: int = 1
    error: Optional[str] = None
    error_code: Optional[str] = None
    screenshot_base64: Optional[str] = None
    output: Optional[Dict[str, Any]] = None


class ClientRunStepsPayload(BaseModel):
    case_id: Optional[int] = None
    name: Optional[str] = None
    case_name: Optional[str] = None  # 平台兼容双写字段（契约字段为 name）
    alias: Optional[str] = None
    success: Optional[bool] = None
    exported_variables: Optional[Dict[str, Any]] = None
    steps: List[ClientRunStep] = Field(default_factory=list)


class ClientRunFinishPayload(BaseModel):
    status: Optional[str] = None
    aborted: bool = False
    duration: Optional[float] = None
    error: Optional[str] = None
    notify: bool = True
    exported_variables: Optional[Dict[str, Any]] = None


_FINISH_STATUS_DOMAIN = {"PASS", "FAIL", "WARNING", "ABORTED", "ERROR"}

# ---- S7：跨仓库漂移锁（import 优先，AST 兜底） ---- #

_PLATFORM_MODELS = {
    "ClientRunCreate": ClientRunCreate,
    "ClientRunStep": ClientRunStep,
    "ClientRunStepsPayload": ClientRunStepsPayload,
    "ClientRunFinishPayload": ClientRunFinishPayload,
}


def _import_platform_models() -> Optional[Dict[str, type]]:
    """跨包 import backend.api.client_runs（sys.path 注入平台仓库根）。失败返回 None。"""
    root = str(PLATFORM_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from backend.api import client_runs  # type: ignore

        return {
            name: getattr(client_runs, name) for name in _PLATFORM_MODELS
        }
    except Exception:
        return None


def _ast_model_fields(class_name: str) -> Set[str]:
    """AST 解析平台源码模型字段名（无需导入平台依赖栈）。"""
    tree = ast.parse(PLATFORM_CLIENT_RUNS.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: Set[str] = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            fields.add(target.id)
            return fields
    raise AssertionError(f"平台源码未找到模型: {class_name}")


def _ast_normalize_status_keys() -> Set[str]:
    """AST 提取平台 _normalize_finish_status 映射表接受的状态键。"""
    tree = ast.parse(PLATFORM_CLIENT_RUNS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_normalize_finish_status":
            keys: Set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for key in sub.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
            return keys
    raise AssertionError("平台源码未找到 _normalize_finish_status")


@pytest.mark.parametrize("model_name", sorted(_PLATFORM_MODELS))
def test_mirror_model_fields_match_platform_contract(model_name):
    """S7：镜像 model_fields 与平台真实模型逐字段相等（漂移即红）。"""
    if not PLATFORM_CLIENT_RUNS.is_file():
        pytest.skip("平台仓库源码不在本机（跳过跨仓库漂移锁）")
    mirror = _PLATFORM_MODELS[model_name]
    platform_models = _import_platform_models()
    if platform_models is not None:
        platform_fields = set(platform_models[model_name].model_fields)
    else:
        platform_fields = _ast_model_fields(model_name)
    assert set(mirror.model_fields) == platform_fields


def test_finish_status_domain_accepted_by_platform():
    """S2 契约：PASS/FAIL/WARNING/ABORTED/ERROR 均可被平台 finish 接受。"""
    if not PLATFORM_CLIENT_RUNS.is_file():
        pytest.skip("平台仓库源码不在本机（跳过跨仓库漂移锁）")
    accepted = _ast_normalize_status_keys()
    assert {"PASS", "FAIL", "WARNING", "ABORTED", "ERROR"} <= accepted


class _CaptureBridge:
    def __init__(self) -> None:
        self.created: List[Dict[str, Any]] = []
        self.step_payloads: List[Dict[str, Any]] = []
        self.finished: List[Dict[str, Any]] = []

    def create_run(self, payload):
        self.created.append(payload)
        return {"id": 77, "run_id": payload.get("run_id")}

    def report_steps(self, run_key, payload):
        self.step_payloads.append(payload)
        return {}

    def finish_run(self, run_key, payload):
        self.finished.append(payload)
        return {"report_id": "rep-1"}


def _context(bridge: _CaptureBridge, **kwargs) -> LocalRunContext:
    async def _noop(_message):
        return None

    return LocalRunContext(
        case_id=kwargs.pop("case_id", 5),
        device_serial="USB1",
        env_id=kwargs.pop("env_id", 3),
        run_id="run-1",
        batch_id="batch-1",
        emit=_noop,
        bridge=bridge,
        **kwargs,
    )


def test_create_payload_parses_against_platform_schema():
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("登录用例", "android")

    parsed = ClientRunCreate.model_validate(bridge.created[0])
    assert parsed.run_id == "run-1"
    assert parsed.kind == "case"
    assert parsed.case_id == 5
    assert parsed.run_source == "platform"
    assert parsed.env_id == 3
    # R2：started_at 必须是 datetime（此前发 epoch float 被云端丢弃）
    assert isinstance(parsed.started_at, datetime)
    assert parsed.name == "登录用例"


def test_create_payload_scenario_kind_has_no_case_id():
    bridge = _CaptureBridge()
    ctx = _context(bridge, case_id=None, kind="scenario", scenario_id=9)
    ctx.create_cloud_run("巡检场景", "android")

    parsed = ClientRunCreate.model_validate(bridge.created[0])
    assert parsed.kind == "scenario"
    assert parsed.scenario_id == 9
    assert parsed.case_id is None


def test_steps_payload_parses_and_name_is_contract_field():
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("登录用例", "android")
    ctx.report_step_results(
        "登录用例",
        True,
        [
            {
                "order": 1,
                "action": "click",
                "report_status": "PASS",
                "duration": 0.2,
                "attempts": 1,
                "error": None,
                "error_code": None,
                "screenshot_base64": None,
                "output": None,
            }
        ],
        {"USER": "u1"},
        alias="登录-A",
    )

    payload = bridge.step_payloads[0]
    parsed = ClientRunStepsPayload.model_validate(payload)
    assert parsed.name == "登录用例"  # 契约字段 name（case_name 双写兼容）
    assert payload.get("case_name") == "登录用例"
    assert parsed.alias == "登录-A"
    assert parsed.success is True
    assert parsed.exported_variables == {"USER": "u1"}
    assert parsed.steps[0].status == "PASS"


def test_empty_steps_batch_requires_explicit_success():
    """R6：空步骤批次不得被 all([])==True 推导为 PASS——success 必须显式。"""
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("空用例", "android")
    ctx.report_step_results("空用例", False, [], {})

    payload = bridge.step_payloads[0]
    assert "success" in payload, "空步骤批次必须带显式 success"
    parsed = ClientRunStepsPayload.model_validate(payload)
    assert parsed.success is False


def test_finish_payload_only_schema_fields_with_status():
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("登录用例", "android")
    ctx.finish(True, False, 1.23, notify=True)

    payload = bridge.finished[0]
    parsed = ClientRunFinishPayload.model_validate(payload)
    assert parsed.status in _FINISH_STATUS_DOMAIN
    # 不再发 success/finished_at/logs（平台 schema 无这些字段，会被丢弃）
    assert set(payload) <= set(ClientRunFinishPayload.model_fields)
    assert "success" not in payload and "finished_at" not in payload and "logs" not in payload
    assert parsed.notify is True
    assert parsed.error is None


def test_finish_failure_and_abort_statuses():
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("登录用例", "android")
    ctx.finish(False, False, 0.5, notify=False, logs=["元素未找到"])
    parsed_fail = ClientRunFinishPayload.model_validate(bridge.finished[0])
    assert parsed_fail.status == "FAIL"
    assert "元素未找到" in (parsed_fail.error or "")

    ctx.finish(False, True, 0.5, notify=False, logs=["执行已被用户终止"])
    parsed_abort = ClientRunFinishPayload.model_validate(bridge.finished[1])
    assert parsed_abort.status == "ABORTED"
    assert parsed_abort.aborted is True


def test_finish_explicit_status_warning_and_error_pass_through():
    """S2：显式 status 直接透传（WARNING 不再被压成 FAIL/PASS，ERROR 可发出）。"""
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("巡检场景", "android")
    ctx.finish(False, False, 1.0, status="WARNING")
    parsed_warning = ClientRunFinishPayload.model_validate(bridge.finished[0])
    assert parsed_warning.status == "WARNING"

    ctx2 = _context(bridge)
    ctx2.create_cloud_run("巡检场景", "android")
    ctx2.finish(False, False, 0.2, logs=["boom"], status="ERROR")
    parsed_error = ClientRunFinishPayload.model_validate(bridge.finished[1])
    assert parsed_error.status == "ERROR"
    assert "boom" in (parsed_error.error or "")


def test_create_run_backfills_registry_execution_id(monkeypatch):
    """S1：create_cloud_run 拿到云端 id 后回填本地 registry（active/terminate 依赖）。"""
    from engine.core import run_registry

    run_registry.registry.reset_for_tests()
    record = run_registry.registry.register(
        run_registry.LocalRunRecord(
            kind="case", target_id=5, run_id="run-1", batch_id="batch-1", device_serial="USB1"
        )
    )
    bridge = _CaptureBridge()
    ctx = _context(bridge)
    ctx.create_cloud_run("登录用例", "android")
    assert record.execution_id == 77
    run_registry.registry.reset_for_tests()


def test_run_case_local_report_chain_parses_end_to_end(fake_runner_module):
    """端到端：run_case_local 的 create/steps/finish 全载荷过平台 schema。"""
    result, messages, bridge = fake_runner_module
    assert result["success"] is True
    ClientRunCreate.model_validate(bridge.created[0])
    steps = ClientRunStepsPayload.model_validate(bridge.step_payloads[0][1])
    finish = ClientRunFinishPayload.model_validate(bridge.finished[0][1])
    assert steps.success is True
    assert finish.status == "PASS"


@pytest.fixture()
def fake_runner_module(monkeypatch):
    import test_runner_service as trs

    FakeBridge, FakeRunner = trs.FakeBridge, trs.FakeRunner

    FakeRunner.results = {}
    FakeRunner.instances = []
    monkeypatch.setattr(runner_service, "CrossPlatformRunner", FakeRunner)
    monkeypatch.setattr(
        runner_service.recording_service, "localize_step_images", lambda step, ensure: step
    )

    bridge = FakeBridge()
    messages: List[Dict[str, Any]] = []

    async def emit(message):
        messages.append(message)

    result = asyncio.run(
        runner_service.run_case_local(
            case_id=5,
            env_id=3,
            device_serial="USB1",
            run_id="run-1",
            batch_id="batch-1",
            emit=emit,
            bridge=bridge,
        )
    )
    return result, messages, bridge
