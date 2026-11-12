"""iOS 稳定性（主机驱动 WDA）单测：事件生成 / .ips 解析 / 主循环守护与崩溃 / 设备汇总。"""
import json
import random

import pytest

from engine.core.fastbot_ios import crash as ios_crash
from engine.core.fastbot_ios import perf as ios_perf
from engine.core.fastbot_ios import runner as ios_runner
from engine.core.fastbot_ios import tools as ios_tools
from engine.core.fastbot_ios.monkey import BATCH_SIZE, EventGenerator, EventWeights, extract_tap_targets
from engine.core.fastbot_ios.wda import WdaError

UDID = "00008120-00146CA4110BC01E"
SERIAL = UDID


def _write_ips(path, bundle="com.demo.app", bug_type="309", by_proc="com.demo.app"):
    header = {"bug_type": bug_type, "app_name": "Demo", "bundleID": bundle, "app_version": "1.0",
              "os_version": "iPhone OS 27.3", "timestamp": "2026-11-05"}
    body = {"pid": 42, "procName": "Demo", "faultingThread": 0,
            "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"},
            "termination": {"namespace": "SIGNAL", "code": 11, "byProc": by_proc},
            "usedImages": [{"name": "Demo"}],
            "threads": [{"frames": [{"imageIndex": 0, "imageOffset": 1234, "symbol": "-[VC tap:]"}]}]}
    path.write_text(json.dumps(header) + "\n" + json.dumps(body))


def test_udid_validation():
    assert ios_tools.udid_from_serial(SERIAL) == UDID
    assert ios_tools.udid_from_serial("ios:" + UDID) == UDID
    assert ios_tools.is_ios_serial(UDID) and ios_tools.is_ios_serial("a" * 40)
    assert not ios_tools.is_ios_serial("3B6F68E6SA7TA0Q5")
    with pytest.raises(ValueError):
        ios_tools.udid_from_serial("ios:abc;rm -rf")


def test_generator_batches_and_blocked_regions():
    gen = EventGenerator(390, 844, EventWeights(touch=1, motion=0, long_press=0, back=0, syskeys=0),
                         rng=random.Random(1))
    payload, kinds = gen.batch(100)
    assert kinds == ["touch"] * BATCH_SIZE
    moves = [a for a in payload[0]["actions"] if a["type"] == "pointerMove"]
    assert all(844 * 0.06 < m["y"] < 844 * 0.95 for m in moves)


def test_generator_syskeys_is_standalone():
    gen = EventGenerator(390, 844, EventWeights(touch=0, motion=0, long_press=0, back=0, syskeys=1))
    payload, kinds = gen.batch(100)
    assert payload is None and kinds == ["syskeys"]


def test_parse_ips_and_full_log(tmp_path):
    path = tmp_path / "Demo-2026.ips"
    _write_ips(path)
    info = ios_crash.parse_ips(str(path))
    assert info["bundle_id"] == "com.demo.app"
    assert info["exception"]["signal"] == "SIGSEGV"
    assert info["faulting_frames"] == ["Demo +1234 -[VC tap:]"]
    log = ios_crash.format_full_log(info)
    assert "EXC_BAD_ACCESS" in log and "-[VC tap:]" in log


def test_collector_returns_only_new_matching(tmp_path, monkeypatch):
    work = tmp_path / "pull"
    work.mkdir()
    _write_ips(work / "old.ips")
    collector = ios_crash.CrashCollector(UDID, str(work))
    monkeypatch.setattr(collector, "_pull", lambda: {p.name: str(p) for p in work.glob("*.ips")})
    collector.baseline()
    _write_ips(work / "other.ips", bundle="com.other")
    _write_ips(work / "new.ips")
    found = collector.collect_new("com.demo.app", {"Demo"}, wait_seconds=0)
    assert [f["file"] for f in found] == ["new.ips"]


def test_perf_parse_snapshot():
    point = ios_perf.parse_snapshot([{"cpuUsage": 12.34, "physFootprint": 50 * 1024 * 1024}])
    assert point == {"cpu": 12.3, "mem": 50.0}
    assert ios_perf.parse_snapshot([]) is None


class FakeHost:
    def __init__(self, udid):
        self.udid = udid
        self.mjpeg_port = 0
        self.recovered = 0
        self.closed = False

    def ensure_ready(self):
        pass

    def status_ok(self):
        return False

    def recover(self):
        self.recovered += 1
        return True

    def close(self):
        self.closed = True


class FakeSession:
    """script：每轮守护的状态序列。4 = 前台；1 = 未运行；2/3 = 后台；Exception = 抛出。
    前台 App 随状态推导（4 → 目标 App，其余 → springboard）；activate/launch 会把目标拉回前台。"""
    script = []
    front_after_activate = True

    def __init__(self, host, bundle_id):
        self.window = {"width": 390.0, "height": 844.0}
        self.calls = []
        self.bundle_id = bundle_id
        self.current = 4
        FakeSession.last = self

    # 系统托管界面 / 系统弹窗场景：最前面的 bundle 与 app_state 解耦
    front_override = None
    alert_clears_front = False

    def accept_alert(self):
        self.calls.append("accept_alert")
        if FakeSession.alert_clears_front:
            FakeSession.front_override = None
            return True
        return False

    def open(self):
        pass

    def close(self):
        pass

    def active_bundle(self):
        if FakeSession.front_override:
            return FakeSession.front_override
        if FakeSession.script:
            value = FakeSession.script.pop(0)
            if isinstance(value, Exception):
                raise value
            self.current = value
        return self.bundle_id if self.current == 4 else "com.apple.springboard"

    def app_state(self):
        return self.current

    def launch(self):
        self.calls.append("launch")
        self.current = 4

    def activate(self):
        self.calls.append("activate")
        if FakeSession.front_after_activate:
            self.current = 4

    def terminate(self):
        self.calls.append("terminate")
        self.current = 1

    def source_json(self):
        return {"type": "Application", "children": [
            {"type": "Button", "rect": {"x": 100, "y": 400, "width": 80, "height": 40}, "isEnabled": "1", "isVisible": "1"},
        ]}

    def perform(self, payload):
        self.calls.append("perform")

    def press_button(self, name):
        self.calls.append("home")


@pytest.fixture
def fake_wda(monkeypatch):
    FakeSession.front_after_activate = True
    FakeSession.front_override = None
    FakeSession.alert_clears_front = False
    monkeypatch.setattr(ios_runner, "WdaSession", FakeSession)
    monkeypatch.setattr(ios_runner.time, "sleep", lambda s: None)
    return FakeHost


def _run(fake_wda, monkeypatch, tmp_path, script, reports, duration=0.3, **kw):
    monkeypatch.setattr(ios_perf, "resolve_pymobiledevice3", lambda: None)
    FakeSession.script = list(script)
    pulls = iter(reports)
    monkeypatch.setattr(ios_crash.CrashCollector, "baseline", lambda self: None)
    monkeypatch.setattr(ios_crash.CrashCollector, "collect_new",
                        lambda self, b, n, w: next(pulls, []))
    monkeypatch.setattr(ios_crash, "which", lambda name: "/bin/true")
    kw.setdefault("enable_local_replay", False)  # 默认不录制，回放用例自己打开（替身 MJPEG）
    return ios_runner.run_ios_fastbot_local(
        device_serial=SERIAL, bundle_id="com.demo.app", duration=duration,
        report_dir=str(tmp_path), host_factory=fake_wda, **kw)


def test_crash_detected_and_stops_without_ignore(fake_wda, monkeypatch, tmp_path):
    info = {"file": "a.ips", "bug_type": "309", "bundle_id": "com.demo.app",
            "exception": {"type": "EXC_BAD_ACCESS"}, "termination": {}, "faulting_frames": ["x"]}
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 4, 1], [[info]], duration=30)
    assert report["summary"]["total_crashes"] == 1
    assert report["summary"]["aborted_reason"] == "crash"
    assert report["crash_events"][0]["platform"] == "ios"
    assert report["summary"]["platform"] == "ios"


def test_crash_with_ignore_relaunches(fake_wda, monkeypatch, tmp_path):
    info = {"file": "a.ips", "bug_type": "298", "bundle_id": "com.demo.app",
            "exception": {}, "termination": {}}
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 1], [[info]], ignore_crashes=True)
    assert report["crash_events"][0]["type"] == "OOM"
    assert report["summary"]["total_crashes"] == 1 and report["summary"]["total_oom"] == 1
    assert "launch" in FakeSession.last.calls
    assert "aborted_reason" not in report["summary"]


def test_exit_without_report_is_not_crash(fake_wda, monkeypatch, tmp_path):
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 1], [[]])
    assert report["summary"]["total_crashes"] == 0
    assert report["summary"]["ios_stats"]["external_kills"] == 1


def test_escape_reactivates(fake_wda, monkeypatch, tmp_path):
    report = _run(fake_wda, monkeypatch, tmp_path, [3], [])
    assert report["summary"]["ios_stats"]["escapes"] == 1
    assert "activate" in FakeSession.last.calls


def test_wda_watchdog_recovers(fake_wda, monkeypatch, tmp_path):
    err = WdaError("boom")
    report = _run(fake_wda, monkeypatch, tmp_path, [err, err, err], [])
    stats = report["summary"]["ios_stats"]
    assert stats["wda_recoveries"] == 1 and stats["wda_errors"] == 3
    assert stats["events"] > 0


def test_perf_degraded_when_tool_missing(fake_wda, monkeypatch, tmp_path):
    monkeypatch.setattr(ios_perf, "resolve_pymobiledevice3", lambda: None)
    report = _run(fake_wda, monkeypatch, tmp_path, [], [], enable_performance_monitor=True)
    assert "performance_monitor" in report["summary"]["degraded"]


class FakeReplayRecorder:
    """MjpegReplayRecorder 替身：不连 MJPEG 端口（测试不碰网络）。"""

    started = 0
    stopped = 0

    def __init__(self, port, replay_dir):
        self.port = port
        self.replay_dir = replay_dir
        self.frames_received = 0

    def start(self):
        FakeReplayRecorder.started += 1

    def stop(self):
        FakeReplayRecorder.stopped += 1

    def capture(self, event_type, event_time, event_mono=None):
        return {"status": "READY", "filename": "crash_t.mp4"}


def test_replay_status_on_when_recorder_started(fake_wda, monkeypatch, tmp_path):
    """回放状态口径对齐 Android 轻量档：录上了记 on，且不写 local_replay_error。"""
    info = {"file": "a.ips", "bug_type": "309", "bundle_id": "com.demo.app",
            "exception": {"type": "EXC_BAD_ACCESS"}, "termination": {}}
    FakeReplayRecorder.started = FakeReplayRecorder.stopped = 0
    monkeypatch.setattr(ios_runner, "MjpegReplayRecorder", FakeReplayRecorder)
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 4, 1], [[info]],
                  ignore_crashes=True, enable_local_replay=True)
    summary = report["summary"]
    assert summary["local_replay_enabled"] is True
    assert summary["local_replay_status"] == "on"
    assert "local_replay_error" not in summary
    assert report["crash_events"][0]["replay"]["status"] == "READY"
    assert FakeReplayRecorder.started == 1 and FakeReplayRecorder.stopped == 1


def test_replay_status_error_when_recorder_fails(fake_wda, monkeypatch, tmp_path):
    """录制起不来只降级（口径同 Android 轻量档），不让整轮稳定性测试陪葬。"""

    class BrokenRecorder:
        def __init__(self, port, replay_dir):
            raise OSError("mjpeg 端口连不上")

    monkeypatch.setattr(ios_runner, "MjpegReplayRecorder", BrokenRecorder)
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 1], [[]], enable_local_replay=True)
    summary = report["summary"]
    assert summary["local_replay_enabled"] is False
    assert summary["local_replay_status"] == "error"
    assert "mjpeg 端口连不上" in summary["local_replay_error"]
    assert "aborted_reason" not in summary, "回放降级不得中断本轮测试"


def test_replay_status_disabled_when_opted_out(fake_wda, monkeypatch, tmp_path):
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 1], [[]])
    summary = report["summary"]
    assert summary["local_replay_enabled"] is False
    assert summary["local_replay_status"] == "disabled"
    assert "local_replay_error" not in summary


def test_device_scan_union(monkeypatch):
    from engine import device_scan

    monkeypatch.setattr("engine.adb_scan.local_serials", lambda: {"A1"})
    monkeypatch.setattr(ios_tools, "local_ios_serials", lambda: {SERIAL})
    assert device_scan.all_local_serials() == {"A1", SERIAL}
    monkeypatch.setattr("engine.adb_scan.local_serials", lambda: None)
    monkeypatch.setattr(ios_tools, "local_ios_serials", lambda: None)
    assert device_scan.all_local_serials() is None


def test_scan_ios_payload(monkeypatch):
    monkeypatch.setattr(ios_tools, "list_udids", lambda: [UDID])
    monkeypatch.setattr(ios_tools, "device_info",
                        lambda u: {"ProductType": "iPhone15,2", "ProductVersion": "27.3"})
    monkeypatch.setattr(ios_tools, "devicectl_marketing_names", lambda: {})
    [payload] = ios_tools.scan_ios_devices()
    assert payload["model"] == "iPhone 14 Pro", "机型标识映射为市场名"
    monkeypatch.setattr(ios_tools, "devicectl_marketing_names", lambda: {UDID: "iPhone 14 Pro (devicectl)"})
    [payload] = ios_tools.scan_ios_devices()
    assert payload["model"] == "iPhone 14 Pro (devicectl)", "devicectl 市场名优先"
    assert payload["serial"] == SERIAL and payload["platform"] == "ios"
    assert payload["connection_type"] == "usb" and payload["custom_name"] is None


def test_start_fastbot_run_accepts_ios(monkeypatch):
    from engine.core import fastbot_service

    captured = {}
    monkeypatch.setattr(fastbot_service, "create_cloud_task", lambda b, p: {"id": 7})
    monkeypatch.setattr(fastbot_service, "run_and_report", lambda **kw: captured.update(kw))
    monkeypatch.setattr(fastbot_service, "prune_local_reports", lambda: None)

    class B:
        auth_headers = {}

    fastbot_service.start_fastbot_run(B(), {"device_serial": SERIAL, "package_name": "com.demo.app"})
    assert captured["device_serial"] == SERIAL
    with pytest.raises(ValueError):
        fastbot_service.start_fastbot_run(B(), {"device_serial": "ios:bad udid", "package_name": "x.y"})


def test_jetsam_matches_by_process_name(tmp_path):
    path = tmp_path / "JetsamEvent.ips"
    path.write_text(json.dumps({"bug_type": "298"}) + "\n" + json.dumps(
        {"processes": [{"name": "Demo", "reason": "per-process-limit", "pid": 9}, {"name": "x"}]}))
    info = ios_crash.parse_ips(str(path))
    assert ios_crash.matches_bundle(info, "com.demo.app", {"Demo"})
    assert not ios_crash.matches_bundle(info, "com.demo.app", {"Other"})
    assert "per-process-limit" in ios_crash.format_full_log(info)


def test_report_epoch_parses_ips_timestamp():
    ts = ios_crash.report_epoch({"timestamp": "2026-11-05 20:03:48.00 +0800"})
    assert ts is not None and int(ts) % 60 == 48
    assert ios_crash.report_epoch({"timestamp": "garbage"}) is None


def test_replay_buffer_downsamples_and_tracks_bytes(tmp_path):
    from engine.core.fastbot_ios.replay import MjpegReplayRecorder

    rec = MjpegReplayRecorder(0, str(tmp_path))
    rec._push(b"a" * 10)
    rec._push(b"b" * 4)  # 同一 100ms 窗口内：替换上一帧
    assert len(rec._frames) == 1 and rec._bytes == 4 and rec.frames_received == 2


def test_crash_found_after_wda_recovery(fake_wda, monkeypatch, tmp_path):
    """批内崩溃 → WDA 连续失败 → 重建会话（App 被重新拉起）→ 仍能从 .ips 记到崩溃。"""
    info = {"file": "a.ips", "bug_type": "309", "bundle_id": "com.demo.app",
            "exception": {"type": "EXC_BAD_ACCESS"}, "termination": {}, "faulting_frames": []}
    err = WdaError("timed out")
    report = _run(fake_wda, monkeypatch, tmp_path, [4, err, err, err], [[info]], duration=30)
    assert report["summary"]["total_crashes"] == 1
    assert report["summary"]["aborted_reason"] == "crash"


def test_targets_from_source_only_visible_interactive_in_screen():
    from engine.core.fastbot_ios.monkey import extract_tap_targets

    src = {"type": "Application", "children": [
        {"type": "Button", "rect": {"x": 10, "y": 100, "width": 100, "height": 40}, "isEnabled": "1", "isVisible": "1"},
        {"type": "XCUIElementTypeCell", "rect": {"x": 0, "y": 200, "width": 390, "height": 50}},
        {"type": "Button", "rect": {"x": 10, "y": 300, "width": 50, "height": 40}, "isEnabled": "0"},
        {"type": "Other", "rect": {"x": 10, "y": 400, "width": 50, "height": 40}},
        {"type": "Button", "rect": {"x": 10, "y": 2000, "width": 50, "height": 40}},
    ]}
    assert extract_tap_targets(src, 390, 844) == [(60.0, 120.0), (195.0, 225.0)]


def test_default_weights_are_tap_heavy_and_taps_hit_targets():
    gen = EventGenerator(390, 844, rng=random.Random(3))
    assert gen.weights.touch > gen.weights.motion + gen.weights.back
    gen.targets = [(200.0, 400.0)]
    hits = sum(1 for _ in range(200) if gen._tap_point() == (200.0, 400.0))
    assert 110 <= hits <= 170  # ≈70%


def test_home_screen_detected_by_front_app_not_lagging_state(fake_wda, monkeypatch, tmp_path):
    """按 Home 后 apps/state 仍报 4：以前台 App 判断，立即拉回而不是往桌面上点。"""
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 3], [])
    assert report["summary"]["ios_stats"]["escapes"] == 1
    assert "activate" in FakeSession.last.calls


def test_activate_failing_escalates_to_relaunch(fake_wda, monkeypatch, tmp_path):
    FakeSession.front_after_activate = False
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 3], [])
    calls = FakeSession.last.calls
    assert calls.count("activate") >= 2 and "terminate" in calls and "launch" in calls
    assert report["summary"]["ios_stats"]["foreground_failures"] == 0


def test_custom_weights_map_remainder_to_long_press(monkeypatch):
    from engine.core import fastbot_service
    from engine.core.fastbot_ios import runner

    seen = {}
    monkeypatch.setattr(runner, "run_ios_fastbot_local", lambda **kw: seen.update(kw) or {})
    fastbot_service._run_ios(weights={"enable_custom_event_weights": True, "pct_touch": 50, "pct_motion": 20,
                                      "pct_majornav": 10, "pct_syskeys": 0}, package_name="com.a.b",
                             device_serial=SERIAL)
    w = seen["weights"]
    assert (w.touch, w.motion, w.back, w.syskeys, w.long_press) == (50, 20, 10, 0, 20)


def test_busy_device_rejects_second_run_and_is_released(monkeypatch):
    from engine.core import device_busy, fastbot_service

    device_busy.reset_for_tests()
    finished = {}
    monkeypatch.setattr(fastbot_service, "create_cloud_task", lambda b, p: {"id": 9})
    monkeypatch.setattr(fastbot_service, "run_and_report", lambda **kw: finished.update(kw))
    monkeypatch.setattr(fastbot_service, "prune_local_reports", lambda: None)

    class B:
        auth_headers = {}

    fastbot_service.start_fastbot_run(B(), {"device_serial": SERIAL, "package_name": "com.demo.app"})
    assert device_busy.is_busy(SERIAL)
    with pytest.raises(ValueError, match="正在跑稳定性测试"):
        fastbot_service.start_fastbot_run(B(), {"device_serial": SERIAL, "package_name": "com.demo.app"})
    device_busy.release(SERIAL)

    # 云端登记失败：不能把设备一直占着
    monkeypatch.setattr(fastbot_service, "create_cloud_task", lambda b, p: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        fastbot_service.start_fastbot_run(B(), {"device_serial": SERIAL, "package_name": "com.demo.app"})
    assert not device_busy.is_busy(SERIAL)


def test_zero_percent_weights_stay_zero(monkeypatch):
    from engine.core import fastbot_service

    seen = {}
    monkeypatch.setattr(fastbot_service, "create_cloud_task", lambda b, p: {"id": 5})
    monkeypatch.setattr(fastbot_service, "run_and_report", lambda **kw: seen.update(kw))
    monkeypatch.setattr(fastbot_service, "prune_local_reports", lambda: None)

    class B:
        auth_headers = {}

    from engine.core import device_busy
    device_busy.reset_for_tests()
    fastbot_service.start_fastbot_run(B(), {"device_serial": "3B6F68E6SA7TA0Q5", "package_name": "com.demo.app",
                                            "enable_custom_event_weights": True, "pct_touch": 0, "pct_syskeys": 0})
    device_busy.reset_for_tests()
    assert seen["event_weights"]["pct_touch"] == 0 and seen["event_weights"]["pct_syskeys"] == 0
    assert seen["event_weights"]["pct_motion"] == 30


def test_crash_detected_even_when_log_capture_is_off(fake_wda, monkeypatch, tmp_path):
    """「不抓取日志」只影响报告里带不带日志：崩溃判定和「崩溃时立即停止」照常生效。"""
    info = {"file": "a.ips", "bug_type": "309", "bundle_id": "com.demo.app",
            "exception": {"type": "EXC_BAD_ACCESS"}, "termination": {}, "faulting_frames": ["x"]}
    report = _run(fake_wda, monkeypatch, tmp_path, [4, 4, 1], [[info]], duration=5, capture_log=False)
    assert report["summary"]["aborted_reason"] == "crash"
    assert len(report["crash_events"]) == 1
    assert report["crash_events"][0]["full_log"] == ""


def test_system_hosted_ui_keeps_testing_without_restart(fake_wda, monkeypatch, tmp_path):
    """App 仍在前台、最前面是 Safari 视图 / 相册选择器等系统界面：不拉回、不杀 App。"""
    FakeSession.front_override = "com.apple.SafariViewService"
    report = _run(fake_wda, monkeypatch, tmp_path, [], [], duration=0.2)
    stats = report["summary"]["ios_stats"]
    assert stats["batches"] > 0 and stats["escapes"] == 0
    assert "terminate" not in FakeSession.last.calls and "activate" not in FakeSession.last.calls


def test_system_alert_is_accepted_before_restarting_the_app(fake_wda, monkeypatch, tmp_path):
    """前台是桌面进程（系统弹窗）且 activate 无效：先接受弹窗，关掉了就不杀 App。"""
    FakeSession.front_override = "com.apple.springboard"
    FakeSession.front_after_activate = False
    FakeSession.alert_clears_front = True
    report = _run(fake_wda, monkeypatch, tmp_path, [], [], duration=0.2)
    assert "accept_alert" in FakeSession.last.calls
    assert "terminate" not in FakeSession.last.calls
    assert report["summary"]["ios_stats"]["relaunches"] == 0


def test_source_json_skips_slow_attributes():
    class _Http:
        def __init__(self):
            self.urls = []

        def request(self, method, url, json=None, timeout=None):
            self.urls.append(url)

            class _Resp:
                status_code = 200

                @staticmethod
                def json():
                    return {"value": {}}

            return _Resp()

    from engine.core.fastbot_ios.wda import WdaSession

    host = type("H", (), {"base_url": "http://x"})()
    http = _Http()
    session = WdaSession(host, "com.demo.app", http=http)
    session.session_id = "S"
    session.source_json()
    assert http.urls == ["http://x/session/S/source?format=json&excluded_attributes=visible,accessible"]


def test_targets_without_visibility_attribute_are_kept():
    source = {"type": "Application", "children": [
        {"type": "Button", "rect": {"x": 100, "y": 400, "width": 80, "height": 40}},
        {"type": "Button", "isEnabled": "0", "rect": {"x": 200, "y": 400, "width": 80, "height": 40}},
    ]}
    assert extract_tap_targets(source, 390, 844) == [(140.0, 420.0)]


def test_start_rejected_while_local_case_runs_on_device(monkeypatch):
    from engine.core import device_busy, fastbot_service, run_registry

    monkeypatch.setattr(fastbot_service, "prune_local_reports", lambda: None)
    record = run_registry.registry.register(
        run_registry.LocalRunRecord(kind="case", target_id=1, device_serial=SERIAL)
    )
    try:
        with pytest.raises(ValueError, match="正在执行用例"):
            fastbot_service.start_fastbot_run(
                type("B", (), {"auth_headers": {}})(), {"device_serial": SERIAL, "package_name": "com.demo.app"}
            )
        assert not device_busy.is_busy(SERIAL)
    finally:
        run_registry.registry.complete(record.run_id)


def test_device_released_when_finish_step_blows_up(monkeypatch):
    from engine.core import device_busy, fastbot_service

    assert device_busy.try_acquire(SERIAL)
    monkeypatch.setattr(fastbot_service, "_run_ios", lambda **kw: {"summary": {}})
    monkeypatch.setattr(fastbot_service, "CloudBridge", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    threads = []
    real_thread = fastbot_service.threading.Thread

    def capture(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        thread.run = _swallow(thread.run)
        threads.append(thread)
        return thread

    monkeypatch.setattr(fastbot_service.threading, "Thread", capture)
    fastbot_service.run_and_report(
        task_id=1, auth_headers={}, device_serial=SERIAL, package_name="com.demo.app", duration=1, throttle=500,
        ignore_crashes=False, capture_log=True, enable_performance_monitor=False,
    )
    threads[0].join(5)
    assert not device_busy.is_busy(SERIAL)


def _swallow(fn):
    def run():
        try:
            fn()
        except RuntimeError:
            pass

    return run
