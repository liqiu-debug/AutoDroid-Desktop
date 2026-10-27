# 来源：语义对齐 AutoDroid backend/scheduler_service.py（触发器构造）+ backend/api/tasks.py
#       _run_scheduled_scenario（UI 场景分支：预检过滤 + 场景批量执行）。
"""定时任务本地触发（P3-1）：云端 ScheduledTask 定义共享，桌面引擎本地 APScheduler 调度。

契约（与平台 GET /api/client-tasks 对齐，backend/api/tasks.py list_client_tasks）：
- 任务定义同源：CRUD 照旧走云端 /api/tasks/（Web/客户端同源透传），本模块只消费
  GET /api/client-tasks 的 {total, items}（条目含 ScheduledTask 全字段）；
- 本地仅调度「创建者=本人 或 device_serials 命中本人设备」的 is_active 任务中
  task_type=ui（strategy_config._task_type 缺省 ui）且带 scenario_id 的条目；
- 触发语义对齐 scheduler_service.create_trigger：DAILY/WEEKLY/INTERVAL/ONCE；
- 触发即本地执行（scenario_service.start_background_scenario_run → run_scenario_local
  → client-runs 上报，与手动执行同链路），逐设备 precheck 过滤明显不可执行设备
  （对齐 tasks._run_scheduled_scenario 的 UI 分支）；
- 本地调度只在引擎存活时触发；引擎启动/登录后重新拉取重排（对齐
  main._restore_scheduled_tasks 重启重排语义）。

安全红线：token 只经 presence 内存取用，绝不落盘。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from engine.core.cloud_bridge import CloudBridge

logger = logging.getLogger(__name__)

# 星期映射与平台 scheduler_service.DAY_MAP 一致（前端数字 → APScheduler 字符串）
DAY_MAP = {
    0: "mon",
    1: "tue",
    2: "wed",
    3: "thu",
    4: "fri",
    5: "sat",
    6: "sun",
}

LOCAL_TASK_TYPES = {"ui"}
JOB_ID_PREFIX = "client_task_"
REFRESH_JOB_ID = "client_tasks_refresh"
DEFAULT_REFRESH_SECONDS = 300.0


def refresh_interval_seconds() -> float:
    raw = str(os.environ.get("AUTODROID_TASK_REFRESH_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_REFRESH_SECONDS
    except ValueError:
        value = DEFAULT_REFRESH_SECONDS
    return max(30.0, value)


# --------------------------------------------------------------------------- #
#  任务定义归一（平台 ClientScheduledTaskRead 条目 → 本地调度输入）
# --------------------------------------------------------------------------- #

def task_type_of(task: Dict[str, Any]) -> str:
    config = task.get("strategy_config") if isinstance(task.get("strategy_config"), dict) else {}
    return str(config.get("_task_type") or "ui").strip().lower()


def device_serials_of(task: Dict[str, Any]) -> List[str]:
    """device_serials 列表优先；空/缺失时回退 device_serial CSV（历史兼容形态）。"""
    raw = task.get("device_serials")
    if isinstance(raw, list):
        parsed = [str(item).strip() for item in raw if str(item).strip()]
        if parsed:
            return parsed
    csv_raw = task.get("device_serial")
    if isinstance(csv_raw, str):
        return [item.strip() for item in csv_raw.split(",") if item.strip()]
    return []


def should_schedule_locally(task: Dict[str, Any]) -> Tuple[bool, str]:
    """本地是否接管调度：ui 任务 + scenario_id + is_active + 合法策略。"""
    if not task.get("id"):
        return False, "缺少任务 id"
    if not task.get("is_active", True):
        return False, "任务未启用"
    if task_type_of(task) not in LOCAL_TASK_TYPES:
        return False, f"任务类型 {task_type_of(task)} 不由客户端本地调度"
    if task.get("scenario_id") is None:
        return False, "ui 任务缺少 scenario_id"
    strategy = str(task.get("strategy") or "").strip().upper()
    if strategy not in ("DAILY", "WEEKLY", "INTERVAL", "ONCE"):
        return False, f"未知策略: {strategy}"
    return True, ""


def build_trigger(strategy: str, config: Dict[str, Any]):
    """触发器构造（逐字段对齐 scheduler_service.create_trigger）。"""
    strategy = str(strategy or "").strip().upper()
    config = config if isinstance(config, dict) else {}
    if strategy == "DAILY":
        return CronTrigger(hour=config.get("hour", 0), minute=config.get("minute", 0))
    if strategy == "WEEKLY":
        days = config.get("days", [])
        day_of_week = ",".join(DAY_MAP.get(d, "mon") for d in days)
        return CronTrigger(
            day_of_week=day_of_week or "mon",
            hour=config.get("hour", 0),
            minute=config.get("minute", 0),
        )
    if strategy == "INTERVAL":
        interval_value = config.get("interval_value", 30)
        if config.get("interval_unit", "minutes") == "hours":
            return IntervalTrigger(hours=interval_value)
        return IntervalTrigger(minutes=interval_value)
    if strategy == "ONCE":
        run_date_str = config.get("run_date", "")
        if not run_date_str:
            raise ValueError("ONCE 策略需要 run_date 参数")
        return DateTrigger(run_date=datetime.fromisoformat(run_date_str))
    raise ValueError(f"未知策略: {strategy}")


def _fingerprint(task: Dict[str, Any]) -> str:
    """任务定义指纹（变化即重建 job）。"""
    payload = {
        "scenario_id": task.get("scenario_id"),
        "strategy": task.get("strategy"),
        "strategy_config": task.get("strategy_config"),
        "device_serials": device_serials_of(task),
        "is_active": task.get("is_active", True),
        "enable_notification": task.get("enable_notification", True),
        "updated_at": str(task.get("updated_at") or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _once_is_past(task: Dict[str, Any]) -> bool:
    config = task.get("strategy_config") if isinstance(task.get("strategy_config"), dict) else {}
    run_date_str = str(config.get("run_date") or "")
    if not run_date_str:
        return False
    try:
        run_date = datetime.fromisoformat(run_date_str)
        # T3：带时区 run_date 与 naive now 直接比较会抛 TypeError——按 run_date
        # 自身时区取 now 对齐（aware/naive 各比各的），坏值一律视为未过期。
        if run_date.tzinfo is not None:
            return run_date <= datetime.now(tz=run_date.tzinfo)
        return run_date <= datetime.now()
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
#  触发执行（对齐 tasks._run_scheduled_scenario 的 UI 场景分支）
# --------------------------------------------------------------------------- #

def local_device_serials() -> Optional[set]:
    """本机 adb 在线设备 serial 集合（None=清单未知，调用方不过滤）。"""
    try:
        from engine.adb_scan import local_serials
    except Exception:  # pragma: no cover - 导入面兜底
        return None
    try:
        return local_serials()
    except Exception:
        logger.warning("本机 adb 设备清单获取失败（按未知处理，不过滤）", exc_info=True)
        return None


def _is_device_on_this_machine(serial: str, local: Optional[set]) -> bool:
    """T4：设备是否在本机（触发前过滤，避免多台桌面各自产生失败 run）。

    - 清单未知（adb 缺失/失败）→ fail-open 不过滤；
    - ios: 前缀设备不在 adb 清单（本机 iOS 枚举随后续 iOS 链路提供）→ 不据此过滤。
    """
    if local is None:
        return True
    if str(serial).startswith("ios:"):
        return True
    return serial in local


def execute_task(task: Dict[str, Any], auth_headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """定时触发本地执行：逐设备 precheck 过滤后本地跑场景 + client-runs 上报。

    返回实际启动的 {device_serial, run_id, batch_id} 列表。
    """
    from engine.core import scenario_service

    strategy_config = task.get("strategy_config") if isinstance(task.get("strategy_config"), dict) else {}
    task_id = task.get("id")
    task_name = str(task.get("name") or f"task-{task_id}")
    scenario_id = task.get("scenario_id")
    serials = device_serials_of(task)
    env_id = strategy_config.get("env_id")
    try:
        env_id = int(env_id) if env_id is not None else None
    except (TypeError, ValueError):
        env_id = None
    notify = bool(task.get("enable_notification", True))

    if task_type_of(task) not in LOCAL_TASK_TYPES:
        logger.info("[定时任务] 任务 #%s 类型 %s 不由客户端本地触发", task_id, task_type_of(task))
        return []
    if scenario_id is None:
        logger.error("[定时任务] 任务 #%s 缺少 scenario_id，跳过", task_id)
        return []
    if not serials:
        logger.error("[定时任务] 任务 #%s 未绑定设备，跳过", task_id)
        return []

    # T4：本机 adb 清单过滤——不在本机的设备直接跳过（debug 日志，不建 run）。
    local = local_device_serials()
    bridge = CloudBridge(auth_headers=auth_headers)
    run_name = f"定时任务: {task_name}"
    started: List[Dict[str, Any]] = []
    for serial in serials:
        if not _is_device_on_this_machine(serial, local):
            logger.debug(
                "[定时任务] 任务 #%s 设备 %s 不在本机 adb 清单，跳过（不建 run）", task_id, serial
            )
            continue
        try:
            precheck = scenario_service.precheck_scenario_for_device(
                bridge, int(scenario_id), serial, env_id
            )
        except Exception as exc:
            logger.warning("[定时任务] 任务 #%s 设备 %s 预检异常（跳过）: %s", task_id, serial, exc)
            continue
        if not precheck.get("ok"):
            reason = scenario_service.summarize_precheck_failure(precheck)
            logger.warning(
                "[定时任务] 任务 #%s 设备 %s 预检未通过（跳过）: %s", task_id, serial, reason
            )
            continue
        run_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        scenario_service.start_background_scenario_run(
            scenario_id=int(scenario_id),
            env_id=env_id,
            device_serial=serial,
            run_id=run_id,
            batch_id=batch_id,
            auth_headers=auth_headers,
            notify=notify,
            run_name=run_name,
        )
        started.append({"device_serial": serial, "run_id": run_id, "batch_id": batch_id})
        logger.info(
            "[定时任务] 任务 #%s 本地触发: scenario=%s device=%s run=%s",
            task_id,
            scenario_id,
            serial,
            run_id,
        )
    return started


# --------------------------------------------------------------------------- #
#  调度器（本地 APScheduler）
# --------------------------------------------------------------------------- #

class LocalTaskScheduler:
    """桌面引擎本地定时任务调度（进程内单例；只在引擎存活时触发）。"""

    def __init__(
        self,
        *,
        runner: Optional[Callable[[Dict[str, Any]], Any]] = None,
        scheduler: Optional[BackgroundScheduler] = None,
    ) -> None:
        self._scheduler = scheduler or BackgroundScheduler()
        self._lock = threading.Lock()
        self._fingerprints: Dict[int, str] = {}
        self._runner = runner or self._default_runner
        self._started = False

    # -- 生命周期 --

    def start(self, refresh_seconds: Optional[float] = None) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._scheduler.start()
            interval = refresh_seconds if refresh_seconds is not None else refresh_interval_seconds()
            self._scheduler.add_job(
                self.refresh_now,
                trigger=IntervalTrigger(seconds=interval),
                id=REFRESH_JOB_ID,
                name="client-tasks-refresh",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        logger.info("本地定时任务调度器已启动（刷新间隔 %.0fs）", refresh_seconds or refresh_interval_seconds())

    def shutdown(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._fingerprints.clear()
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            logger.warning("本地定时任务调度器关闭失败", exc_info=True)

    def scheduled_task_ids(self) -> List[int]:
        with self._lock:
            return sorted(self._fingerprints)

    # -- 同步（拉取 → 重排） --

    def sync_tasks(self, tasks: List[Dict[str, Any]]) -> Dict[str, int]:
        """以云端任务集合重排本地 jobs（新增/更新/移除；对齐重启重排语义）。

        T3：单任务解析/构造异常按任务隔离（跳过该任务），不炸整批 sync。
        """
        desired: Dict[int, Dict[str, Any]] = {}
        skipped = 0
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            try:
                ok, reason = should_schedule_locally(task)
                if not ok:
                    skipped += 1
                    logger.debug("任务 %s 不进本地调度: %s", task.get("id"), reason)
                    continue
                if str(task.get("strategy") or "").upper() == "ONCE" and _once_is_past(task):
                    skipped += 1
                    logger.info("[定时任务] ONCE 任务 #%s 已过期，不进本地调度", task.get("id"))
                    continue
                desired[int(task["id"])] = task
            except Exception as exc:
                # 单任务坏数据（非法 id / 策略解析异常等）不影响其余任务入列
                skipped += 1
                logger.warning(
                    "[定时任务] 任务 %s 调度准备失败（隔离跳过，不影响其余任务）: %s",
                    task.get("id"),
                    exc,
                )

        added = 0
        updated = 0
        removed = 0
        with self._lock:
            stale_ids = [tid for tid in self._fingerprints if tid not in desired]
            for task_id in stale_ids:
                self._fingerprints.pop(task_id, None)
                try:
                    self._scheduler.remove_job(JOB_ID_PREFIX + str(task_id))
                except Exception:
                    logger.debug("移除本地任务 job 失败（可能未注册）: %s", task_id)
                removed += 1
            for task_id, task in desired.items():
                fingerprint = _fingerprint(task)
                if self._fingerprints.get(task_id) == fingerprint:
                    continue
                is_update = task_id in self._fingerprints
                try:
                    trigger = build_trigger(
                        str(task.get("strategy") or ""), task.get("strategy_config") or {}
                    )
                    self._scheduler.add_job(
                        self._runner,
                        trigger=trigger,
                        id=JOB_ID_PREFIX + str(task_id),
                        name=f"定时任务#{task_id}",
                        replace_existing=True,
                        kwargs={"task": dict(task)},
                    )
                except Exception as exc:
                    skipped += 1
                    logger.warning("[定时任务] 任务 #%s 触发器构造/注册失败（跳过）: %s", task_id, exc)
                    continue
                self._fingerprints[task_id] = fingerprint
                if is_update:
                    updated += 1
                else:
                    added += 1
        result = {"scheduled": len(desired), "added": added, "updated": updated,
                  "removed": removed, "skipped": skipped}
        logger.info("[定时任务] 本地调度同步: %s", result)
        return result

    # -- 拉取（登录后/启动后/周期） --

    def refresh_now(self) -> Dict[str, int]:
        """拉取云端任务并重排（无 token/上游时静默跳过）。"""
        from engine import config_store, presence

        token = presence.get_token()
        upstream = config_store.get_upstream()
        if not token or not upstream:
            return {"scheduled": len(self.scheduled_task_ids()), "added": 0,
                    "updated": 0, "removed": 0, "skipped": 0, "refreshed": 0}
        bridge = CloudBridge(auth_headers={"Authorization": f"Bearer {token}"})
        try:
            items = bridge.fetch_client_tasks()
        except Exception as exc:
            logger.warning("[定时任务] 拉取 /api/client-tasks 失败（保留现有调度）: %s", exc)
            return {"scheduled": len(self.scheduled_task_ids()), "added": 0,
                    "updated": 0, "removed": 0, "skipped": 0, "refreshed": 0}
        result = self.sync_tasks(items)
        result["refreshed"] = 1
        return result

    def refresh_async(self) -> None:
        """后台线程刷新（登录响应路径调用，不阻塞请求）。"""
        threading.Thread(
            target=self.refresh_now, name="client-tasks-refresh", daemon=True
        ).start()

    # -- 触发 --

    def _default_runner(self, task: Dict[str, Any]) -> None:
        from engine import presence

        token = presence.get_token()
        if not token:
            logger.warning("[定时任务] 未登录（无 token），任务 #%s 触发被跳过", task.get("id"))
            return
        try:
            execute_task(task, {"Authorization": f"Bearer {token}"})
        except Exception:
            logger.exception("[定时任务] 任务 #%s 本地触发异常", task.get("id"))


scheduler = LocalTaskScheduler()


def start_scheduler(refresh_seconds: Optional[float] = None) -> None:
    scheduler.start(refresh_seconds=refresh_seconds)


def shutdown_scheduler() -> None:
    scheduler.shutdown()


def reset_for_tests() -> None:
    """测试隔离：重置单例（重建调度器实例）。"""
    global scheduler
    try:
        scheduler.shutdown()
    except Exception:
        pass
    scheduler = LocalTaskScheduler()
