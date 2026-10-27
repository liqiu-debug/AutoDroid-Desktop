"""本地引擎 FastAPI 应用（只绑定 127.0.0.1，由 __main__ 启动）。

路由职责：
- GET  /api/client/info     客户端模式标识（云端平台不实现该路由，404 = Web 模式）
- POST /api/auth/token      读 X-AutoDroid-Server 归一化后设 upstream 并转发
- POST /api/devices/sync    本机 adb 扫描 → /api/client-devices/register → 回源 /api/devices/
- WS   /ws/* 与 /api/**     反代 upstream（消息双向透传；/api/** 覆盖场景执行 WS）
- 其余 /api/*               流式反代 upstream（本地设备 I/O 端点见 engine.local_api）
- 其余路径                   静态托管前端 dist（SPA fallback）
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.websockets import WebSocket

from engine import config_store, presence
from engine.adb_scan import scan_devices
from engine.local_api import router as local_api_router
from engine.proxy import (
    HOP_BY_HOP,
    SENSITIVE_REQUEST_HEADERS,
    apply_headers,
    build_target_url,
    filter_headers,
    passthrough_response,
    proxy_request,
)
from engine.server_address import ServerAddressError, normalize
from engine.ws_proxy import proxy_websocket

logger = logging.getLogger(__name__)

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRONTEND_DIST = ENGINE_ROOT.parent / "AutoDroid" / "frontend" / "dist"
SERVER_HEADER = "x-autodroid-server"
AUTH_FORWARDED_HEADERS = ("authorization", "cookie")

# 上传类请求放宽读超时；普通 API 保持较快失败
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


def frontend_dist_dir() -> Path:
    """前端 dist 目录：env AUTODROID_FRONTEND_DIST，缺省 ../AutoDroid/frontend/dist。"""
    override = os.environ.get("AUTODROID_FRONTEND_DIST", "").strip()
    return Path(override) if override else DEFAULT_FRONTEND_DIST


def client_version() -> str:
    """客户端版本：env AUTODROID_CLIENT_VERSION 缺省读 package.json。"""
    override = os.environ.get("AUTODROID_CLIENT_VERSION", "").strip()
    if override:
        return override
    try:
        with (ENGINE_ROOT / "package.json").open("r", encoding="utf-8") as fp:
            return str(json.load(fp).get("version") or "").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def platform_name() -> str:
    """契约取值 darwin | win32；其余平台透传 sys.platform。"""
    if sys.platform in ("darwin", "win32"):
        return sys.platform
    return sys.platform


def require_upstream() -> Optional[str]:
    return config_store.get_upstream()


def _upstream_missing() -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "未配置上游服务器（请先在登录页填写服务器地址）"})


def _auth_headers(request: Request) -> Dict[str, str]:
    """透传鉴权相关头到 upstream（register / 列表回源用）。"""
    headers: Dict[str, str] = {}
    for key in AUTH_FORWARDED_HEADERS:
        value = request.headers.get(key)
        if value:
            headers[key] = value
    return headers


def _list_devices_payload(data: Any) -> Optional[List[Dict[str, Any]]]:
    """回源 /api/devices/ 响应体 → 设备列表（契约：List[DeviceRead]）。"""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict) and isinstance(data.get("devices"), list):
        return [d for d in data["devices"] if isinstance(d, dict)]
    return None


def _count_online(devices: List[Dict[str, Any]]) -> int:
    """与 backend/api/devices.py sync 端点的在线口径一致。"""
    total = 0
    for device in devices:
        status = str(device.get("status") or "").strip().upper()
        app_presence = device.get("app_presence") if isinstance(device.get("app_presence"), dict) else {}
        if status != "OFFLINE" or str(app_presence.get("status") or "").upper() == "ONLINE":
            total += 1
    return total


def _serve_static(request: Request) -> Response:
    """静态托管 dist；未命中的非 /api 路径回退 index.html（SPA 路由）。"""
    dist = frontend_dist_dir()
    index = dist / "index.html"
    rel = request.url.path.lstrip("/")
    candidate = (dist / rel).resolve() if rel else index.resolve()
    try:
        candidate.relative_to(dist.resolve())
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    if candidate.is_file():
        media_type = mimetypes.guess_type(str(candidate))[0]
        return FileResponse(candidate, media_type=media_type)
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(
        status_code=404,
        content={"detail": "前端资源不存在（请先构建 frontend 或设置 AUTODROID_FRONTEND_DIST）"},
    )


def create_app() -> FastAPI:
    # 快赢：本地执行默认启用驱动连接池（显式 AUTODROID_DRIVER_POOL=0 可关闭）
    os.environ.setdefault("AUTODROID_DRIVER_POOL", "1")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from engine.core import runner_service, task_scheduler

        app.state.device_discovery = {"ts": 0.0, "serials": set()}
        app.state.http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False)
        runner_service.broker.bind_loop(asyncio.get_running_loop())
        # 定时任务本地调度（P3-1）：引擎存活期本地 APScheduler 触发；登录后/周期重拉
        try:
            task_scheduler.start_scheduler()
        except Exception:
            logger.warning("定时任务本地调度启动失败（其余功能不受影响）", exc_info=True)
        # 设备流（scrcpy）：启动 USB 设备监听（同平台 main.py 生命周期）
        try:
            from engine.core.device_stream.manager import device_manager

            device_manager.start_tracking()
        except Exception:
            logger.warning("设备流监听启动失败（投屏不可用，其余功能不受影响）", exc_info=True)
        try:
            yield
        finally:
            # 进程退出/托盘退出（SIGTERM → lifespan shutdown）时 best-effort
            # 把本客户端设备置 OFFLINE，再停心跳、关设备流、关 HTTP 客户端。
            try:
                await presence.logout_once(app.state.http)
            except Exception:
                logger.warning("退出 logout 上报失败", exc_info=True)
            presence.stop_heartbeat_loop()
            try:
                from engine.core.device_stream.manager import device_manager

                device_manager.stop_tracking()
            except Exception:
                logger.warning("设备流监听停止失败", exc_info=True)
            try:
                from engine.core import task_scheduler

                task_scheduler.shutdown_scheduler()
            except Exception:
                logger.warning("定时任务本地调度停止失败", exc_info=True)
            await app.state.http.aclose()

    app = FastAPI(title="AutoDroid Desktop Engine", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    # 本地设备 I/O / 本地执行路由必须先于 /{path:path} 与 /ws/{path:path} 注册
    # （Starlette 按注册顺序匹配），否则会被反代兜底吞掉。
    app.include_router(local_api_router)

    @app.get("/api/client/info")
    async def client_info() -> JSONResponse:
        return JSONResponse(
            content={"clientMode": True, "platform": platform_name(), "version": client_version()}
        )

    @app.post("/api/auth/token")
    async def auth_token(request: Request) -> Response:
        raw = request.headers.get(SERVER_HEADER)
        upstream = require_upstream()
        if raw:
            try:
                upstream = normalize(raw)
            except ServerAddressError as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})
            config_store.set_upstream(upstream)
        if not upstream:
            return JSONResponse(
                status_code=400, content={"detail": "缺少 X-AutoDroid-Server 请求头（客户端模式必须指定服务器地址）"}
            )
        # login 分支显式读取 X-AutoDroid-Server 后剥离；token 响应需整体缓冲，
        # 以便在内存中登记 access_token 供心跳/logout 使用（绝不落盘）。
        target = build_target_url(upstream, "/auth/token", request.url.query)
        headers = filter_headers(
            request.headers.items(), HOP_BY_HOP | SENSITIVE_REQUEST_HEADERS | frozenset({SERVER_HEADER})
        )
        body = await request.body()
        http = request.app.state.http
        try:
            upstream_response = await http.post(target, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("上游超时: POST %s -> %s", target, exc)
            return JSONResponse(status_code=504, content={"detail": f"上游服务器超时: {exc}"})
        except httpx.HTTPError as exc:
            logger.warning("上游连接失败: POST %s -> %s", target, exc)
            return JSONResponse(status_code=502, content={"detail": f"上游服务器连接失败: {exc}"})

        content = upstream_response.content
        token = None
        try:
            token = presence.extract_access_token(json.loads(content.decode("utf-8")))
        except Exception:
            token = None
        presence.set_token(token)
        if token:
            # 登录成功即接管设备存活链路：启动心跳循环（幂等）
            presence.start_heartbeat_loop(http)
            # P3-1：登录后拉取「应由客户端调度」的定时任务并在本地重排（后台线程，不阻塞登录）
            try:
                from engine.core import task_scheduler

                task_scheduler.scheduler.refresh_async()
            except Exception:
                logger.warning("定时任务本地拉取失败（不影响登录）", exc_info=True)

        response_headers = filter_headers(upstream_response.headers.items(), HOP_BY_HOP)
        result = Response(content=content, status_code=upstream_response.status_code)
        return apply_headers(result, response_headers)

    async def _register_local_devices(
        http: httpx.AsyncClient,
        auth_headers: dict,
        upstream: str,
        only_new: set = None,
    ) -> int:
        """本机 adb 扫描并逐台上报 /api/client-devices/register（返回成功台数）。

        only_new 传入「已登记 serial」可变集合时：跳过集合内设备、成功或 409 后
        写回（供 GET 列表的节流自愈使用，避免轮询反复上报）；缺省为全量上报
        （一键同步语义）。
        """
        infos = await asyncio.to_thread(scan_devices)
        synced = 0
        for info in infos:
            serial = str(info.get("serial") or "").strip()
            if not serial:
                continue
            if only_new is not None and serial in only_new:
                continue
            payload = dict(info)
            payload["client_instance_id"] = config_store.get_client_instance_id()
            register_url = f"{upstream.rstrip('/')}/api/client-devices/register"
            try:
                resp = await http.post(register_url, json=payload, headers=auth_headers)
            except httpx.HTTPError as exc:
                logger.warning("设备上报失败: %s -> %s", payload.get("serial"), exc)
                continue
            if resp.status_code // 100 == 2:
                synced += 1
                if only_new is not None:
                    only_new.add(serial)
            elif resp.status_code == 409:
                logger.info("设备已被其他成员注册: %s", payload.get("serial"))
                if only_new is not None:
                    only_new.add(serial)
            else:
                logger.warning(
                    "设备上报被拒: serial=%s status=%s body=%s",
                    payload.get("serial"),
                    resp.status_code,
                    resp.text[:200],
                )
        return synced

    @app.post("/api/devices/sync")
    async def sync_devices(request: Request) -> Response:
        """拦截设备同步：本机 adb 扫描上报，再回源权威设备列表。"""
        upstream = require_upstream()
        if not upstream:
            return _upstream_missing()

        auth_headers = _auth_headers(request)
        synced = await _register_local_devices(
            request.app.state.http, auth_headers, upstream
        )

        list_url = f"{upstream.rstrip('/')}/api/devices/"
        try:
            listed = await request.app.state.http.get(list_url, headers=auth_headers)
        except httpx.HTTPError as exc:
            logger.warning("回源设备列表失败: %s", exc)
            return JSONResponse(status_code=502, content={"detail": f"上游服务器连接失败: {exc}"})
        if listed.status_code // 100 != 2:
            return passthrough_response(listed)

        try:
            devices = _list_devices_payload(listed.json())
        except ValueError:
            devices = None
        if devices is None:
            return JSONResponse(
                status_code=502, content={"detail": "上游设备列表响应格式异常（应为设备数组）"}
            )

        online = _count_online(devices)
        return JSONResponse(
            content={
                "synced": synced,
                "online": online,
                "offline": len(devices) - online,
                "devices": devices,
            }
        )

    @app.get("/api/devices/")
    @app.get("/api/devices")
    async def list_devices_with_discovery(request: Request) -> Response:
        """GET 设备列表前置「节流自愈登记」：本机新插入的 adb 设备自动注册上报，

        消除「必须先去设备中心点一键同步，用例编辑器才看得到设备」的体验断点；
        随后照常反代云端权威列表。同一进程内 10 秒最多扫描/上报一轮。
        """
        upstream = require_upstream()
        if not upstream:
            return _upstream_missing()
        state = app.state.device_discovery
        now = time.monotonic()
        if now - state["ts"] >= 10.0:
            state["ts"] = now
            try:
                await _register_local_devices(
                    app.state.http,
                    _auth_headers(request),
                    upstream,
                    only_new=state["serials"],
                )
            except Exception:
                logger.warning("设备自愈登记失败（不影响列表）", exc_info=True)
        return await proxy_request(app.state.http, request, upstream)

    @app.websocket("/ws")
    @app.websocket("/ws/{path:path}")
    async def ws_route(websocket: WebSocket, path: str = "") -> None:
        upstream = require_upstream()
        if not upstream:
            await websocket.close(code=1011, reason="未配置上游服务器")
            return
        await proxy_websocket(websocket, upstream, "/ws" + (f"/{path}" if path else ""))

    # /api/** 的 WebSocket 升级（如 /api/scenarios/ws/run/{id}）同样反代，
    # 与 HTTP 网关共用 upstream；本地拦截的 WS（/ws/run/*）由 local_api 优先注册。
    @app.websocket("/api")
    @app.websocket("/api/{path:path}")
    async def ws_api_route(websocket: WebSocket, path: str = "") -> None:
        upstream = require_upstream()
        if not upstream:
            await websocket.close(code=1011, reason="未配置上游服务器")
            return
        await proxy_websocket(websocket, upstream, "/api" + (f"/{path}" if path else ""))

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def gateway(request: Request, path: str) -> Response:
        if path == "ws" or path.startswith("ws/"):
            return JSONResponse(
                status_code=501,
                content={"detail": "WebSocket 仅支持协议升级访问（/ws/* 反代）"},
            )
        if path == "api" or path.startswith("api/"):
            upstream = require_upstream()
            if not upstream:
                return _upstream_missing()
            return await proxy_request(request.app.state.http, request, upstream)
        return _serve_static(request)

    return app


app = create_app()
