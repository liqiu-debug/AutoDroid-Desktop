"""iOS CPU/内存采样：外部调用 pymobiledevice3 sysmon（GPL-3.0，只作外部 CLI，不随包分发）。

实测 iOS 27.3 经 USB 无需 tunnel，单次快照 ≈3.6s；cpuUsage 为百分比，physFootprint 为字节。
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from engine.core.fastbot_ios.tools import which

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 5.0
COMMAND_TIMEOUT_SECONDS = 30


def resolve_pymobiledevice3() -> Optional[str]:
    import os

    found = which("pymobiledevice3")
    if found:
        return found
    candidate = os.path.expanduser("~/.local/bin/pymobiledevice3")
    return candidate if os.path.isfile(candidate) else None


def _run_json(args: List[str]) -> Any:
    out = subprocess.run(args, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or b"").decode(errors="ignore")[-300:])
    return json.loads(out.stdout.decode("utf-8", errors="replace") or "null")


def resolve_executable(exe: str, udid: str, bundle_id: str) -> Optional[str]:
    try:
        data = _run_json([exe, "apps", "query", bundle_id, "--udid", udid])
    except Exception as exc:
        logger.warning("查询 App 可执行名失败: %s", exc)
        return None
    if isinstance(data, dict):
        entry = data.get(bundle_id) if bundle_id in data else data
        if isinstance(entry, dict):
            return entry.get("CFBundleExecutable")
    return None


def parse_snapshot(records: Any) -> Optional[Dict[str, float]]:
    if not isinstance(records, list) or not records:
        return None
    cpu = sum(float(r.get("cpuUsage") or 0.0) for r in records if isinstance(r, dict))
    mem = sum(float(r.get("physFootprint") or 0.0) for r in records if isinstance(r, dict))
    return {"cpu": round(cpu, 1), "mem": round(mem / (1024 * 1024), 1)}


class PerfSampler:
    def __init__(self, udid: str, bundle_id: str, sink: List[Dict[str, Any]]) -> None:
        self.udid = udid
        self.bundle_id = bundle_id
        self.sink = sink
        self.exe = resolve_pymobiledevice3()
        self.exec_name = resolve_executable(self.exe, udid, bundle_id) if self.exe else None
        self.available = bool(self.exe and self.exec_name)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ios-perf", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=COMMAND_TIMEOUT_SECONDS + 5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                records = _run_json([
                    self.exe, "developer", "dvt", "sysmon", "process", "single", "--udid", self.udid,
                    "-f", f"name={self.exec_name}", "-k", "cpuUsage", "-k", "physFootprint",
                ])
                point = parse_snapshot(records)
                if point is not None:
                    self.sink.append({"time": datetime.now().strftime("%H:%M:%S"), **point})
            except Exception as exc:
                logger.debug("iOS 性能采样失败: %s", exc)
            self._stop.wait(SAMPLE_INTERVAL_SECONDS)
