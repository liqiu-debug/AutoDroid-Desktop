"""桌面引擎步骤禁用与从此步继续运行（start_step_index）测试。"""
from __future__ import annotations

import pytest

from engine.core import runner_service
from engine.core.drivers.cross_platform_runner import DriverFactory, TestCaseRunner
from engine.core.execution_service import precheck_steps_for_platform, prepare_steps_for_platform
from engine.core.step_contract import is_step_disabled, legacy_step_to_standard, standard_step_to_legacy


class _DummyDriver:
    def __init__(self) -> None:
        self.calls = []

    def back(self):
        self.calls.append("back")

    def disconnect(self):
        return None


def test_desktop_step_disable_contract_precheck_and_runner(monkeypatch):
    std = legacy_step_to_standard({"action": "back", "disabled": True}, case_id=1, order=1)
    assert is_step_disabled(std) is True
    assert std["args"].get("disabled") is True
    assert standard_step_to_legacy(std).get("disabled") is True

    checks = precheck_steps_for_platform(
        [{"action": "click", "args": {"disabled": True}, "execute_on": ["android"]}, {"action": "back", "args": {}, "execute_on": ["android"]}],
        platform="android",
    )
    assert [c["status"] for c in checks] == ["SKIP", "PASS"]
    assert checks[0]["code"] == "STEP_DISABLED"

    prepared = prepare_steps_for_platform(
        [{"action": "start_app", "args": {"disabled": True}, "execute_on": ["android"]}],
        platform="android",
    )
    assert len(prepared) == 1

    driver = _DummyDriver()
    monkeypatch.setattr(DriverFactory, "create", lambda *args, **kwargs: driver)
    runner = TestCaseRunner(platform="android", device_id="serial-1")
    res = runner.run_step({"action": "back", "args": {"disabled": True}})
    assert res["status"] == "SKIP"
    assert res["error_code"] == "STEP_DISABLED"
    assert driver.calls == []


@pytest.mark.asyncio
async def test_desktop_execute_prepared_steps_respects_start_step_index():
    executed = []

    class _FakeRunner:
        def __init__(self, **kwargs):
            self.runtime_variables = {}

        def run_step(self, step):
            executed.append(step["action"])
            return {"status": "PASS", "error_strategy": "ABORT", "duration": 0.01, "artifacts": {}}

        def disconnect(self):
            return None

    events = []

    async def emit(msg):
        events.append(msg)

    steps = [
        {"order": 1, "action": "back", "args": {}, "error_strategy": "ABORT"},
        {"order": 2, "action": "home", "args": {}, "error_strategy": "ABORT"},
        {"order": 3, "action": "sleep", "args": {"seconds": 1}, "error_strategy": "ABORT"},
    ]
    out = await runner_service.execute_prepared_steps(
        steps,
        platform="android",
        device_serial="serial-1",
        emit=emit,
        runner=_FakeRunner(),
        start_step_index=2,
    )
    assert out["success"] is True
    assert out["passed"] == 1
    assert out["failed"] == 0
    assert executed == ["sleep"]
    assert [e["report_status"] for e in out["step_entries"]] == ["SKIP", "SKIP", "PASS"]
    assert [e["step_index"] for e in events if e["type"] == "step_update" and e["status"] == "skipped"] == [0, 1]

    invalid = await runner_service.execute_prepared_steps(
        steps,
        platform="android",
        device_serial="serial-1",
        emit=emit,
        runner=_FakeRunner(),
        start_step_index=5,
    )
    assert invalid["success"] is False
    assert "超出范围" in invalid["error"]
