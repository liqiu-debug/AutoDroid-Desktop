"""引擎本地配置存储（userData/config.json）。

只保存非敏感字段：{server: <归一化 upstream>, client_instance_id: <实例标识>}。
按 P0 契约，引擎绝不落盘任何 token / 密码。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_upstream: Optional[str] = None
_client_instance_id: Optional[str] = None


def user_data_dir() -> Path:
    """userData 目录：Electron 注入 AUTODROID_USER_DATA，缺省 ~/.autodroid-desktop。"""
    override = os.environ.get("AUTODROID_USER_DATA", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".autodroid-desktop"


def config_path() -> Path:
    return user_data_dir() / "config.json"


def _load_raw() -> Dict[str, Any]:
    path = config_path()
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("读取 %s 失败，按空配置处理: %s", path, exc)
        return {}


def _save_raw(data: Dict[str, Any]) -> None:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("写入 %s 失败: %s", path, exc)


def get_client_instance_id() -> str:
    """本机客户端实例标识（非敏感，随 register 上报；跨重启稳定）。"""
    global _client_instance_id
    with _lock:
        if _client_instance_id:
            return _client_instance_id
        data = _load_raw()
        value = str(data.get("client_instance_id") or "").strip()
        if not value:
            value = uuid.uuid4().hex
            data["client_instance_id"] = value
            _save_raw(data)
        _client_instance_id = value
        return value


def get_upstream() -> Optional[str]:
    """当前 upstream（内存优先，启动时从 config.json 恢复）。"""
    global _upstream
    with _lock:
        if _upstream:
            return _upstream
        value = str(_load_raw().get("server") or "").strip()
        _upstream = value or None
        return _upstream


def set_upstream(url: str) -> None:
    """写入 upstream（内存 + userData/config.json 的 {server} 字段）。"""
    global _upstream
    with _lock:
        _upstream = url
        data = _load_raw()
        data["server"] = url
        _save_raw(data)


def reset_for_tests() -> None:
    """测试辅助：清空内存状态。"""
    global _upstream, _client_instance_id
    with _lock:
        _upstream = None
        _client_instance_id = None
