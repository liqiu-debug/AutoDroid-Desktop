"""兼容性「升级后链路回放」本地化（P4-2 按可行边界）测试。

覆盖：
- 预检本地化：设备安装包快照本机读（adb_runner 注入）+ 冻结链路云端只读组装，
  响应形状对齐 CompatibilityReplayPreflightRead（blockers/warnings/plan_digest/
  device_snapshot_digest/chains/summary/excluded/available_prefixes）
- entry 用例安全筛查（UNSAFE_ENTRY_CASE / ENTRY_CASE_MISSING）
- 冻结路径回放执行内核：chain.first_path → runner_service 批量步骤执行（trace/状态）
- 回放执行 + 上报（client-compatibility）：cell/chain 载荷映射、RUNNING 中间态 +
  终态上报、内核异常兜底（P4-2 收尾）
- local_api 拦截形状：replay-preflight 本地应答；installed_replay 创建本地执行接线；
  其余执行模式原样反代
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app
from engine.core import compatibility_service, runner_service


class FakeCompatBridge:
    """云端只读替身：inspection run / replay-paths / case。"""

    _DEFAULT_CASE = {
        "id": 11,
        "name": "entry",
        "steps": [
            {"action": "click", "selector": "#btn", "description": "进入首页", "timeout": 10},
            {"action": "assert_text", "selector": "#title", "description": "首页可见", "timeout": 10},
        ],
    }

    def __init__(self, *, branch_config=None, chains=None, case="DEFAULT"):
        self.available = True
        self.auth_headers = {"Authorization": "Bearer t"}
        self._branch_config = branch_config if branch_config is not None else {"entry_case_id": 11}
        self._chains = chains if chains is not None else [self._chain(1), self._chain(2)]
        if case == "DEFAULT":
            self._case = dict(self._DEFAULT_CASE)
        else:
            self._case = case  # None = 用例缺失

    @staticmethod
    def _chain(index: int) -> Dict[str, Any]:
        return {
            "chain_id": f"chain-{index}",
            "path_key": f"path-{index}",
            "name": f"链路{index}",
            "endpoint_state_id": 100 + index,
            "evidence_level": "VERIFIED_TWICE",
            "replay_eligibility": "FULL" if index == 1 else "SAFE_PREFIX",
            "first_path": [
                {
                    "action_type": "click",
                    "action_key": f"go-{index}",
                    "locator_candidates": [
                        {"by": "description", "selector": f"btn-{index}"}
                    ],
                }
            ],
            "checkpoints": [{}, {}],
            "terminal_boundaries": [],
        }

    def get_json(self, path: str, params=None):
        if path == "/api/inspections/runs/9":
            return {
                "id": 9,
                "status": "PASS",
                "package_name": "com.x",
                "profile_snapshot": {
                    "branches": {"main": dict(self._branch_config)} if self._branch_config else {},
                    "package_snapshot": {
                        "package_name": "com.x",
                        "version_code": "100",
                        "version_name": "1.0",
                    },
                },
            }
        if path == "/api/inspections/runs/9/replay-paths":
            return {
                "schema_version": 3,
                "run_id": 9,
                "branch_key": "main",
                "total": len(self._chains),
                "page": 1,
                "page_size": 20,
                "summary": {"full_path": 1, "safety_prefix": 1},
                "items": [dict(item) for item in self._chains],
            }
        raise RuntimeError(f"404 unexpected path: {path}")

    def fetch_case(self, case_id):
        if not self._case or int(case_id) != int(self._case.get("id") or 0):
            raise RuntimeError(f"404 用例不存在: {case_id}")
        return dict(self._case)


def _installed(**overrides) -> Dict[str, Any]:
    snapshot = {
        "package_name": "com.x",
        "version_name": "2.0",
        "version_code": "200",
        "first_install_time": "2026-10-01",
        "last_update_time": "2026-10-20",
        "signing_digest": "abc",
        "installed": True,
        "known": True,
        "source": "device",
    }
    snapshot.update(overrides)
    return snapshot


# --------------------------- 预检本地化 --------------------------- #

def test_local_replay_preflight_shape_and_digests():
    bridge = FakeCompatBridge()
    result = compatibility_service.local_replay_preflight(
        bridge,
        inspection_run_id=9,
        branch_key="main",
        device_serial="USB1",
        installed_package=_installed(),
    )
    assert set(result) >= {
        "execution_mode", "inspection_run_id", "branch_key", "package_name",
        "source_package", "installed_package", "blockers", "warnings",
        "plan_digest", "device_snapshot_digest", "plan_version", "summary",
        "chains", "available_prefixes", "excluded",
    }
    assert result["execution_mode"] == "installed_replay"
    assert result["package_name"] == "com.x"
    assert result["blockers"] == []
    assert result["plan_digest"]
    assert result["device_snapshot_digest"] == compatibility_service.package_snapshot_digest(
        _installed()
    )
    # 来源包快照取自巡检冻结快照
    assert result["source_package"]["version_code"] == "100"
    # 版本不同 → 无 SAME_VERSION_REPLAY
    assert [w["code"] for w in result["warnings"]] == []
    chains = result["chains"]
    assert [c["chain_id"] for c in chains] == ["chain-1", "chain-2"]
    # 形状对齐 _replay_preflight_result：scope/eligibility/terminal_outcome 回填
    assert chains[0]["replay_scope"] == "FULL_PATH"
    assert chains[0]["replay_eligibility"] == "FULL"
    assert chains[1]["replay_scope"] == "PREFIX_TO_SAFETY_BOUNDARY"
    assert result["available_prefixes"] == ["chain-2"]


def test_local_replay_preflight_blockers_and_warnings():
    # 设备未安装 → PACKAGE_NOT_INSTALLED
    result = compatibility_service.local_replay_preflight(
        FakeCompatBridge(),
        inspection_run_id=9,
        branch_key="main",
        device_serial="USB1",
        installed_package=_installed(installed=False, package_name="com.x"),
    )
    assert [b["code"] for b in result["blockers"]] == ["PACKAGE_NOT_INSTALLED"]

    # 同版本 → SAME_VERSION_REPLAY 警告
    result = compatibility_service.local_replay_preflight(
        FakeCompatBridge(),
        inspection_run_id=9,
        branch_key="main",
        device_serial="USB1",
        installed_package=_installed(version_code="100"),
    )
    assert [w["code"] for w in result["warnings"]] == ["SAME_VERSION_REPLAY"]

    # branch_config 缺失 → BRANCH_SNAPSHOT_MISSING
    result = compatibility_service.local_replay_preflight(
        FakeCompatBridge(branch_config={}),
        inspection_run_id=9,
        branch_key="main",
        device_serial="USB1",
        installed_package=_installed(),
    )
    assert "BRANCH_SNAPSHOT_MISSING" in [b["code"] for b in result["blockers"]]


def test_local_replay_preflight_entry_case_safety():
    unsafe_case = {
        "id": 11,
        "name": "entry",
        "steps": [{"action": "input", "selector": "#pwd", "description": "输入密码", "timeout": 10}],
    }
    result = compatibility_service.local_replay_preflight(
        FakeCompatBridge(case=unsafe_case),
        inspection_run_id=9,
        branch_key="main",
        device_serial="USB1",
        installed_package=_installed(),
    )
    codes = [b["code"] for b in result["blockers"]]
    assert "UNSAFE_ENTRY_CASE" in codes

    result = compatibility_service.local_replay_preflight(
        FakeCompatBridge(case=None),
        inspection_run_id=9,
        branch_key="main",
        device_serial="USB1",
        installed_package=_installed(),
    )
    assert [b["code"] for b in result["blockers"]] == ["ENTRY_CASE_MISSING"]


def test_read_installed_package_parses_dumpsys():
    """设备快照解析（adb_runner 注入）：pm path + dumpsys → 快照字段 + 摘要。"""
    outputs = {
        "pm path": "package:/data/app/com.x/base.apk\n",
        "dumpsys package": (
            "    versionCode=100 minSdk=24 targetSdk=34\n"
            "    versionName=1.0\n"
            "    firstInstallTime=2026-10-01 00:00:00\n"
            "    lastUpdateTime=2026-10-20 00:00:00\n"
            "    signatures=PackageSignatures{abc}\n"
        ),
    }

    async def _runner(cmd: str, timeout: float) -> str:
        for key, value in outputs.items():
            if key in cmd:
                return value
        return ""

    snapshot = asyncio.run(
        compatibility_service.read_installed_package("USB1", "com.x", adb_runner=_runner)
    )
    assert snapshot["installed"] is True
    assert snapshot["package_name"] == "com.x"
    assert snapshot["version_code"] == "100"
    assert snapshot["version_name"] == "1.0"
    assert snapshot["signing_digest"]
    assert snapshot["snapshot_digest"] == compatibility_service.package_snapshot_digest(snapshot)


# --------------------------- 执行内核（批量步骤执行） --------------------------- #

class _FakeKernelRunner:
    results: Dict[str, Dict[str, Any]] = {}

    def __init__(self, platform=None, device_id=None, abort_event=None, **kwargs):
        self.runtime_variables = {}
        self.abort_event = abort_event

        class _D:
            def screenshot(self):
                return b"\x89PNG-fake"

        self.driver = _D()

    def run_step(self, step):
        action = str(step.get("action") or "")
        preset = dict(_FakeKernelRunner.results.get(action) or {})
        preset.setdefault("status", "PASS")
        preset.setdefault("duration", 0.01)
        preset.setdefault("attempts", 1)
        preset.setdefault("step", step)
        preset.setdefault("error_strategy", step.get("error_strategy", "ABORT"))
        return preset

    def disconnect(self):
        pass


@pytest.fixture()
def fake_kernel_runner(monkeypatch):
    _FakeKernelRunner.results = {}
    monkeypatch.setattr(runner_service, "CrossPlatformRunner", _FakeKernelRunner)
    return _FakeKernelRunner


def _chain_fixture() -> Dict[str, Any]:
    return FakeCompatBridge._chain(1)


def test_run_frozen_chain_locally_pass_trace(fake_kernel_runner):
    chain = _chain_fixture()
    result = compatibility_service.run_frozen_chain_locally(chain, device_serial="USB1")
    assert result["status"] == "PASS"
    assert result["failure_type"] is None
    assert result["checkpoint_count"] == 2
    assert result["completed_checkpoints"] == 1
    assert result["trace"] and result["trace"][0]["action"] == "click"
    assert result["trace"][0]["status"] == "PASS"


def test_run_frozen_chain_locally_fail_and_empty(fake_kernel_runner):
    fake_kernel_runner.results["click"] = {"status": "FAIL", "error": "找不到", "duration": 0.1}
    result = compatibility_service.run_frozen_chain_locally(_chain_fixture(), device_serial="USB1")
    assert result["status"] == "FAIL"
    assert result["failure_type"] == "STEP_FAILED"
    assert result["trace"][0]["status"] == "FAIL"

    empty = {"chain_id": "c", "first_path": [], "checkpoints": []}
    result = compatibility_service.run_frozen_chain_locally(empty, device_serial="USB1")
    assert result["status"] == "FAIL" and result["failure_type"] == "INVALID_REPLAY_PLAN"


def test_chain_steps_to_standard_rows_maps_locators():
    rows = compatibility_service.chain_steps_to_standard_rows(_chain_fixture())
    assert len(rows) == 1
    assert rows[0]["action"] == "click"
    assert rows[0]["args"]["selector"] == "btn-1"
    assert rows[0]["args"]["by"] == "description"


# --------------------------- local_api 拦截形状 --------------------------- #

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


def test_replay_preflight_endpoint_local_answer(client, monkeypatch):
    captured = {}

    def _fake(bridge, **kwargs):
        captured.update(kwargs)
        return {"execution_mode": "installed_replay", "chains": [], "blockers": []}

    monkeypatch.setattr(compatibility_service, "local_replay_preflight", _fake)
    resp = client.post(
        "/api/compatibility/replay-preflight",
        json={
            "inspection_run_id": 9,
            "branch_key": "main",
            "device_serial": "USB1",
            "max_chains": 5,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["execution_mode"] == "installed_replay"
    assert captured["inspection_run_id"] == 9 and captured["device_serial"] == "USB1"


def test_replay_preflight_endpoint_maps_preflight_error(client, monkeypatch):
    def _boom(bridge, **kwargs):
        raise compatibility_service.CompatibilityPreflightError(400, "巡检来源任务尚未完成")

    monkeypatch.setattr(compatibility_service, "local_replay_preflight", _boom)
    resp = client.post(
        "/api/compatibility/replay-preflight",
        json={"inspection_run_id": 9, "branch_key": "main", "device_serial": "USB1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "巡检来源任务尚未完成"


def test_create_run_installed_replay_local_execution(client, mock_cloud, monkeypatch):
    """installed_replay 创建：云端 client-compatibility 建档 + 本地后台执行接线。"""
    from engine.core.cloud_bridge import CloudBridge

    captured = {}

    def _fake_installed(bridge, *, inspection_run_id, device_serial, adb_runner=None):
        captured["installed_call"] = (inspection_run_id, device_serial)
        return _installed()

    def _fake_create(self, payload):
        captured["create_payload"] = dict(payload)
        return {
            "id": 77,
            "name": payload.get("name"),
            "device_serials": list(payload.get("device_serials") or []),
            "page_set_snapshot": [FakeCompatBridge._chain(1)],
            "status": "PENDING",
        }

    def _fake_start(run, *, auth_headers=None, kernel_runner=None):
        captured["started_run"] = run
        captured["auth_headers"] = auth_headers

    monkeypatch.setattr(
        compatibility_service, "build_client_installed_package", _fake_installed
    )
    monkeypatch.setattr(CloudBridge, "create_client_compatibility_run", _fake_create)
    monkeypatch.setattr(
        compatibility_service, "start_background_replay_run", _fake_start
    )

    resp = client.post(
        "/api/compatibility/runs",
        json={
            "execution_mode": "installed_replay",
            "name": "回放",
            "inspection_run_id": 9,
            "replay_branch_key": "main",
            "device_serials": ["USB1"],
            "selected_chain_ids": ["chain-1"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 77
    assert captured["installed_call"] == (9, "USB1")
    assert captured["create_payload"]["installed_package"]["package_name"] == "com.x"
    assert captured["create_payload"]["execution_mode"] == "installed_replay"
    assert captured["started_run"]["id"] == 77
    # 不应把 installed_replay 创建透传到云端 /api/compatibility/runs
    assert not [r for r in mock_cloud.requests if r["path"].endswith("/api/compatibility/runs")]


def test_create_run_installed_replay_requires_device(client, monkeypatch):
    resp = client.post(
        "/api/compatibility/runs",
        json={"execution_mode": "installed_replay", "name": "回放", "device_serials": []},
    )
    assert resp.status_code == 400
    assert "设备" in resp.json()["detail"]


def test_create_run_other_modes_still_proxied(client, mock_cloud):
    """重资产模式（多设备矩阵/对比）保留云端：原样反代。"""
    resp = client.post(
        "/api/compatibility/runs",
        json={"execution_mode": "page_compare", "name": "对比", "device_serials": ["USB1"]},
    )
    # _MockCloud 对未知 POST 返回 404 —— 证明请求确实到达了云端（透传）
    assert resp.status_code == 404
    assert [r for r in mock_cloud.requests if r["path"].endswith("/api/compatibility/runs")]


# --------------------------- 回放执行 + 上报（client-compatibility） --------------------------- #

class _RecordingReportBridge:
    def __init__(self):
        self.calls = []

    def report_client_compatibility_run(self, run_id, payload):
        self.calls.append((int(run_id), payload))
        return {"success": True, "run_id": run_id, "finished": False}


def _fake_run_payload() -> Dict[str, Any]:
    return {
        "id": 5,
        "device_serials": ["USB1"],
        "page_set_snapshot": [FakeCompatBridge._chain(1), FakeCompatBridge._chain(2)],
    }


def test_chain_and_cell_report_mapping():
    chain = FakeCompatBridge._chain(1)
    result = {
        "status": "FAIL",
        "reason": "replay step failed",
        "failure_type": "STEP_FAILED",
        "trace": [
            {"order": 1, "action": "click", "status": "PASS", "duration": 0.1},
            {"order": 2, "action": "click", "status": "FAIL", "duration": 0.2},
        ],
        "completed_checkpoints": 1,
        "duration": 1.5,
    }
    report = compatibility_service.chain_report_from_result(chain, result)
    assert report["chain_id"] == "chain-1"
    assert report["status"] == "FAIL"
    assert report["failure_type"] == "STEP_FAILED"
    assert report["failed_step_index"] == 1
    assert report["completed_checkpoints"] == 1
    assert report["duration_ms"] == 1500.0
    assert len(report["trace"]) == 2

    cell = compatibility_service.cell_report_from_chain_reports(
        "USB1", [report], installed_package=_installed()
    )
    assert cell["status"] == "FAIL"
    assert cell["device_serial"] == "USB1"
    assert cell["installed_package_snapshot"]["package_name"] == "com.x"

    assert (
        compatibility_service.cell_report_from_chain_reports(
            "USB1", [{"status": "PASS"}, {"status": "WARNING"}]
        )["status"]
        == "WARNING"
    )
    assert (
        compatibility_service.cell_report_from_chain_reports("USB1", [{"status": "PASS"}])[
            "status"
        ]
        == "PASS"
    )
    assert compatibility_service.cell_report_from_chain_reports("USB1", [])["status"] == "ERROR"


def test_execute_client_replay_run_reports_intermediate_and_terminal():
    bridge = _RecordingReportBridge()
    seen = []

    def _kernel(chain, *, device_serial):
        seen.append((chain.get("chain_id"), device_serial))
        if chain.get("chain_id") == "chain-1":
            return {
                "status": "PASS",
                "reason": "",
                "failure_type": None,
                "trace": [{"order": 1, "action": "click", "status": "PASS"}],
                "completed_checkpoints": 1,
                "duration": 0.5,
            }
        return {
            "status": "FAIL",
            "reason": "replay step failed",
            "failure_type": "STEP_FAILED",
            "trace": [{"order": 1, "action": "click", "status": "FAIL"}],
            "completed_checkpoints": 0,
            "duration": 0.25,
        }

    summary = compatibility_service.execute_client_replay_run(
        _fake_run_payload(), bridge=bridge, kernel_runner=_kernel
    )
    assert seen == [("chain-1", "USB1"), ("chain-2", "USB1")]
    assert len(bridge.calls) == 2
    run_id, first = bridge.calls[0]
    assert run_id == 5
    assert first["cells"][0]["status"] == "RUNNING"
    assert first["cells"][0]["chains"] == []
    run_id, final = bridge.calls[1]
    cell = final["cells"][0]
    assert cell["status"] == "FAIL"
    assert [c["chain_id"] for c in cell["chains"]] == ["chain-1", "chain-2"]
    assert cell["chains"][0]["status"] == "PASS"
    assert cell["chains"][1]["status"] == "FAIL"
    assert summary.get("run_id") == 5


def test_execute_client_replay_run_kernel_error_fails_chain():
    bridge = _RecordingReportBridge()

    def _boom(chain, *, device_serial):
        raise RuntimeError("adb gone")

    compatibility_service.execute_client_replay_run(
        {"id": 6, "device_serials": ["USB1"], "page_set_snapshot": [FakeCompatBridge._chain(1)]},
        bridge=bridge,
        kernel_runner=_boom,
    )
    _, final = bridge.calls[-1]
    cell = final["cells"][0]
    assert cell["status"] == "FAIL"
    assert cell["chains"][0]["failure_type"] == "EXECUTION_ERROR"
    assert "adb gone" in cell["chains"][0]["reason"]


def test_execute_client_replay_run_requires_id_and_serials():
    with pytest.raises(ValueError):
        compatibility_service.execute_client_replay_run(
            {"device_serials": ["USB1"], "page_set_snapshot": []},
            bridge=_RecordingReportBridge(),
            kernel_runner=lambda chain, *, device_serial: {"status": "PASS"},
        )
    with pytest.raises(ValueError):
        compatibility_service.execute_client_replay_run(
            {"id": 1, "device_serials": [], "page_set_snapshot": []},
            bridge=_RecordingReportBridge(),
            kernel_runner=lambda chain, *, device_serial: {"status": "PASS"},
        )


def test_start_background_replay_run_reports_error_cells(monkeypatch):
    from engine.core.cloud_bridge import CloudBridge

    reported = []

    def _fake_report(self, run_id, payload):
        reported.append((int(run_id), payload))
        return {"success": True}

    def _explode(run, *, auth_headers=None, kernel_runner=None, bridge=None, installed_package=None):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(CloudBridge, "report_client_compatibility_run", _fake_report)
    monkeypatch.setattr(compatibility_service, "execute_client_replay_run", _explode)

    thread = compatibility_service.start_background_replay_run(_fake_run_payload())
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert reported, "失败兜底必须上报 ERROR cell"
    run_id, payload = reported[0]
    assert run_id == 5
    assert payload["cells"][0]["status"] == "ERROR"
    assert "worker crashed" in payload["cells"][0]["error_message"]
