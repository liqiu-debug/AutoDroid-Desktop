"""Local startup execution and a credential-free durable result outbox.

Every task has one cloud run_id and one guarded attempt. A process restart only
reconciles and uploads partial results; it never resumes timing device actions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from engine import config_store, presence
from engine.core import device_busy, run_registry
from engine.core.cloud_bridge import CloudBridge
from engine.core.run_registry import LocalRunRecord

logger = logging.getLogger(__name__)
REPORT_COLUMNS = ("performance_data", "jank_data", "jank_events", "trace_artifacts", "crash_events", "summary")
HEARTBEAT_SECONDS = 5.0
MAX_ARTIFACT_BYTES = 500 * 1024 * 1024


class LeaseStopEvent:
    """A stale lease fences every adapter action, even during network waits."""

    def __init__(self, job):
        self.job = job

    def is_set(self):
        record = self.job["record"]
        if time.monotonic() >= self.job["lease_until"]:
            record.abort_event.set()
        return record.abort_event.is_set()


def capabilities() -> Dict[str, Any]:
    from engine.core.startup.contract import startup_capabilities

    return startup_capabilities(trace_supported=True, web_dispatch=True)


def _empty_report(config: Dict[str, Any], status="RUNNING", error=None) -> Dict[str, Any]:
    return {
        **{key: [] for key in REPORT_COLUMNS if key != "summary"},
        "summary": {
            "session_type": "startup", "startup_config": config, "startup_runs": [],
            "startup_aggregate": {}, "platform": config.get("platform"), "status": status,
            "error": error, "progress": {"completed_count": 0, "requested_count":
                int(config.get("iterations", 3)) * len(config.get("startup_modes", ["cold", "hot"])),
                "valid_count": 0, "invalid_count": 0, "phase": status.lower()},
        },
    }


def _identity(bridge: CloudBridge) -> str:
    if not bridge.auth_headers:
        raise ValueError("请先登录平台后执行冷热启动测试")
    user = bridge.get_json("/api/auth/users/me")
    identifier = user.get("id") if isinstance(user, dict) else None
    if identifier is None:
        raise ValueError("无法确认当前登录账号，请重新登录")
    return str(identifier)


def _scope(upstream: str, owner: str) -> str:
    return hashlib.sha256(f"{upstream.rstrip('/')}\0{owner}".encode()).hexdigest()


def _credential(bridge):
    return {str(key).lower(): value for key, value in bridge.auth_headers.items()}


def _local_serials() -> set:
    from engine.device_scan import all_local_serials

    serials = all_local_serials()
    if serials is None:
        raise ValueError("无法读取本机设备清单，请检查 adb 和 iOS 设备工具")
    return {device_busy.canonical_serial(value) for value in serials}


def _platform(serial: str) -> str:
    from engine.core.fastbot_ios.tools import is_ios_serial

    return "ios" if is_ios_serial(serial) else "android"


def _new_adapter(serial: str, platform: str, artifact_dir: Path, *, stop_event=None):
    from engine.core.startup.runtime import check_cancel

    check_cancel(stop_event)
    if platform == "android":
        from engine.adb_scan import resolve_adb_binary
        from engine.core.startup.android import AndroidStartupAdapter

        return AndroidStartupAdapter(serial, adb_binary=resolve_adb_binary(), artifact_dir=artifact_dir)
    from engine.core import ios_device
    from engine.core import ios_mjpeg
    from engine.core.drivers.ios_driver import IOSDriver
    from engine.core.fastbot_ios.tools import udid_from_serial
    from engine.core.startup.ios import IOSStartupAdapter

    # Reuse the device registry's existing iproxy/xcodebuild host. This must not
    # call execution_wda_url: the startup task already owns its device lease.
    ios_mjpeg.pause_device(serial)
    check_cancel(stop_event)
    ios_device.driver_pool.drain_for_collection(serial)
    check_cancel(stop_event)
    url = ios_device.wda_url(serial, wait_seconds=ios_device.WDA_RUN_WAIT_SECONDS)
    check_cancel(stop_event)
    driver = IOSDriver(device_id=udid_from_serial(serial), wda_url=url)
    return IOSStartupAdapter(driver, device_serial=serial, artifact_dir=artifact_dir)


class StartupService:
    def __init__(self, root: Optional[Path] = None, *, bridge_factory=CloudBridge, adapter_factory=None):
        self._root = root
        self.bridge_factory = bridge_factory
        self.adapter_factory = adapter_factory
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._write_lock = threading.Lock()
        self._sync_lock = asyncio.Lock()
        self._submitting = asyncio.Lock()
        self._closing = False
        self._session_revision = 0

    @property
    def root(self) -> Path:
        return self._root or config_store.user_data_dir() / "startup_runs"

    def _path(self, run_id: str) -> Path:
        return self.root / str(uuid.UUID(run_id)) / "state.json"

    def _persist(self, entry: Dict[str, Any]) -> None:
        # Entries are constructed explicitly. Never serialize bridge headers,
        # cookies, current login tokens, adapter/driver objects or claim tokens.
        data = deepcopy(entry)
        data.pop("claim_token", None)
        path = self._path(data["run_id"])
        with self._write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)

    async def local_results(self, bridge: CloudBridge):
        # Prefer online authentication; when offline allow only the exact token
        # previously validated for this process, without persisting that token.
        credential = _credential(bridge)
        try:
            owner = await asyncio.to_thread(_identity, bridge)
            self._validated_login = (bridge.upstream, credential, owner)
        except (httpx.ConnectError, httpx.TimeoutException):
            cached = getattr(self, "_validated_login", None)
            if not cached or cached[:2] != (bridge.upstream, credential):
                raise
            owner = cached[2]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                cached = getattr(self, "_validated_login", None)
                if cached and cached[:2] == (bridge.upstream, credential):
                    self._validated_login = None
            raise
        rows = [row for row in self.entries() if row.get("scope") == _scope(bridge.upstream, owner)]
        return {"items": [{**self._task_view(row), "summary": row["report"]["summary"], "report": row["report"]}
                          for row in rows]}

    def entries(self):
        if not self.root.is_dir():
            return []
        rows = []
        for path in self.root.glob("*/state.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                if self._path(row["run_id"]) == path:
                    rows.append(row)
            except (ValueError, KeyError, OSError):
                logger.warning("无法读取本地启动结果 %s", path.name)
        return rows

    def _guard(self, entry, command=None):
        command = command or entry
        return {"client_instance_id": config_store.get_client_instance_id(),
                "attempt": command.get("attempt", entry.get("attempt")),
                "claim_token": command.get("claim_token", entry.get("claim_token", ""))}

    async def _adapter(self, entry, *, stop_event=None):
        # Cancelling to_thread cannot cancel its WDA initialization. Drain it
        # and close the returned adapter before releasing the device lease.
        factory = self.adapter_factory or _new_adapter
        work = asyncio.create_task(asyncio.to_thread(factory, entry["device_serial"],
            entry["config"]["platform"], self._path(entry["run_id"]).parent,
            **({"stop_event": stop_event} if self.adapter_factory is None else {})))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            try:
                adapter = await asyncio.shield(work)
                await adapter.close()
            except Exception:
                logger.exception("启动适配器取消后收尾失败")
            raise

    async def _release_prepared(self, prepared):
        for entry, record, adapter in prepared:
            if entry["run_id"] in self.jobs or record.done_event.is_set():
                continue
            try:
                if adapter is not None:
                    await adapter.close()
            except Exception:
                logger.exception("启动采集适配器收尾失败")
            finally:
                run_registry.registry.complete(record.run_id, entry["status"])

    @staticmethod
    def _command(created: Dict[str, Any]) -> Dict[str, Any]:
        command = created.get("command") or created
        task_id = command.get("task_id") or command.get("id")
        if not task_id or not command.get("attempt") or not command.get("claim_token"):
            raise RuntimeError("平台未提供启动任务的认领凭据，请先升级平台")
        return {**command, "task_id": int(task_id), "id": int(task_id)}

    async def devices(self, bridge: CloudBridge) -> Dict[str, Any]:
        # The cloud remains authoritative for account/instance ownership. A
        # physically attached device owned by another login must not be exposed.
        data = await asyncio.to_thread(bridge.get_json, "/api/devices/")
        items = data.get("items", data.get("devices", [])) if isinstance(data, dict) else data
        local = await asyncio.to_thread(_local_serials)
        instance = config_store.get_client_instance_id()
        caps = capabilities()
        result = []
        for device in items or []:
            serial = str(device.get("serial") or "").strip()
            if device.get("client_instance_id") != instance:
                continue
            platform = _platform(serial)
            reason = None
            if device_busy.canonical_serial(serial) not in local:
                reason = "设备未连接到本机或未授权 USB 调试"
            elif device_busy.is_busy(serial):
                reason = "设备正在执行其他任务"
            elif platform not in caps.get("platforms", ["android", "ios"]):
                reason = "当前客户端未提供该平台的启动采集能力"
            result.append({**device, "platform": platform, "executor_kind": "desktop",
                           "available": reason is None, "unavailable_reason": reason,
                           "capabilities": caps, "startup_capabilities": caps})
        return {"items": result, "capabilities": caps}

    async def _validate_owned(self, bridge, serials):
        data = await asyncio.to_thread(bridge.get_json, "/api/devices/")
        rows = data.get("items", data.get("devices", [])) if isinstance(data, dict) else data
        owned = {device_busy.canonical_serial(d.get("serial")): d for d in rows or []
                 if d.get("client_instance_id") == config_store.get_client_instance_id()}
        local = await asyncio.to_thread(_local_serials)
        for serial in serials:
            key = device_busy.canonical_serial(serial)
            if key not in owned:
                raise ValueError(f"设备不属于当前登录的客户端：{serial}，请先同步设备")
            if key not in local:
                raise ValueError(f"设备未连接到本机或未授权：{serial}")

    def _create_payload(self, entry):
        config = entry["config"]
        return {"kind": "startup", "run_id": entry["run_id"], "package_name": config["package_name"],
                "device_serial": entry["device_serial"], "capture_log": config.get("capture_log", True),
                "duration": 0, "throttle": 0, "platform": config["platform"],
                "client_instance_id": config_store.get_client_instance_id(), "startup_config": config,
                "startup_capabilities": capabilities()}

    async def start(self, bridge: CloudBridge, payload: Dict[str, Any]) -> Dict[str, Any]:
        from engine.core.startup.contract import StartupConfig

        session_revision = self._session_revision
        if self._closing:
            raise ValueError("客户端正在退出，请稍后重新启动测试")
        config = StartupConfig.model_validate(payload).model_dump(mode="json")
        serials = list(dict.fromkeys(config["device_serials"]))
        platforms = {_platform(serial) for serial in serials}
        if len(platforms) != 1 or (config.get("platform") and config["platform"] not in platforms):
            raise ValueError("一次启动测试只能选择同一平台的设备")
        config["platform"] = next(iter(platforms))
        config = StartupConfig.model_validate(config).model_dump(mode="json")
        owner = await asyncio.to_thread(_identity, bridge)
        if self._session_revision != session_revision:
            raise ValueError("登录状态已变化，本次启动测试已取消，请重新提交")
        self._validated_login = (bridge.upstream, _credential(bridge), owner)
        scope = _scope(bridge.upstream, owner)
        request_id = config.get("request_id") or str(uuid.uuid4())
        fingerprint = json.dumps({**config, "request_id": None, "device_serials": sorted(serials)}, sort_keys=True)
        async with self._submitting:
            previous = [row for row in self.entries() if row.get("scope") == scope and row.get("request_id") == request_id]
            if previous:
                if any(row.get("fingerprint") != fingerprint for row in previous):
                    raise ValueError("同一启动请求编号已使用其他配置，请查看原执行后重新操作")
                return {"tasks": [self._task_view(row) for row in previous], "request_id": request_id}
            await self._validate_owned(bridge, serials)
            prepared = []
            try:
                # Reserve the complete batch before any preflight connects to a
                # device. On a partial failure every reservation is released.
                for serial in serials:
                    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"startup:{scope}:{request_id}:{serial}"))
                    record = run_registry.registry.register(LocalRunRecord(
                        kind="startup", target_id=0, run_id=run_id, device_serial=serial))
                    entry = {"run_id": run_id, "task_id": None, "owner_id": owner, "scope": scope,
                             "upstream": bridge.upstream, "request_id": request_id, "fingerprint": fingerprint,
                             "device_serial": serial, "config": {**config, "device_serials": [serial]},
                             "stage": "CREATING", "status": "RUNNING", "sync_state": "pending",
                             "attempt": None, "artifacts": {}, "report": _empty_report(config)}
                    prepared.append((entry, record, None))
                for index, (entry, record, _) in enumerate(prepared):
                    adapter = await self._adapter(entry)
                    prepared[index] = (entry, record, adapter)
                    await adapter.preflight(entry["config"])
                if self._session_revision != session_revision:
                    raise ValueError("登录状态已变化，本次启动测试已取消，请重新提交")
            except BaseException:
                await self._release_prepared(prepared)
                raise
            tasks = []
            try:
                for entry, record, adapter in prepared:
                    try:
                        if self._session_revision != session_revision:
                            raise ValueError("登录状态已变化，本次启动测试已取消")
                        self._persist(entry)
                        created = await asyncio.to_thread(bridge.create_client_fastbot, self._create_payload(entry))
                        command = self._command(created)
                        entry.update({key: command.get(key) for key in
                                      ("task_id", "attempt", "claim_token", "lease_expires_at")})
                        record.target_id = entry["task_id"]
                        if self._session_revision != session_revision:
                            raise ValueError("登录状态已变化，本次启动测试已取消")
                        if command.get("status") in {"COMPLETED", "FAILED", "ABORTED"}:
                            entry.update(stage="FINISHED", status=command["status"], sync_state="synced")
                            self._persist(entry)
                        else:
                            self._launch(entry, record, adapter, dict(bridge.auth_headers))
                        tasks.append(self._task_view(entry))
                    except Exception as exc:
                        entry.update(stage="FINISHED", status="ABORTED", error=str(exc))
                        entry["report"]["summary"].update(status="ABORTED", error="启动结果待同步，未进行设备采样")
                        self._persist(entry)
                        tasks.append(self._task_view(entry))
            finally:
                # Includes failures while writing an aborted entry: never leak
                # later devices from a partially submitted batch.
                await self._release_prepared(prepared)
            return {"tasks": tasks, "request_id": request_id}

    @staticmethod
    def _task_view(entry):
        return {"id": entry.get("task_id"), "task_id": entry.get("task_id"), "run_id": entry["run_id"],
                "device_serial": entry["device_serial"], "package_name": entry["config"]["package_name"],
                "platform": entry["config"]["platform"], "executor_kind": "desktop", "status": entry["status"],
                "progress": entry["report"]["summary"].get("progress"), "sync_state": entry["sync_state"],
                "error": entry.get("error")}

    def _launch(self, entry, record, adapter, auth_headers):
        entry.update(stage="RUNNING", sync_state="pending")
        self._persist(entry)
        job = {"entry": entry, "record": record, "adapter": adapter, "headers": auth_headers,
               "task": None, "lease_until": self._lease_until(entry)}
        self.jobs[entry["run_id"]] = job
        job["task"] = asyncio.create_task(self._run(job), name=f"startup-{entry['task_id']}")

    @staticmethod
    def _lease_until(entry):
        raw = entry.get("lease_expires_at")
        try:
            date = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return time.monotonic() + max(0, date.timestamp() - time.time())
        except (ValueError, TypeError):
            return time.monotonic() + 60

    async def _heartbeat(self, job, bridge):
        entry, record = job["entry"], job["record"]
        # Keep the cloud lease alive during offline trace processing/upload,
        # after the device has safely been released locally.
        while not job.get("stop_heartbeat"):
            LeaseStopEvent(job).is_set()
            try:
                response = await asyncio.to_thread(bridge.progress_startup, entry["task_id"], {
                    **self._guard(entry), "progress": entry["report"]["summary"].get("progress", {}),
                    "partial_report": deepcopy(entry["report"]),
                })
                if response.get("cancel_requested") or response.get("status") == "ABORTED":
                    record.abort_event.set()
                if response.get("lease_expires_at"):
                    entry["lease_expires_at"] = response["lease_expires_at"]
                    job["lease_until"] = self._lease_until(entry)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403, 404, 409}:
                    record.abort_event.set()
            except Exception:
                logger.info("启动进度暂未同步，保留本地结果")
            if time.monotonic() >= job["lease_until"]:
                record.abort_event.set()
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _run(self, job):
        from engine.core.startup.runner import run_startup
        from engine.core.startup.runtime import check_cancel

        entry, record, adapter = job["entry"], job["record"], job["adapter"]
        bridge = self.bridge_factory(upstream=entry["upstream"], auth_headers=job["headers"])
        heartbeat = asyncio.create_task(self._heartbeat(job, bridge))
        stop_event = LeaseStopEvent(job)
        async def progress(report):
            entry["report"] = deepcopy(report)
            self._persist(entry)
        try:
            check_cancel(stop_event)
            # Ownership and connection may have changed after submitting.
            await self._validate_owned(bridge, [entry["device_serial"]])
            check_cancel(stop_event)
            if adapter is None:
                # A Web claim already has a live lease. Heartbeats must begin
                # before WDA cold initialization, which can take over a minute.
                adapter = await self._adapter(entry, stop_event=stop_event)
                job["adapter"] = adapter
            # WDA cold initialization can outlive a cloud lease. Never launch
            # the target app with a stale claim after that preflight wait.
            check_cancel(stop_event)
            report = await run_startup(adapter, entry["config"], stop_event=stop_event, progress=progress)
            entry["report"] = deepcopy(report)
            status = str(report.get("summary", {}).get("status") or "FAILED").upper()
            entry["status"] = "ABORTED" if record.abort_event.is_set() else status
            if entry["status"] not in {"COMPLETED", "FAILED", "ABORTED"}:
                entry["status"] = "FAILED"
        except BaseException as exc:
            entry["status"] = "ABORTED" if record.abort_event.is_set() or isinstance(exc, asyncio.CancelledError) else "FAILED"
            entry["error"] = str(exc) or "客户端退出，启动测试已中断"
            entry["report"]["summary"].update(status=entry["status"], error=entry["error"])
        finally:
            # Release the device only after outstanding WDA/ADB work is done.
            try:
                if adapter is not None:
                    await adapter.close()
            except Exception:
                logger.exception("启动采集适配器收尾失败")
            entry.update(stage="FINISHED", sync_state="pending")
            entry["report"]["summary"]["status"] = entry["status"]
            try:
                self._persist(entry)
            except Exception:
                logger.exception("启动结果落盘失败，尝试直接同步已有结果")
            finally:
                run_registry.registry.complete(record.run_id, entry["status"])
            try:
                await self._sync_entry(entry, bridge, recover=False)
            except Exception:
                logger.info("启动结果暂未同步，网络恢复后重试")
            finally:
                job["stop_heartbeat"] = True
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                bridge.close()
                self.jobs.pop(entry["run_id"], None)

    async def _analyze_traces(self, entry):
        from engine.core.startup_trace_analysis import analyze_startup_trace

        root = self._path(entry["run_id"]).parent.resolve()
        for artifact in entry["report"].get("trace_artifacts", []):
            relative = artifact.get("path")
            if (not isinstance(relative, str) or not relative.startswith("traces/")
                    or not relative.endswith(".perfetto-trace")
                    or artifact.get("analysis_status") not in {None, "PENDING"}):
                continue
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                artifact.update(analysis_status="FAILED", analysis_error="Trace 路径无效")
                continue
            try:
                analysis = await asyncio.to_thread(analyze_startup_trace, path,
                    entry["config"]["package_name"], platform_name=entry["config"]["platform"])
            except Exception as exc:
                analysis = {"analysis_status": "FAILED", "reason": f"原轮次 Trace 分析失败：{exc}"}
            artifact.update(analysis_status=analysis["analysis_status"],
                            analysis_level=analysis.get("analysis_level", "startup_evidence"),
                            analysis=analysis)
            if analysis.get("reason"):
                artifact["analysis_error" if analysis["analysis_status"] in {"FAILED", "TOOL_MISSING", "TRACE_MISSING"}
                         else "analysis_note"] = analysis["reason"]
            self._persist(entry)

    async def accept_command(self, bridge: CloudBridge, command: Dict[str, Any], owner: str):
        session_revision = self._session_revision
        command = self._command(command)
        self._validated_login = (bridge.upstream, _credential(bridge), owner)
        run_id = str(uuid.UUID(command["run_id"]))
        if run_id in self.jobs:
            return
        if self._path(run_id).exists():
            # Duplicate delivery or recovered process: never run it again.
            await self.recover(bridge, owner)
            return
        config = deepcopy(command.get("config") or command.get("startup_config") or {})
        serial = str(command.get("device_serial") or (config.get("device_serials") or [""])[0])
        config.update(device_serials=[serial], platform=config.get("platform") or _platform(serial))
        entry = {"run_id": run_id, "task_id": command["task_id"], "attempt": command["attempt"],
                 "claim_token": command["claim_token"], "lease_expires_at": command.get("lease_expires_at"),
                 "owner_id": owner, "scope": _scope(bridge.upstream, owner), "upstream": bridge.upstream,
                 "device_serial": serial, "config": config, "stage": "CREATING", "status": "RUNNING",
                 "sync_state": "pending", "artifacts": {}, "report": _empty_report(config)}
        self._persist(entry)
        record = None
        try:
            await self._validate_owned(bridge, [serial])
            if self._session_revision != session_revision:
                raise ValueError("登录状态已变化，派发任务已取消")
            if command.get("cancel_requested"):
                raise ValueError("任务在采集前已取消")
            record = run_registry.registry.register(LocalRunRecord(kind="startup", target_id=command["task_id"],
                         run_id=run_id, device_serial=serial))
            self._launch(entry, record, None, dict(bridge.auth_headers))
        except Exception as exc:
            entry.update(stage="FINISHED", status="ABORTED" if command.get("cancel_requested") else "FAILED", error=str(exc))
            entry["report"]["summary"].update(status=entry["status"], error=str(exc))
            try:
                self._persist(entry)
            finally:
                if record is not None:
                    run_registry.registry.complete(run_id, entry["status"])
            await self._sync_entry(entry, bridge, recover=False)

    async def _sync_entry(self, entry, bridge, *, recover=True):
        if entry.get("sync_state") in {"synced", "superseded"}:
            return
        if not entry.get("task_id"):
            command = self._command(await asyncio.to_thread(bridge.create_client_fastbot, self._create_payload(entry)))
            entry.update({key: command[key] for key in ("task_id", "attempt", "claim_token")})
        if recover:
            response = await asyncio.to_thread(bridge.recover_startup, entry["task_id"], {
                **self._guard(entry), "run_id": entry["run_id"], "local_status": entry["status"]})
            if response.get("claim_token"):
                entry["claim_token"] = response["claim_token"]
            if response.get("attempt"):
                entry["attempt"] = response["attempt"]
            if response.get("status") == "ABORTED":
                entry["status"] = "ABORTED"
                entry["report"]["summary"]["status"] = "ABORTED"
        await self._analyze_traces(entry)
        await self._upload_artifacts(entry, bridge)
        response = await asyncio.to_thread(bridge.finish_startup, entry["task_id"], {
            **self._guard(entry), "status": entry["status"], "report": entry["report"]})
        entry.update(sync_state="synced", synced_at=datetime.now(timezone.utc).isoformat())
        if response.get("status"):
            entry["status"] = response["status"]
            entry["report"]["summary"]["status"] = response["status"]
        self._persist(entry)

    async def _upload_artifacts(self, entry, bridge):
        root = self._path(entry["run_id"]).parent
        for subdir in ("logs", "screenshots", "traces"):
            base = root / subdir
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    continue
                relative = path.relative_to(root).as_posix()
                if path.stat().st_size > MAX_ARTIFACT_BYTES:
                    entry["report"]["summary"].setdefault("artifact_errors", []).append(
                        {"path": relative, "error": "产物超过上传上限，保留在本机"})
                    continue
                if relative in entry["artifacts"]:
                    continue
                result = await asyncio.to_thread(bridge.upload_client_fastbot_artifact, entry["task_id"],
                                                 str(path), relative, guard=self._guard(entry))
                canonical = result.get("path") or result.get("artifact_path")
                if not canonical:
                    raise RuntimeError("平台未返回产物访问路径")
                entry["artifacts"][relative] = canonical
                entry["report"] = _replace_paths(entry["report"], relative, canonical)
                self._persist(entry)

    async def recover(self, bridge: CloudBridge, owner: Optional[str] = None):
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            owner = owner or await asyncio.to_thread(_identity, bridge)
            for entry in self.entries():
                if entry.get("scope") != _scope(bridge.upstream, owner) or entry["run_id"] in self.jobs:
                    continue
                if entry.get("sync_state") in {"synced", "superseded"}:
                    continue
                if entry.get("stage") != "FINISHED":
                    entry.update(stage="FINISHED", status="ABORTED", error="客户端重启，本轮启动采集已中断")
                    entry["report"]["summary"].update(status="ABORTED", error=entry["error"])
                    self._persist(entry)
                try:
                    await self._sync_entry(entry, bridge)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {403, 404, 409}:
                        entry.update(sync_state="superseded", sync_error="任务归属或执行轮次已变化，保留本地证据")
                        self._persist(entry)
                    elif exc.response.status_code == 401:
                        return
                except Exception:
                    logger.info("启动结果补传暂不可用，下轮重试")

    def cancel(self, task_id: int) -> bool:
        for job in self.jobs.values():
            if job["entry"]["task_id"] == task_id:
                job["record"].abort_event.set()
                return True
        return False

    def cancel_authenticated_local(self, bridge: CloudBridge, task_id: int) -> bool:
        """Offline stop is authorized by this process's validated login only."""
        cached = getattr(self, "_validated_login", None)
        credential = _credential(bridge)
        if not credential or not cached or cached[:2] != (bridge.upstream, credential):
            return False
        scope = _scope(bridge.upstream, cached[2])
        for job in self.jobs.values():
            entry = job["entry"]
            if entry["task_id"] == task_id and entry.get("scope") == scope:
                job["record"].abort_event.set()
                return True
        return False

    def cancel_session(self):
        self._session_revision += 1
        self._validated_login = None
        for job in list(self.jobs.values()):
            job["record"].abort_event.set()

    async def shutdown(self, timeout=20.0):
        self._closing = True
        self.cancel_session()
        tasks = [job["task"] for job in self.jobs.values() if job.get("task")]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)


def _replace_paths(value, relative, canonical):
    if isinstance(value, dict):
        return {key: _replace_paths(item, relative, canonical) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, relative, canonical) for item in value]
    return canonical if value == relative else value


service = StartupService()


async def _worker_loop():
    while True:
        try:
            token = presence.get_token()
            upstream = config_store.get_upstream()
            if token and upstream:
                with service.bridge_factory(upstream=upstream, auth_headers={"Authorization": f"Bearer {token}"}) as bridge:
                    owner = await asyncio.to_thread(_identity, bridge)
                    if presence.get_token() != token or config_store.get_upstream() != upstream:
                        continue
                    await service.recover(bridge, owner)
                    if (not service._closing and presence.get_token() == token
                            and config_store.get_upstream() == upstream):
                        response = await asyncio.to_thread(bridge.claim_startup, {
                            "client_instance_id": config_store.get_client_instance_id(), "capabilities": capabilities(),
                            "active_run_ids": list(service.jobs)})
                        if response.get("command") and presence.get_token() == token and config_store.get_upstream() == upstream:
                            await service.accept_command(bridge, response["command"], owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("启动命令暂不可用，稍后重试", exc_info=True)
        await asyncio.sleep(3.0)


def start_worker():
    service._closing = False
    return asyncio.create_task(_worker_loop(), name="startup-command-worker")
