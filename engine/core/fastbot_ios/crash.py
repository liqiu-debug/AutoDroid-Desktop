"""iOS 崩溃报告采集：lockdown 拉取 .ips（idevicecrashreport -k 只拷贝不删除）+ 解析。

实测：崩溃后 apps/state 0.75s 内变 1，8s 内能拉到新 .ips；锁屏也可用。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Set

from engine.core.fastbot_ios.tools import which

logger = logging.getLogger(__name__)

PULL_TIMEOUT_SECONDS = 120
# bug_type：309 = 应用崩溃（EXC_*），298 = Jetsam（内存）
BUG_TYPE_CRASH = "309"
BUG_TYPE_JETSAM = "298"


def parse_ips(path: str, max_frames: int = 40) -> Dict[str, Any]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        header_line = handle.readline()
        body_text = handle.read()
    try:
        header = json.loads(header_line)
    except ValueError:
        header = {}
    info: Dict[str, Any] = {
        "file": os.path.basename(path),
        "bug_type": str(header.get("bug_type") or ""),
        "app_name": header.get("app_name"),
        "bundle_id": header.get("bundleID"),
        "app_version": header.get("app_version"),
        "os_version": header.get("os_version"),
        "timestamp": header.get("timestamp"),
    }
    try:
        body = json.loads(body_text)
    except ValueError:
        info["raw"] = body_text[:20000]
        return info
    if info["bug_type"] == BUG_TYPE_JETSAM:
        killed = [
            {"name": proc.get("name"), "reason": proc.get("reason"), "pid": proc.get("pid"),
             "rpages": proc.get("rpages")}
            for proc in (body.get("processes") or [])
            if isinstance(proc, dict) and proc.get("reason")
        ]
        info["jetsam_killed"] = killed
        return info
    exc = body.get("exception") or {}
    term = body.get("termination") or {}
    info["exception"] = {k: exc.get(k) for k in ("type", "signal", "codes", "subtype")}
    info["termination"] = {k: term.get(k) for k in ("namespace", "code", "indicator", "byProc")}
    info["pid"] = body.get("pid")
    info["proc_name"] = body.get("procName")
    threads = body.get("threads") or []
    images = body.get("usedImages") or []
    fault = body.get("faultingThread")
    frames: List[str] = []
    if isinstance(fault, int) and 0 <= fault < len(threads):
        for frame in (threads[fault].get("frames") or [])[:max_frames]:
            idx = frame.get("imageIndex", -1)
            image = images[idx] if isinstance(idx, int) and 0 <= idx < len(images) else {}
            symbol = frame.get("symbol") or ""
            frames.append(f"{image.get('name', '?')} +{frame.get('imageOffset')} {symbol}".strip())
    info["faulting_frames"] = frames
    if body.get("asi"):
        info["asi"] = body.get("asi")
    return info


def report_epoch(info: Dict[str, Any]) -> Optional[float]:
    """.ips 头部 timestamp（如 ``2026-11-05 20:03:48.00 +0800``）→ epoch 秒。"""
    from datetime import datetime

    text = str(info.get("timestamp") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def matches_bundle(info: Dict[str, Any], bundle_id: str, app_names: Set[str]) -> bool:
    if info.get("bundle_id"):
        return info.get("bundle_id") == bundle_id
    if info.get("bug_type") == BUG_TYPE_JETSAM:
        return any(p.get("name") in app_names for p in info.get("jetsam_killed") or [])
    # 无 bundleID 的报告才按进程名兜底
    return bool(info.get("app_name") and info.get("app_name") in app_names) or bool(
        info.get("proc_name") and info.get("proc_name") in app_names
    )


def format_full_log(info: Dict[str, Any]) -> str:
    exc = info.get("exception") or {}
    term = info.get("termination") or {}
    lines = [
        f"Report: {info.get('file')} (bug_type {info.get('bug_type')})",
        f"Process: {info.get('proc_name') or info.get('app_name')} [{info.get('pid')}]  "
        f"Version: {info.get('app_version')}  OS: {info.get('os_version')}",
        f"Exception: {exc.get('type')} {exc.get('signal') or ''} {exc.get('subtype') or ''}".rstrip(),
        f"Termination: {term.get('namespace')} code={term.get('code')} by={term.get('byProc')} {term.get('indicator') or ''}".rstrip(),
    ]
    for proc in info.get("jetsam_killed") or []:
        lines.append(f"Jetsam: {proc.get('name')} [{proc.get('pid')}] reason={proc.get('reason')} rpages={proc.get('rpages')}")
    if info.get("asi"):
        lines.append(f"Application Specific Information: {json.dumps(info['asi'], ensure_ascii=False)[:2000]}")
    if info.get("faulting_frames"):
        lines.append("Faulting thread:")
        lines.extend(f"  {i:>2} {frame}" for i, frame in enumerate(info["faulting_frames"]))
    if info.get("raw"):
        lines.append(info["raw"])
    return "\n".join(lines)


class CrashCollector:
    def __init__(self, udid: str, work_dir: str) -> None:
        self.udid = udid
        self.work_dir = work_dir
        self._seen: Set[str] = set()
        self.available = bool(which("idevicecrashreport"))

    def _pull(self) -> Dict[str, str]:
        exe = which("idevicecrashreport")
        if not exe:
            return {}
        os.makedirs(self.work_dir, exist_ok=True)  # 目录不存在 idevicecrashreport 直接报错
        try:
            subprocess.run([exe, "-u", self.udid, "-k", "-e", self.work_dir],
                           capture_output=True, timeout=PULL_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("拉取 iOS 崩溃报告失败: %s", exc)
        return {os.path.basename(p): p for p in glob.glob(os.path.join(self.work_dir, "**", "*.ips"), recursive=True)}

    def baseline(self) -> None:
        self._seen = set(self._pull())

    def collect_new(self, bundle_id: str, app_names: Set[str], wait_seconds: float = 15.0) -> List[Dict[str, Any]]:
        """崩溃后轮询新增报告（报告落盘有延迟），返回属于目标 App 的解析结果。"""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            reports = self._pull()
            fresh = [name for name in reports if name not in self._seen]
            matched: List[Dict[str, Any]] = []
            for name in sorted(fresh):
                self._seen.add(name)
                try:
                    info = parse_ips(reports[name])
                except OSError:
                    continue
                if matches_bundle(info, bundle_id, app_names):
                    matched.append(info)
            if matched or time.monotonic() >= deadline:
                return matched
            time.sleep(2.0)
