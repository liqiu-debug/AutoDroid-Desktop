# 来源：无（桌面端新增）。对接云端数据面：步骤/变量/设置拉取、模板上传/缓存、client-runs 上报。
"""云端数据面桥接（同步 httpx，供线程池中的录制/执行服务调用）。

契约假设（与平台端 /api/client-runs 对齐，字段名对齐 backend/api/client_runs.py 的
ClientRun* 模型，pipeline 复用 _persist_case_result_and_build_case_report）：
- POST /api/client-runs/                 {run_id, kind, case_id, scenario_id, device_serial,
                                          platform, run_source, started_at(ISO datetime),
                                          name, env_id}
  → {id, run_id, ...}
- POST /api/client-runs/{id}/steps       {case_id, name, alias, success, exported_variables,
                                          steps: [{order, action, status, duration, attempts,
                                          error, error_code, screenshot_base64, output}]}
- POST /api/client-runs/{id}/finish      {status: PASS|FAIL|WARNING|ABORTED|ERROR, aborted,
                                          duration, error, notify, exported_variables}
- POST /api/client-templates             multipart file → {image_path: "static/images/element_<hash>.png"}

安全红线：本模块不落盘任何凭据；auth 头只在内存中随请求发出。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from engine import config_store

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)


def template_cache_dir() -> Path:
    """模板图本地缓存目录（userData/templates）。"""
    path = config_store.user_data_dir() / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


class CloudBridge:
    """一次性调用的云端访问器（非线程安全；每次调用/任务各自构造）。"""

    def __init__(
        self,
        upstream: Optional[str] = None,
        auth_headers: Optional[Dict[str, str]] = None,
        timeout: httpx.Timeout = _UPLOAD_TIMEOUT,
    ) -> None:
        self.upstream = (upstream or config_store.get_upstream() or "").rstrip("/")
        self.auth_headers = dict(auth_headers or {})
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.upstream)

    def _require_upstream(self) -> str:
        if not self.upstream:
            raise RuntimeError("未配置上游服务器（请先在登录页填写服务器地址）")
        return self.upstream

    def _url(self, path: str) -> str:
        base = self._require_upstream()
        return f"{base}{path if path.startswith('/') else '/' + path}"

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=False)

    # ---------- 通用 ----------

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self._client() as client:
            resp = client.get(self._url(path), params=params, headers=self.auth_headers)
            resp.raise_for_status()
            return resp.json()

    def post_json(self, path: str, payload: Dict[str, Any]) -> Any:
        with self._client() as client:
            resp = client.post(self._url(path), json=payload, headers=self.auth_headers)
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()

    def post_multipart(self, path: str, files: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> Any:
        with self._client() as client:
            resp = client.post(
                self._url(path), files=files, data=data or {}, headers=self.auth_headers
            )
            resp.raise_for_status()
            return resp.json()

    # ---------- 用例数据（本地执行前置） ----------

    def fetch_case(self, case_id: int) -> Dict[str, Any]:
        """GET /api/cases/{id} → 用例（name/variables/...）。"""
        return self.get_json(f"/api/cases/{int(case_id)}")

    def fetch_case_steps(self, case_id: int) -> List[Dict[str, Any]]:
        """GET /api/cases/{id}/steps → 标准步骤行（TestCaseStepRead 数组）。"""
        data = self.get_json(f"/api/cases/{int(case_id)}/steps")
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def fetch_env_variables(self, env_id: Optional[int]) -> Dict[str, str]:
        """GET /api/environments/{env_id}/variables → {key: value}。"""
        if not env_id:
            return {}
        data = self.get_json(f"/api/environments/{int(env_id)}/variables")
        result: Dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("key"):
                    result[str(item["key"])] = "" if item.get("value") is None else str(item.get("value"))
        return result

    def fetch_settings_map(self) -> Dict[str, Optional[str]]:
        """GET /api/settings/ → {key: raw_value}（app_key 映射等）。"""
        try:
            data = self.get_json("/api/settings/")
        except Exception as exc:
            logger.warning("拉取云端设置失败（按空映射继续）: %s", exc)
            return {}
        result: Dict[str, Optional[str]] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("key") is not None:
                    result[str(item["key"])] = item.get("value")
        elif isinstance(data, dict):
            for key, value in data.items():
                result[str(key)] = None if value is None else str(value)
        return result

    def fetch_app_key_mapping(self) -> Dict[str, Dict[str, str]]:
        from engine.core.execution_service import load_app_key_mapping

        return load_app_key_mapping(self.fetch_settings_map())

    # ---------- 场景数据（场景批量执行） ----------

    def fetch_scenario(self, scenario_id: int) -> Dict[str, Any]:
        """GET /api/scenarios/{id} → 场景（name/...）。"""
        return self.get_json(f"/api/scenarios/{int(scenario_id)}")

    def fetch_scenario_steps(self, scenario_id: int) -> List[Dict[str, Any]]:
        """GET /api/scenarios/{id}/steps → ScenarioStepRead 数组（case_id/order/alias）。"""
        data = self.get_json(f"/api/scenarios/{int(scenario_id)}/steps")
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    # ---------- 安装包（App 包管理安装本地化） ----------

    def fetch_package(self, package_id: int) -> Dict[str, Any]:
        """GET /api/packages/（分页扫描）→ AppPackageRead 行（按 id 定位）。"""
        package_id = int(package_id)
        for page in range(1, 11):
            data = self.get_json("/api/packages/", params={"page": page, "page_size": 100})
            items = (data or {}).get("items") if isinstance(data, dict) else None
            for item in items or []:
                if isinstance(item, dict) and int(item.get("id") or 0) == package_id:
                    return item
            if not items or len(items) < 100:
                break
        raise FileNotFoundError(f"安装包不存在: {package_id}")

    def download_package(self, package_id: int) -> bytes:
        """GET /api/packages/{id}/download → 包文件字节（APK/IPA）。"""
        with self._client() as client:
            resp = client.get(self._url(f"/api/packages/{int(package_id)}/download"), headers=self.auth_headers)
            resp.raise_for_status()
            return resp.content

    # ---------- 模板图 ----------

    def upload_template(self, image_bytes: bytes, filename: str) -> str:
        """POST /api/client-templates（multipart）→ 云端 image_path。"""
        payload = self.post_multipart(
            "/api/client-templates",
            files={"file": (filename, image_bytes, "image/png")},
        )
        image_path = str((payload or {}).get("image_path") or "").strip()
        if not image_path:
            raise RuntimeError("云端模板上传响应缺少 image_path")
        return image_path

    def ensure_template_local(self, image_path: str) -> str:
        """image_path（如 static/images/x.png）→ 本地绝对路径；缺失则从云端下载缓存。"""
        target = str(image_path or "").strip()
        if not target:
            raise ValueError("image_path 为空")
        if os.path.isabs(target):
            if not os.path.exists(target):
                raise FileNotFoundError(f"图像文件不存在: {target}")
            return target

        name = Path(target).name
        cached = template_cache_dir() / name
        if cached.is_file():
            return str(cached)

        # 云端 StaticFiles 挂载：mount("/api/static", STATIC_DIR) + mount("/static", STATIC_DIR)，
        # STATIC_DIR=static/（backend/main.py）。image_path 契约 rel 自带 "static/" 前缀
        # → 回源候选 /api/<rel> 与 /<rel>；再拼 /api/static/<rel> 会双 static/ 段 404。
        # 历史数据（S8）：image_path 无 static/ 前缀（如 images/x.png）时两类候选全 404，
        # 补 /api/static/<rel> 候选。
        rel = target.lstrip("/")
        candidates = [f"/api/{rel}", f"/{rel}"]
        if not rel.startswith("static/"):
            candidates.insert(0, f"/api/static/{rel}")
        for candidate in candidates:
            try:
                with self._client() as client:
                    resp = client.get(self._url(candidate), headers=self.auth_headers)
                    if resp.status_code // 100 == 2 and resp.content:
                        cached.write_bytes(resp.content)
                        return str(cached)
            except Exception as exc:
                logger.debug("模板下载失败 %s: %s", candidate, exc)
        raise FileNotFoundError(f"图像文件不存在且云端拉取失败: {target}")

    # ---------- 定时任务（P3-1 本地调度） ----------

    def fetch_client_tasks(self) -> List[Dict[str, Any]]:
        """GET /api/client-tasks → 应由客户端调度的任务列表。

        平台契约（backend/api/tasks.py list_client_tasks）：返回 {total, items}，
        条目为 ScheduledTask 全字段（ClientScheduledTaskRead：id/name/scenario_id/
        device_serials/strategy/strategy_config/is_active/enable_notification/...），
        仅含 is_active 且「创建者=本人 或 device_serials 命中本人设备」的任务。
        """
        data = self.get_json("/api/client-tasks")
        if isinstance(data, dict):
            items = data.get("items")
        else:
            items = data
        return [row for row in (items or []) if isinstance(row, dict)]

    # ---------- client-runs 上报 ----------

    def create_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post_json("/api/client-runs/", payload)

    def report_steps(self, run_key: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post_json(f"/api/client-runs/{run_key}/steps", payload)

    def finish_run(self, run_key: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post_json(f"/api/client-runs/{run_key}/finish", payload)

    # ---------- client-fastbot 上报（P3-2：采集本地 + 结果上报云端） ----------

    def create_client_fastbot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/client-fastbot/ → FastbotTaskRead（run_id 幂等）。"""
        return self.post_json("/api/client-fastbot/", payload)

    def finish_client_fastbot(self, task_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/client-fastbot/{id}/finish（status + report 六列）。"""
        return self.post_json(f"/api/client-fastbot/{int(task_id)}/finish", payload)

    # ---------- client-compatibility 上报（P4-2：回放本地执行 + 结果上报） ----------

    def create_client_compatibility_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/client-compatibility/runs → CompatibilityRunRead（client_executed run）。"""
        return self.post_json("/api/client-compatibility/runs", payload)

    def report_client_compatibility_run(self, run_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/client-compatibility/runs/{id}/report（cell × chain 幂等上报）。"""
        return self.post_json(
            f"/api/client-compatibility/runs/{int(run_id)}/report", payload
        )


def auth_headers_from_request(request: Any) -> Dict[str, str]:
    """从入站请求摘鉴权头（Authorization/cookie）；无则回退登录 token。"""
    headers: Dict[str, str] = {}
    try:
        for key in ("authorization", "cookie"):
            value = request.headers.get(key)
            if value:
                headers[key] = value
    except Exception:
        pass
    if not headers:
        try:
            from engine import presence

            token = presence.get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass
    return headers
