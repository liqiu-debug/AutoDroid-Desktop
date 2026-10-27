"""定时任务本地触发（P3-1）测试。

覆盖：
- 触发器构造对齐 scheduler_service.create_trigger（DAILY/WEEKLY/INTERVAL/ONCE）
- 本地调度过滤（仅 ui + scenario_id + is_active；ONCE 过期不排）
- sync_tasks 增/改/删 job（重启重排语义）
- 触发执行：precheck 过滤 + start_background_scenario_run 本地链路 + run_name
- ONCE 任务端到端触发（APScheduler 真调度）
- refresh_now 拉取 /api/client-tasks 契约（{total, items}）
- 登录响应触发 refresh_async（app 接线）
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from engine import config_store, presence
from engine.core import cloud_bridge, scenario_service, task_scheduler
from engine.core.task_scheduler import (
    LocalTaskScheduler,
    build_trigger,
    device_serials_of,
    should_schedule_locally,
    task_type_of,
)


def _task(**overrides) -> Dict[str, Any]:
    task = {
        "id": 1,
        "name": "每日巡检",
        "scenario_id": 7,
        "device_serial": "USB1,USB2",
        "device_serials": ["USB1", "USB2"],
        "strategy": "DAILY",
        "strategy_config": {"hour": 8, "minute": 30, "_task_type": "ui", "env_id": 3},
        "is_active": True,
        "enable_notification": True,
        "next_run_time": None,
        "user_id": 9,
        "created_at": "2026-10-26T00:00:00",
        "updated_at": "2026-10-26T00:00:00",
    }
    task.update(overrides)
    return task


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
    from fastapi.testclient import TestClient

    from engine.app import create_app

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


# --------------------------- 触发器构造 --------------------------- #

def test_build_trigger_daily_and_weekly_match_platform():
    daily = build_trigger("DAILY", {"hour": 8, "minute": 30})
    assert isinstance(daily, CronTrigger)
    daily_fields = {f.name: f for f in daily.fields}
    assert str(daily_fields["hour"]) == "8"
    assert str(daily_fields["minute"]) == "30"

    weekly = build_trigger("WEEKLY", {"days": [0, 2, 4], "hour": 9, "minute": 0})
    assert isinstance(weekly, CronTrigger)
    weekly_fields = {f.name: f for f in weekly.fields}
    assert str(weekly_fields["day_of_week"]) == "mon,wed,fri"
    assert str(weekly_fields["hour"]) == "9"


def test_build_trigger_interval_and_once():
    every_min = build_trigger("INTERVAL", {"interval_value": 15, "interval_unit": "minutes"})
    assert isinstance(every_min, IntervalTrigger)
    assert every_min.interval.total_seconds() == 15 * 60

    every_hour = build_trigger("INTERVAL", {"interval_value": 2, "interval_unit": "hours"})
    assert every_hour.interval.total_seconds() == 2 * 3600

    once = build_trigger("ONCE", {"run_date": "2030-01-02T03:04:05"})
    assert isinstance(once, DateTrigger)

    with pytest.raises(ValueError):
        build_trigger("ONCE", {})
    with pytest.raises(ValueError):
        build_trigger("YEARLY", {})


# --------------------------- 过滤 / 归一 --------------------------- #

def test_should_schedule_locally_filters_non_local_task_types():
    ok, _ = should_schedule_locally(_task())
    assert ok is True

    ok, reason = should_schedule_locally(_task(strategy_config={"_task_type": "fastbot"}))
    assert ok is False and "fastbot" in reason

    ok, reason = should_schedule_locally(_task(scenario_id=None))
    assert ok is False

    ok, reason = should_schedule_locally(_task(is_active=False))
    assert ok is False

    ok, reason = should_schedule_locally(_task(strategy="YEARLY"))
    assert ok is False


def test_device_serials_prefers_list_then_csv():
    assert device_serials_of(_task()) == ["USB1", "USB2"]
    assert device_serials_of(_task(device_serials=[], device_serial="A, B ,")) == ["A", "B"]
    assert task_type_of(_task()) == "ui"


# --------------------------- sync_tasks --------------------------- #

def test_sync_tasks_adds_updates_removes_jobs():
    fired: List[Any] = []
    sched = LocalTaskScheduler(runner=lambda task: fired.append(task))
    sched.start(refresh_seconds=3600)
    try:
        result = sched.sync_tasks([_task()])
        assert result["added"] == 1 and result["scheduled"] == 1
        assert sched.scheduled_task_ids() == [1]

        # 同定义重排：幂等（不重建）
        again = sched.sync_tasks([_task()])
        assert again["added"] == 0 and again["updated"] == 0

        # 定义变化：重建 job（updated）
        changed = sched.sync_tasks([_task(strategy_config={"hour": 9, "minute": 0, "_task_type": "ui"})])
        assert changed["updated"] == 1

        # 非 ui / 未启用 / 过期 ONCE 不排
        past_once = datetime.now() - timedelta(minutes=1)
        skipped = sched.sync_tasks(
            [
                _task(id=2, strategy="ONCE", strategy_config={"run_date": past_once.isoformat()}),
                _task(id=3, strategy_config={"_task_type": "api"}),
                _task(id=4, is_active=False),
            ]
        )
        assert skipped["removed"] == 1  # 任务 1 不在新集合中被移除
        assert sched.scheduled_task_ids() == []
    finally:
        sched.shutdown()


def test_once_job_fires_end_to_end():
    """ONCE 任务经本地 APScheduler 真实触发（引擎存活时才触发的文档口径）。"""
    fired: List[Any] = []
    sched = LocalTaskScheduler(runner=lambda task: fired.append(task.get("id")))
    sched.start(refresh_seconds=3600)
    try:
        run_date = datetime.now() + timedelta(seconds=0.3)
        sched.sync_tasks(
            [_task(id=5, strategy="ONCE", strategy_config={"run_date": run_date.isoformat(), "_task_type": "ui"})]
        )
        deadline = time.time() + 8
        while not fired and time.time() < deadline:
            time.sleep(0.05)
        assert fired == [5]
    finally:
        sched.shutdown()


# --------------------------- 触发执行 --------------------------- #

def test_execute_task_precheck_filters_and_runs_with_task_name(monkeypatch):
    started = []
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: {"USB1", "BAD"})
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": serial != "BAD", "cases": []},
    )
    monkeypatch.setattr(
        scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs)
    )

    result = task_scheduler.execute_task(
        _task(device_serials=["USB1", "BAD"]), {"Authorization": "Bearer t"}
    )
    assert [item["device_serial"] for item in result] == ["USB1"]
    assert len(started) == 1
    assert started[0]["scenario_id"] == 7
    assert started[0]["env_id"] == 3
    assert started[0]["device_serial"] == "USB1"
    assert started[0]["notify"] is True
    # 云端执行记录展示名带定时任务前缀（对齐平台 batch_name 口径）
    assert started[0]["run_name"] == "定时任务: 每日巡检"
    assert result[0]["run_id"] == started[0]["run_id"]


def test_execute_task_skips_missing_target(monkeypatch):
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: None)
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda *args, **kwargs: pytest.fail("不应预检"),
    )
    assert task_scheduler.execute_task(_task(scenario_id=None), {}) == []
    assert task_scheduler.execute_task(_task(device_serials=[], device_serial=""), {}) == []
    assert task_scheduler.execute_task(_task(strategy_config={"_task_type": "fastbot"}), {}) == []


def test_execute_task_skips_devices_not_on_this_machine(monkeypatch):
    """T4：触发前按本机 adb 清单过滤——不在本机的 serial 跳过（不建 run、不预检）。"""
    started = []
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: {"USB1"})
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": True, "cases": []},
    )
    monkeypatch.setattr(
        scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs)
    )

    result = task_scheduler.execute_task(
        _task(device_serials=["USB1", "USB-OTHER-DESKTOP"]), {"Authorization": "Bearer t"}
    )
    assert [item["device_serial"] for item in result] == ["USB1"]
    assert [item["device_serial"] for item in started] == ["USB1"]


def test_execute_task_fail_open_when_local_list_unknown(monkeypatch):
    """T4：本机清单未知（adb 缺失/失败 → None）时不过滤，保持现状语义。"""
    started = []
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: None)
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": True, "cases": []},
    )
    monkeypatch.setattr(
        scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs)
    )
    result = task_scheduler.execute_task(
        _task(device_serials=["USB1", "USB2"]), {"Authorization": "Bearer t"}
    )
    assert [item["device_serial"] for item in result] == ["USB1", "USB2"]


def test_execute_task_ios_serial_not_filtered_by_adb_list(monkeypatch):
    """T4：ios: 前缀不在 adb 清单（iOS 本机枚举后置）——不据此过滤。"""
    started = []
    monkeypatch.setattr(task_scheduler, "local_device_serials", lambda: {"USB1"})
    monkeypatch.setattr(
        scenario_service,
        "precheck_scenario_for_device",
        lambda bridge, scenario_id, serial, env_id=None: {"ok": True, "cases": []},
    )
    monkeypatch.setattr(
        scenario_service, "start_background_scenario_run", lambda **kwargs: started.append(kwargs)
    )
    result = task_scheduler.execute_task(
        _task(device_serials=["ios:00008101", "USB-OTHER"]), {"Authorization": "Bearer t"}
    )
    assert [item["device_serial"] for item in result] == ["ios:00008101"]


# --------------------------- T3：时区容错 / 整批隔离 --------------------------- #

def test_once_is_past_handles_aware_run_date_and_bad_values():
    """T3：带时区 run_date 不抛 TypeError；坏值视为未过期（不炸整批 sync）。"""
    from engine.core.task_scheduler import _once_is_past

    past_aware = (datetime.now() - timedelta(minutes=5)).astimezone().isoformat()
    future_aware = (datetime.now() + timedelta(hours=1)).astimezone().isoformat()
    assert _once_is_past({"strategy_config": {"run_date": past_aware}}) is True
    assert _once_is_past({"strategy_config": {"run_date": future_aware}}) is False
    # 坏值：不抛异常、按未过期处理
    assert _once_is_past({"strategy_config": {"run_date": "not-a-date"}}) is False
    assert _once_is_past({"strategy_config": {"run_date": ""}}) is False


def test_sync_tasks_isolates_bad_task_from_batch():
    """T3：单任务异常（非法 id/坏配置）只跳过该任务，其余任务照常入列。"""
    fired: List[Any] = []
    sched = LocalTaskScheduler(runner=lambda task: fired.append(task))
    sched.start(refresh_seconds=3600)
    try:
        result = sched.sync_tasks(
            [
                _task(id="not-an-int"),  # int(id) 炸 → 隔离跳过
                _task(id=2, name="正常任务"),
            ]
        )
        assert sched.scheduled_task_ids() == [2]
        assert result["added"] == 1
        assert result["skipped"] >= 1
    finally:
        sched.shutdown()


# --------------------------- T4：本机 adb 清单（local_serials） --------------------------- #

def test_local_serials_parses_adb_devices_output(monkeypatch):
    """adb devices 输出解析：仅 state=device 的 serial 入集合。"""
    import subprocess

    from engine import adb_scan

    class _Proc:
        returncode = 0
        stdout = (
            b"List of devices attached\n"
            b"USB1\tdevice\n"
            b"USB2\toffline\n"
            b"USB3\tunauthorized\n"
            b"emulator-5554\tdevice\n"
        )
        stderr = b""

    monkeypatch.setattr(adb_scan, "find_adb_path", lambda explicit=None: "/usr/bin/adb")
    monkeypatch.setattr(
        adb_scan.subprocess, "run", lambda *args, **kwargs: _Proc()
    )
    assert adb_scan.local_serials() == {"USB1", "emulator-5554"}


def test_local_serials_unknown_when_adb_missing_or_failing(monkeypatch):
    """adb 缺失/命令失败 → None（未知清单，调用方 fail-open 不过滤）。"""
    import subprocess

    from engine import adb_scan

    monkeypatch.setattr(adb_scan, "find_adb_path", lambda explicit=None: None)
    assert adb_scan.local_serials() is None

    class _Fail:
        returncode = 1
        stdout = b""
        stderr = b"adb server dead"

    monkeypatch.setattr(adb_scan, "find_adb_path", lambda explicit=None: "/usr/bin/adb")
    monkeypatch.setattr(adb_scan.subprocess, "run", lambda *args, **kwargs: _Fail())
    assert adb_scan.local_serials() is None

    def _boom(*args, **kwargs):
        raise OSError("no adb")

    monkeypatch.setattr(adb_scan.subprocess, "run", _boom)
    assert adb_scan.local_serials() is None


# --------------------------- 拉取契约 / 登录接线 --------------------------- #

def test_refresh_now_pulls_client_tasks_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    config_store.reset_for_tests()
    presence.reset_for_tests()
    try:
        config_store.set_upstream("http://127.0.0.1:9")
        presence.set_token("tok-test")
        seen = {}

        def _get_json(self, path, params=None):
            seen["called"] = True
            seen["path"] = path
            # 平台契约：GET /api/client-tasks → {total, items}
            return {"total": 1, "items": [_task()]}

        monkeypatch.setattr(cloud_bridge.CloudBridge, "get_json", _get_json)
        sched = LocalTaskScheduler(runner=lambda task: None)
        try:
            result = sched.refresh_now()
            assert seen.get("called") is True
            assert result["refreshed"] == 1 and result["scheduled"] == 1
            assert sched.scheduled_task_ids() == [1]
        finally:
            sched.shutdown()
    finally:
        presence.reset_for_tests()
        config_store.reset_for_tests()


def test_refresh_now_skips_without_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    config_store.reset_for_tests()
    presence.reset_for_tests()
    try:
        config_store.set_upstream("http://127.0.0.1:9")

        def _fetch(self):
            raise AssertionError("无 token 不应拉取")

        monkeypatch.setattr(cloud_bridge.CloudBridge, "fetch_client_tasks", _fetch)
        sched = LocalTaskScheduler(runner=lambda task: None)
        try:
            result = sched.refresh_now()
            assert result.get("refreshed", 0) == 0
        finally:
            sched.shutdown()
    finally:
        presence.reset_for_tests()
        config_store.reset_for_tests()


def test_login_response_triggers_task_refresh(client, monkeypatch):
    """登录（/api/auth/token）→ 登记 token 并后台刷新本地任务调度。"""
    refreshed = []
    monkeypatch.setattr(presence, "start_heartbeat_loop", lambda http: None)
    monkeypatch.setattr(
        task_scheduler.scheduler, "refresh_async", lambda: refreshed.append(True)
    )
    resp = client.post("/api/auth/token", json={"username": "u", "password": "p"})
    assert resp.status_code == 200
    assert presence.get_token() == "tok-test"
    assert refreshed == [True]
