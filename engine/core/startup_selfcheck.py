"""Offline frozen-engine check: load both adapters and verify report contract.

No device enumeration, WDA connection, ADB process or cloud request is allowed.
The build scripts execute this from the installed bundle's own module tree.
"""
from __future__ import annotations


async def check_startup():
    import importlib
    from engine.core.startup.contract import StartupConfig
    from engine.core.startup_service import REPORT_COLUMNS, _empty_report, capabilities
    from engine.core.startup_trace_analysis import verify_trace_processor

    android = importlib.import_module("engine.core.startup.android")
    ios = importlib.import_module("engine.core.startup.ios")
    runner = importlib.import_module("engine.core.startup.runner")
    assert callable(android.AndroidStartupAdapter)
    assert callable(ios.IOSStartupAdapter)
    assert callable(runner.run_startup)
    config = StartupConfig.model_validate({
        "platform": "android", "package_name": "com.autodroid.selfcheck",
        "device_serials": ["OFFLINE_SELFCHECK"], "iterations": 1,
        "startup_modes": ["cold"], "cooldown_sec": 0,
        "perfetto_slow_trace": {"enabled": False},
    }).model_dump(mode="json")
    report = _empty_report(config, status="ABORTED", error="offline selfcheck")
    assert set(report) == set(REPORT_COLUMNS)
    assert report["summary"]["startup_config"]["platform"] == "android"
    assert capabilities()["protocol_version"] == 1
    trace_processor = verify_trace_processor()
    if not trace_processor["available"]:
        raise RuntimeError(f"Trace Processor 自检失败：{trace_processor['status']} {trace_processor.get('reason', '')}")
    return {"ok": True, "offline": True, "protocol_version": 1,
            "adapters": ["android", "ios"], "report_columns": list(REPORT_COLUMNS),
            "trace_processor": trace_processor}
