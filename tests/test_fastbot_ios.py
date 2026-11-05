"""iOS 稳定性（主机驱动 WDA）单测：事件生成 / .ips 解析 / 主循环守护与崩溃 / 设备汇总。"""
import json
import random

import pytest

from engine.core.fastbot_ios import crash as ios_crash
from engine.core.fastbot_ios import perf as ios_perf
from engine.core.fastbot_ios import runner as ios_runner
from engine.core.fastbot_ios import tools as ios_tools
from engine.core.fastbot_ios.monkey import BATCH_SIZE, EventGenerator, EventWeights
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
    script = []  # app_state 返回序列；元素为 Exception 时抛出

    def __init__(self, host, bundle_id):
        self.window = {"width": 390.0, "height": 844.0}
        self.calls = []
        FakeSession.last = self

    def open(self):
        pass

    def close(self):
        pass

    def app_state(self):
        if not FakeSession.script:
            return 4
        value = FakeSession.script.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def launch(self):
        self.calls.append("launch")

    def activate(self):
        self.calls.append("activate")

    def perform(self, payload):
        self.calls.append("perform")

    def press_button(self, name):
        self.calls.append("home")


@pytest.fixture
def fake_wda(monkeypatch):
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
    return ios_runner.run_ios_fastbot_local(
        device_serial=SERIAL, bundle_id="com.demo.app", duration=duration,
        enable_local_replay=False, report_dir=str(tmp_path), host_factory=fake_wda, **kw)


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
    [payload] = ios_tools.scan_ios_devices()
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
