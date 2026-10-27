# 来源：语义对齐 AutoDroid backend/run_control.py（RunRecord/RunRegistry）。
"""本地执行 run 登记（桌面引擎无 DB，进程内 registry）。

用途：
- GET  /api/runs/active   前端每 3s 轮询（run_start 后），本 registry 应答，不再反代云端；
- POST /api/runs/cancel   置 abort_event 并等待 runner 退出（wait_for_exit）；
- run_case_local / 场景批量执行的生命周期登记（register → complete）。

字段形状对齐 backend/run_control.py RunRecord.to_dict（execution_id 本地无 DB 恒可空）。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

QUEUED_STATUS = "QUEUED"
RUNNING_STATUS = "RUNNING"
ABORTED_STATUS = "ABORTED"
RUNNING_STATUSES = {QUEUED_STATUS, RUNNING_STATUS}

# cancel 后等待 runner 自然退出的默认上限（秒）；超时仍返回已请求中止。
DEFAULT_CANCEL_WAIT_SECONDS = 15.0


class LocalRunRecord:
    """一次本地执行的登记项（case 或 scenario，单设备）。"""

    def __init__(
        self,
        *,
        kind: str,
        target_id: int,
        run_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        device_serial: Optional[str] = None,
        execution_id: Optional[int] = None,
        status: str = RUNNING_STATUS,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.run_id = str(run_id or uuid.uuid4())
        self.kind = str(kind or "case").strip().lower()
        self.target_id = int(target_id)
        self.batch_id = str(batch_id) if batch_id else None
        self.device_serial = str(device_serial) if device_serial else None
        self.execution_id = int(execution_id) if execution_id is not None else None
        self.status = str(status or RUNNING_STATUS).upper()
        self.started_at = datetime.now()
        self.cancel_requested_at: Optional[datetime] = None
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self.abort_event = threading.Event()
        self.done_event = threading.Event()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "target_id": self.target_id,
            "batch_id": self.batch_id,
            "device_serial": self.device_serial,
            "execution_id": self.execution_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "cancel_requested_at": (
                self.cancel_requested_at.isoformat() if self.cancel_requested_at else None
            ),
            "metadata": dict(self.metadata or {}),
        }


def _string_set(values: Optional[Iterable[Any]]) -> set:
    return {str(value).strip() for value in values or [] if str(value).strip()}


def _int_set(values: Optional[Iterable[Any]]) -> set:
    result = set()
    for value in values or []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


class LocalRunRegistry:
    """进程内 run 登记（对齐 backend.run_control.RunRegistry 的匹配/取消语义）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, LocalRunRecord] = {}

    def register(self, record: LocalRunRecord) -> LocalRunRecord:
        with self._lock:
            self._runs[record.run_id] = record
        return record

    def get(self, run_id: Optional[str]) -> Optional[LocalRunRecord]:
        if not run_id:
            return None
        with self._lock:
            return self._runs.get(str(run_id))

    def update_execution_id(self, run_id: Optional[str], execution_id: Optional[Any]) -> None:
        """回填云端 execution_id（S1）。

        create_cloud_run 拿到云端 id、场景批量 preset_run_key 预创建后调用；
        GET /api/runs/active 的 items[].execution_id 依赖此回填（前端 terminate
        载荷 {batch_id, execution_ids, device_serials} 的 AND 匹配需要真实 id）。
        """
        if not run_id or execution_id is None:
            return
        try:
            normalized = int(execution_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            record = self._runs.get(str(run_id))
        if record is not None:
            record.execution_id = normalized

    def active(self, *, kind: Optional[str] = None, target_id: Optional[int] = None) -> List[LocalRunRecord]:
        normalized_kind = str(kind or "").strip().lower() if kind else None
        with self._lock:
            records = list(self._runs.values())
        return [
            record
            for record in records
            if (not normalized_kind or record.kind == normalized_kind)
            and (target_id is None or record.target_id == int(target_id))
            and record.status in RUNNING_STATUSES
        ]

    def _match(
        self,
        *,
        kind: Optional[str],
        target_id: Optional[int],
        batch_id: Optional[str],
        run_ids: Optional[Sequence[Any]],
        execution_ids: Optional[Sequence[Any]],
        device_serials: Optional[Sequence[Any]],
    ) -> List[LocalRunRecord]:
        run_id_set = _string_set(run_ids)
        execution_id_set = _int_set(execution_ids)
        serial_set = _string_set(device_serials)
        normalized_kind = str(kind or "").strip().lower() if kind else None
        normalized_batch = str(batch_id or "").strip() if batch_id else None

        with self._lock:
            records = list(self._runs.values())

        matched: List[LocalRunRecord] = []
        for record in records:
            if normalized_kind and record.kind != normalized_kind:
                continue
            if target_id is not None and record.target_id != int(target_id):
                continue
            if normalized_batch and (record.batch_id or "") != normalized_batch:
                continue
            if run_id_set and record.run_id not in run_id_set:
                continue
            # execution_id 生命周期（S1）：云端 id 于 create_cloud_run/preset_run_key 后回填，
            # 登记瞬间可能尚未拿到（或预创建失败恒缺失）。执行 id 缺失的记录不被
            # execution_ids 过滤器排除（其余过滤器仍需命中），避免前端
            # {batch_id, execution_ids, device_serials} AND 匹配把未回填记录漏掉、abort 静默失效。
            if (
                execution_id_set
                and record.execution_id is not None
                and record.execution_id not in execution_id_set
            ):
                continue
            if serial_set and (record.device_serial or "") not in serial_set:
                continue
            if record.status not in RUNNING_STATUSES:
                continue
            matched.append(record)
        return matched

    def cancel(
        self,
        *,
        kind: Optional[str] = None,
        target_id: Optional[int] = None,
        batch_id: Optional[str] = None,
        run_ids: Optional[Sequence[Any]] = None,
        execution_ids: Optional[Sequence[Any]] = None,
        device_serials: Optional[Sequence[Any]] = None,
    ) -> List[LocalRunRecord]:
        """命中并置 abort_event（状态 ABORTED 立即从 active 消失，与平台口径一致）。"""
        records = self._match(
            kind=kind,
            target_id=target_id,
            batch_id=batch_id,
            run_ids=run_ids,
            execution_ids=execution_ids,
            device_serials=device_serials,
        )
        now = datetime.now()
        for record in records:
            record.status = ABORTED_STATUS
            record.cancel_requested_at = now
            record.abort_event.set()
        return records

    def wait_for_exit(self, records: Sequence[LocalRunRecord], timeout: float) -> bool:
        """cancel 后等 runner 退出（done_event 由 complete 置位）；超时返回 False。"""
        deadline = datetime.now().timestamp() + max(0.0, float(timeout))
        for record in records:
            remaining = deadline - datetime.now().timestamp()
            if remaining <= 0:
                return False
            if not record.done_event.wait(timeout=remaining):
                return False
        return True

    def complete(self, run_id: Optional[str], status: Optional[str] = None) -> None:
        if not run_id:
            return
        with self._lock:
            record = self._runs.pop(str(run_id), None)
        if record is not None:
            if status:
                record.status = str(status).upper()
            record.done_event.set()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._runs.clear()


registry = LocalRunRegistry()
