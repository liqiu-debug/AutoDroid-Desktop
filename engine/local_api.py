# 来源：路由语义对齐 AutoDroid backend/api/recording.py + backend/api/cases.py run 端点。
"""本地设备 I/O 与本地执行路由（客户端模式：设备 I/O 全本地，结果上报云端）。

拦截（不再反代上游）：
- GET  /api/device/dump            dump：截图 + 层级 + 设备信息（可选 hierarchy_hash 缓存）
- POST /api/device/inspect         坐标 → 元素/定位策略/可录制步骤
- POST /api/device/interact        交互：分析 → 执行 → 等待稳定 → 新 dump
- POST /api/device/execute_step    单步执行（跨端 Runner）→ 新 dump
- POST /api/device/crop_template   模板裁剪 → 云端 /api/client-templates 上传（失败阻断重试）
- POST /api/cases/{id}/run         单设备后台执行（本地 Runner + client-runs 上报）
- POST /api/cases/{id}/run-batch   多设备批量执行（形状对齐 cases.py）
- GET  /api/cases/{id}/precheck    本地预检（形状对齐 cross_platform_execution）
- WS   /ws/run/{case_id}           连接即执行 + 实时步骤消息（协议对齐 ws_run.py）
- GET  /api/runs/active            本地活跃执行轮询（run registry）
- POST /api/runs/cancel            终止本地执行（abort_event + 等 runner 退出）
- POST /api/scenarios/{id}/run     场景多设备批量执行（形状对齐 scenarios.py run_scenario_api）
- GET  /api/scenarios/{id}/precheck 场景本地预检（形状对齐 scenarios.precheck_scenario_execution）
- WS   /api/scenarios/ws/run/{id}  场景实时执行（协议对齐 scenarios.websocket_run_scenario）
- POST /api/packages/{id}/install  App 包下载到本地 + adb install（形状对齐 packages.py）
- POST /api/fastbot/run            Fastbot 本地轻量档采集（含异常回放）+ client-fastbot 上报
- GET  /api/devices/{serial}/screenshot  设备中心截图（Android adb / iOS WDA）
- POST /api/devices/{serial}/wda/check   设备中心「启动 WDA」（iOS，WDA 在本机）
- WS   /ws/ios-mjpeg/{serial}            iOS 实时画面（WDA MJPEG 中继，每条消息一帧 JPEG）
- GET  /api/stream/ios-mjpeg/{serial}    HTTP 版 iOS 流（前端未使用，显式 503）
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from fastapi import APIRouter, Body, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from engine import config_store
from engine.core import recording_service
from engine.core.cloud_bridge import CloudBridge, auth_headers_from_request, template_cache_dir
from engine.core.launch_requests import (
    ENGINE_INSTANCE_ID, LaunchRequestRegistry, launch_fingerprint, launch_ids, session_scope,
)
from engine.core.recording_service import RecordingError
from engine.core.utils.image_match_preview import crop_metadata, preview_image_match

logger = logging.getLogger(__name__)

router = APIRouter()

# 视频泵专用线程池：get_video_generator 的 next() 等帧可能长占调用线程
# （无帧 deadline 内会一直持有），必须与 dump/interact/touch 共用的默认
# asyncio 线程池隔离，避免视频泵把默认池占死后本地设备 I/O 全体饿死。
VIDEO_PUMP_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="autodroid-video-pump")


def _json_response_with_optional_gzip(request: Request, payload: Any) -> Response:
    """对大 JSON 响应按需 gzip（同上游 recording.py 白名单端点语义）。"""
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    accept_encoding = (request.headers.get("accept-encoding") or "").lower()
    if len(body) >= recording_service.RESPONSE_GZIP_MIN_BYTES and "gzip" in accept_encoding:
        return Response(
            content=gzip.compress(body, compresslevel=6),
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(content=body, media_type="application/json")


def _error_response(exc: RecordingError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
#  录制链路
# --------------------------------------------------------------------------- #

@router.get("/api/device/dump")
@router.get("/device/dump", include_in_schema=False)
async def dump_device_info(
    request: Request,
    serial: Optional[str] = None,
    include_device_info: bool = True,
    include_hierarchy: bool = True,
    include_screenshot: bool = True,
    hierarchy_hash: Optional[str] = Query(default=None),
) -> Response:
    """获取设备信息：截图(base64) + 层级XML + 设备信息。

    hierarchy_hash（快赢）：前端缓存的层级哈希命中时省略 hierarchy_xml 原文。
    """
    try:
        payload = await _to_thread(
            recording_service.dump_device,
            serial=serial,
            include_device_info=include_device_info,
            include_hierarchy=include_hierarchy,
            include_screenshot=include_screenshot,
        )
    except RecordingError as exc:
        return _error_response(exc)

    if hierarchy_hash and payload.get("hierarchy_hash") == str(hierarchy_hash).strip():
        payload.pop("hierarchy_xml", None)
        payload["hierarchy_cached"] = True
    return _json_response_with_optional_gzip(request, payload)


class InteractionPayload(BaseModel):
    x: int
    y: int
    operation: str = "click"  # click, swipe, back, home, start_app, stop_app
    action_data: Optional[str] = None  # package name or swipe direction
    xml_dump: Optional[str] = None
    device_serial: Optional[str] = None
    record_step: bool = True
    include_screenshot: bool = True
    # 快赢：device_info 按需（前端有缓存兜底；显式 true 时随 dump 返回）
    include_device_info: bool = False
    locator_config: Optional[Dict[str, Any]] = None


_RECORDING_LOCATOR_CACHE_TTL_SECONDS = 10.0
_recording_locator_cache: Dict[tuple[str, str], tuple[float, Dict[str, Any]]] = {}
_recording_locator_cache_lock = threading.Lock()
_TRUE_BOOL_TEXTS = {"1", "true", "yes", "on", "enabled"}


def _coerce_locator_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE_BOOL_TEXTS:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _parse_recording_locator_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    enable_id_raw = (
        source.get("enable_resource_id")
        if "enable_resource_id" in source
        else source.get("recording_enable_resource_id")
    )
    enable_class_raw = (
        source.get("enable_class_name")
        if "enable_class_name" in source
        else source.get("recording_enable_class_name")
    )
    priority_raw = (
        source.get("priority")
        if "priority" in source
        else source.get("recording_locator_priority")
    )
    priority = "id_first" if str(priority_raw or "").strip().lower() == "id_first" else "semantic_first"
    return {
        "enable_resource_id": _coerce_locator_bool(enable_id_raw, default=False),
        "enable_class_name": _coerce_locator_bool(enable_class_raw, default=False),
        "priority": priority,
    }


def _resolve_recording_locator_config(
    request: Request,
    explicit_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if isinstance(explicit_config, dict):
        return _parse_recording_locator_config(explicit_config)

    auth_headers = auth_headers_from_request(request)
    auth_key = auth_headers.get("authorization") or auth_headers.get("Authorization") or ""
    upstream_key = config_store.get_upstream() or ""
    cache_key = (upstream_key, auth_key)
    now = time.monotonic()

    with _recording_locator_cache_lock:
        cached = _recording_locator_cache.get(cache_key)
        if cached and (now - cached[0]) < _RECORDING_LOCATOR_CACHE_TTL_SECONDS:
            return dict(cached[1])

    parsed = _parse_recording_locator_config(None)
    if upstream_key:
        try:
            with CloudBridge(auth_headers=auth_headers) as bridge:
                settings_map = bridge.fetch_settings_map()
            parsed = _parse_recording_locator_config(settings_map)
        except Exception as exc:
            logger.debug("拉取录制定位配置失败，使用默认值: %s", exc)

    with _recording_locator_cache_lock:
        _recording_locator_cache[cache_key] = (time.monotonic(), dict(parsed))
    return parsed


class SingleStepPayload(BaseModel):
    step: Dict[str, Any]
    case_id: Optional[int] = None
    env_id: Optional[int] = None
    variables: Optional[List[dict]] = Field(default_factory=list)
    device_serial: Optional[str] = None
    include_screenshot: bool = True
    include_device_info: bool = False


class CropTemplatePayload(BaseModel):
    screenshot_base64: str = Field(..., description="当前设备截图的 base64 编码")
    x1: int = Field(..., description="裁剪区域左上角 X 坐标（像素）")
    y1: int = Field(..., description="裁剪区域左上角 Y 坐标（像素）")
    x2: int = Field(..., description="裁剪区域右下角 X 坐标（像素）")
    y2: int = Field(..., description="裁剪区域右下角 Y 坐标（像素）")


@router.post("/api/device/inspect")
@router.post("/device/inspect", include_in_schema=False)
async def inspect_device(
    request: Request,
    x: int,
    y: int,
    serial: Optional[str] = None,
) -> JSONResponse:
    """审查模式：返回指定坐标处的最佳元素和定位策略。"""
    try:
        locator_cfg = await _to_thread(_resolve_recording_locator_config, request, None)
        payload = await _to_thread(recording_service.inspect_device, x, y, serial, locator_cfg)
    except RecordingError as exc:
        return _error_response(exc)
    return JSONResponse(content=payload)


@router.post("/api/device/interact")
@router.post("/device/interact", include_in_schema=False)
async def interact_with_device(request: Request, payload: InteractionPayload) -> Response:
    """交互模式：分析元素 → 执行点击 → 等待 UI 稳定 → 返回新状态。"""
    try:
        locator_cfg = (
            await _to_thread(_resolve_recording_locator_config, request, payload.locator_config)
            if (payload.operation == "click" and payload.record_step)
            else None
        )
        result = await _to_thread(
            recording_service.interact_device,
            x=payload.x,
            y=payload.y,
            operation=payload.operation,
            action_data=payload.action_data,
            xml_dump=payload.xml_dump,
            device_serial=payload.device_serial,
            record_step=payload.record_step,
            include_screenshot=payload.include_screenshot,
            include_device_info=payload.include_device_info,
            locator_config=locator_cfg,
        )
    except RecordingError as exc:
        return _error_response(exc)
    return _json_response_with_optional_gzip(request, result)


@router.post("/api/device/execute_step")
@router.post("/device/execute_step", include_in_schema=False)
async def execute_single_step(request: Request, payload: SingleStepPayload) -> Response:
    """执行单个步骤并返回最新 UI 快照（本地跨端 Runner）。"""
    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        env_variables: Dict[str, str] = {}
        if payload.env_id:
            try:
                env_variables = await _to_thread(bridge.fetch_env_variables, payload.env_id)
            except Exception as exc:
                logger.warning("拉取环境变量失败（按空继续）: %s", exc)

        try:
            result = await _to_thread(
                recording_service.execute_single_step,
                step=payload.step,
                device_serial=payload.device_serial,
                case_id=payload.case_id,
                env_variables=env_variables,
                variables=payload.variables,
                include_screenshot=payload.include_screenshot,
                include_device_info=payload.include_device_info,
                ensure_local=bridge.ensure_template_local,
            )
        except RecordingError as exc:
            return _error_response(exc)
        return _json_response_with_optional_gzip(request, result)


@router.get("/api/devices/{serial}/screenshot")
async def device_screenshot(serial: str) -> Response:
    """设备中心「查看截图」本地拦截（Android 本机 adb screencap / iOS 本机 WDA）。

    契约与平台 backend/api/devices.py get_screenshot 一致：
    {"base64_img": ..., "image_format": "jpeg"}（预览规格最长边 ≤1280 的 JPEG）。
    不拦截会反代到云端——云端看不到本机 USB 设备，报
    「error: device '<serial>' not found」。
    """
    from engine.adb_scan import resolve_adb_binary
    from engine.core import ios_device
    from engine.core.fastbot_ios.tools import is_ios_serial

    def capture_ios():
        from engine.core import device_busy

        with device_busy.interaction(serial):
            return ios_device.preview_screenshot(serial)

    if is_ios_serial(serial):
        try:
            return JSONResponse(content=await asyncio.to_thread(capture_ios))
        except ios_device.IOSDeviceError as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})

    try:
        adb = resolve_adb_binary()
    except FileNotFoundError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    try:
        def capture_android():
            from engine.core import device_busy

            with device_busy.interaction(serial):
                return _screencap_png(adb, serial)

        raw = await asyncio.to_thread(capture_android)
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    except _DeviceNotAttached as exc:
        return JSONResponse(
            status_code=404,
            content={"detail": f"{exc}（请检查 USB 连接与调试授权）"},
        )
    except Exception as exc:  # noqa: BLE001 - 统一转可读错误
        logger.warning("本地截图失败: serial=%s error=%s", serial, exc)
        return JSONResponse(status_code=500, content={"detail": f"截图失败: {exc}"})

    try:
        base64_img, image_format = await asyncio.to_thread(_to_preview_jpeg, raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("截图预览压缩失败: serial=%s error=%s", serial, exc)
        return JSONResponse(status_code=500, content={"detail": f"截图处理失败: {exc}"})

    return JSONResponse(content={"base64_img": base64_img, "image_format": image_format})


class _DeviceNotAttached(RuntimeError):
    """设备不在本机 adb 清单时抛出，转 404 可读提示。"""


def _screencap_png(adb: str, serial: str) -> bytes:
    """adb exec-out screencap -p 取原始 PNG（exec 数组，不经宿主 shell）。"""
    try:
        proc = subprocess.run(
            [adb, "-s", str(serial), "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=15,
        )
    except FileNotFoundError as exc:  # pragma: no cover - resolve 已兜底
        raise FileNotFoundError(str(exc)) from exc
    stderr = (proc.stderr or b"").decode("utf-8", "ignore").strip()
    if proc.returncode != 0 or not proc.stdout:
        if "not found" in stderr.lower() or "offline" in stderr.lower():
            raise _DeviceNotAttached(f"设备未连接到本机: {serial}")
        raise RuntimeError(stderr or f"adb screencap 退出码 {proc.returncode}")
    return proc.stdout


def _to_preview_jpeg(raw: bytes):
    """预览规格：最长边 ≤1280 的 JPEG q80（与平台 get_screenshot 口径一致）。"""
    from PIL import Image

    image = Image.open(io.BytesIO(raw))
    image.thumbnail((1280, 1280))
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return base64.b64encode(buffer.getvalue()).decode("ascii"), "jpeg"


@router.post("/api/devices/{serial}/wda/check")
async def check_device_wda(serial: str) -> JSONResponse:
    """设备中心「启动 WDA」本地拦截：有线 iPhone 的 WDA 在本机（iproxy + xcodebuild），云端探测不到。

    响应形状对齐平台 devices.check_ios_wda（前端读 wda_healthy / status / error）。
    冷启动超过请求等待上限时返回未就绪，启动在后台继续。
    """
    from engine.core import ios_device
    from engine.core.fastbot_ios.tools import is_ios_serial

    if not is_ios_serial(serial):
        return JSONResponse(
            status_code=400,
            content={"detail": "P3002_WDA_IOS_ONLY: WDA 启动仅适用于 iOS 设备。"},
        )
    wda_url = None
    error = None
    try:
        def check_local_wda():
            from engine.core import device_busy

            with device_busy.interaction(serial):
                return ios_device.wda_url(serial)

        wda_url = await asyncio.to_thread(check_local_wda)
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    except ios_device.IOSDeviceError as exc:
        error = str(exc)
    return JSONResponse(
        content={
            "serial": serial,
            "platform": "ios",
            "wda_healthy": wda_url is not None,
            "wda_url": wda_url,
            "status": "IDLE" if wda_url is not None else "WDA_DOWN",
            "error": error,
            "attempted_start": False,
            "recovered_by_cleanup": False,
        }
    )


@router.post("/api/device/crop_template")
@router.post("/device/crop_template", include_in_schema=False)
async def crop_template(request: Request, payload: CropTemplatePayload) -> JSONResponse:
    """手动截取图像模板：裁剪 → 云端 /api/client-templates 上传 → 返回云端 image_path。

    R6：上传失败即阻断并提示重试——本地兜底文件名（element_<uuid>.png）一旦写进用例
    步骤就是悬空引用（云端无此文件），故仅返回上云成功的 image_path。
    """
    try:
        image_bytes, suggested_name = await _to_thread(
            recording_service.crop_template_image,
            payload.screenshot_base64,
            payload.x1,
            payload.y1,
            payload.x2,
            payload.y2,
        )
    except RecordingError as exc:
        return _error_response(exc)

    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        if not bridge.available:
            return JSONResponse(
                status_code=503,
                content={"detail": "未配置上游服务器，模板无法上云保存，请先登录后重试"},
            )
        try:
            image_path = await _to_thread(bridge.upload_template, image_bytes, suggested_name)
        except Exception as exc:
            logger.warning("模板上传云端失败（阻断返回，避免悬空引用）: %s", exc)
            return JSONResponse(
                status_code=502,
                content={"detail": f"模板上传云端失败，请重试：{exc}"},
            )

        # 本地缓存按云端文件名保存，供本地执行引用（无需回源）
        cache_name = image_path.rsplit("/", 1)[-1] or suggested_name
        try:
            (template_cache_dir() / cache_name).write_bytes(image_bytes)
        except Exception:
            logger.warning("模板本地缓存写入失败", exc_info=True)

        logger.info("手动截取模板图已保存: %s (%s bytes)", image_path, len(image_bytes))
        metadata = await _to_thread(crop_metadata, image_bytes, payload.screenshot_base64)
        return JSONResponse(content={"image_path": image_path, **metadata})


class MatchImagePayload(BaseModel):
    screenshot_base64: str
    image_path: str
    image_options: Optional[Dict[str, Any]] = None
    image_match: Optional[Dict[str, Any]] = None


@router.post("/api/device/match_image")
@router.post("/device/match_image", include_in_schema=False)
async def preview_match_image(request: Request, payload: MatchImagePayload) -> JSONResponse:
    """Match a provided frame locally. This endpoint never connects to a device."""
    import re

    if not re.fullmatch(r"static/images/[\w.-]+\.(?:png|jpe?g)", payload.image_path, flags=re.IGNORECASE):
        return JSONResponse(status_code=400, content={"detail": "模板路径无效，请重新截取"})
    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        try:
            path = await _to_thread(bridge.ensure_template_local, payload.image_path)
            from pathlib import Path
            result = await _to_thread(preview_image_match, payload.screenshot_base64, Path(path).read_bytes(), payload.image_options, payload.image_match)
            return JSONResponse(content=result)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=502, content={"detail": f"模板读取失败: {exc}"})


# --------------------------------------------------------------------------- #
#  本地执行 + 上报
# --------------------------------------------------------------------------- #

class CaseRunBatchPayload(BaseModel):
    env_id: Optional[int] = None
    device_serials: Optional[List[Optional[str]]] = Field(default_factory=list)


def _run_payload(case_id: int, batch_id: str, run_id: str, device_serial: str, message: str) -> Dict[str, Any]:
    """形状对齐 backend/api/cases.py run_test_case 响应。"""
    return {
        "message": message,
        "case_id": case_id,
        "batch_id": batch_id,
        "run_id": run_id,
        "device_serial": device_serial,
        "runner": "cross_platform",
        "queued": False,
        "queue_position": None,
    }


# Cloud record preparation has a batch deadline independent of device count.
CREATE_RECORD_DEADLINE_SECONDS = 10.0
CREATE_RECORD_CONCURRENCY = 4


def _execution_data_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            detail = exc.response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if status == 404 and detail in (None, "Not Found", "Not found"):
            detail = "云端尚未提供执行数据接口，请先升级云端服务"
        return JSONResponse(status_code=status if status in (401, 403, 404) else 502,
                            content={"detail": detail or "云端执行数据读取失败，请稍后重试"})
    if isinstance(exc, httpx.TimeoutException):
        return JSONResponse(status_code=504, content={"detail": "读取执行数据超时，尚未启动执行，请稍后重试"})
    return JSONResponse(status_code=502, content={"detail": f"读取执行数据失败：{exc}"})


def _device_serials(values: List[Optional[str]]) -> List[Optional[str]]:
    # Deduplicate data/launch work while retaining the user's response order.
    return list(dict.fromkeys(str(value).strip() or None if value is not None else None for value in values))


async def _precreate_scenario_records(
    bridge: CloudBridge, client: httpx.AsyncClient, payloads: List[Dict[str, Any]],
) -> Dict[str, Any]:
    semaphore = asyncio.Semaphore(CREATE_RECORD_CONCURRENCY)
    confirmed: Dict[str, Any] = {}
    attempted: Dict[str, Dict[str, Any]] = {}

    async def create(payload: Dict[str, Any]) -> None:
        try:
            async with semaphore:
                attempted[payload["device_serial"]] = payload
                result = await bridge.create_run_async(payload, client)
                if (result or {}).get("id") is not None:
                    confirmed[payload["device_serial"]] = result["id"]
        except Exception as exc:
            logger.warning("云端执行记录尚未确认，本地沿用同一 run_id 继续：%s", exc)

    tasks = [asyncio.create_task(create(payload)) for payload in payloads]
    interrupted = False
    try:
        if tasks:
            await asyncio.wait(tasks, timeout=CREATE_RECORD_DEADLINE_SECONDS)
    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if interrupted and attempted:
            # A disconnect on legacy callers or a forced shutdown can happen
            # after the cloud committed a record but before its response arrived.
            # Resolve only attempted IDs, then finish them; no device is started.
            async def abort_unstarted(serial, payload):
                try:
                    async with semaphore:
                        key = confirmed.get(serial)
                        if key is None:
                            result = await bridge.create_run_async(payload, client)
                            key = (result or {}).get("id")
                        if key is not None:
                            response = await client.post(
                                bridge._url(f"/api/client-runs/{key}/finish"), headers=bridge.auth_headers,
                                json={"status": "ABORTED", "aborted": True, "duration": 0,
                                      "error": "启动准备已取消，未执行设备操作"}, timeout=2.0,
                            )
                            response.raise_for_status()
                except Exception:
                    logger.warning("取消启动后的云端记录收尾未确认", exc_info=True)

            cleanup = [asyncio.create_task(abort_unstarted(serial, payload)) for serial, payload in attempted.items()]
            try:
                await asyncio.wait(cleanup, timeout=2.0)
            finally:
                for task in cleanup:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*cleanup, return_exceptions=True)
    return confirmed


async def _submit_execution(
    request: Request, kind: str, target_id: int, env_id: Optional[int],
    device_serials: List[Optional[str]], *, single: bool = False,
) -> JSONResponse:
    from engine.core import runner_service, scenario_service

    request_id = (request.headers.get("Idempotency-Key") or "").strip() or None
    instance = request.headers.get("X-AutoDroid-Engine-Instance")
    if instance is not None and instance != ENGINE_INSTANCE_ID:
        return JSONResponse(status_code=409, content={"detail": {
            "code": "L1002_ENGINE_INSTANCE_CHANGED",
            "message": "桌面引擎已重启，请查看原执行记录后重新操作",
        }})
    if request_id and not instance:
        return JSONResponse(status_code=400, content={"detail": {
            "code": "L1003_ENGINE_INSTANCE_REQUIRED", "message": "启动请求缺少桌面实例标识，请刷新后重试",
        }})
    if request_id and len(request_id) > 256:
        return JSONResponse(status_code=400, content={"detail": "启动请求编号过长"})

    headers = auth_headers_from_request(request)
    from engine import config_store

    upstream = config_store.get_upstream() or ""
    serials = _device_serials(device_serials or [None])
    scope = session_scope(upstream, headers)
    batch_id, device_run_id = launch_ids(scope, request_id)

    async def launch() -> JSONResponse:
        with CloudBridge(upstream=upstream, auth_headers=headers) as bridge:
            try:
                fetch = bridge.fetch_case_execution_data if kind == "case" else bridge.fetch_scenario_execution_data
                data = await _to_thread(fetch, target_id, env_id)
            except Exception as exc:
                return _execution_data_error(exc)

            runnable: List[str] = []
            blocked: List[Dict[str, Any]] = []
            check_device = runner_service.precheck_for_device if kind == "case" else scenario_service.precheck_scenario_for_device
            for serial in serials:
                if not serial:
                    blocked.append({"device_serial": serial, "reason": "请选择执行设备"})
                    continue
                try:
                    check = await _to_thread(check_device, bridge, target_id, serial, env_id, execution_data=data)
                except Exception as exc:
                    blocked.append({"device_serial": serial, "reason": f"预检异常：{exc}"})
                    continue
                if check.get("ok"):
                    runnable.append(serial)
                else:
                    blocked.append({"device_serial": serial, "reason": scenario_service.summarize_precheck_failure(check),
                                    "precheck": check})
            if not runnable:
                return JSONResponse(status_code=400, content={"detail": {
                    "code": "C1001_CASE_PRECHECK_FAILED" if kind == "case" else "S1001_SCENARIO_PRECHECK_FAILED",
                    "message": "运行前预检未通过，所选设备均无法执行", "items": blocked,
                }})

            confirmed = {}
            if kind == "scenario":
                payloads = [{
                    "run_id": device_run_id(serial), "kind": "scenario", "case_id": None,
                    "scenario_id": target_id, "device_serial": serial, "platform": None,
                    "run_source": "platform", "started_at": datetime.now().isoformat(),
                    "name": None, "env_id": env_id,
                } for serial in runnable]
                confirmed = await _precreate_scenario_records(bridge, request.app.state.http, payloads)

            runs: List[Dict[str, Any]] = []
            failed_records = []
            for serial in runnable:
                run_id = device_run_id(serial)
                key = confirmed.get(serial)
                try:
                    common = dict(env_id=env_id, device_serial=serial, run_id=run_id,
                                  batch_id=batch_id, auth_headers=headers, upstream=upstream, execution_data=data)
                    if kind == "case":
                        runner_service.start_background_case_run(case_id=target_id, **common)
                    else:
                        scenario_service.start_background_scenario_run(scenario_id=target_id, preset_run_key=key, **common)
                except Exception:
                    # Keep the launch key and other devices' outcomes. Never replay
                    # workers that were successfully started before this failure.
                    logger.exception("本地执行线程启动失败：kind=%s target=%s", kind, target_id)
                    blocked.append({"device_serial": serial, "reason": "本地执行线程启动失败"})
                    if kind == "scenario":
                        failed_records.append((serial, key))
                    continue
                runs.append({"run_id": run_id, "execution_id": key, "device_serial": serial,
                             "queued": False, "queue_position": None,
                             "cloud_record_pending": kind == "scenario" and key is None})
            if failed_records:
                cleanup_slots = asyncio.Semaphore(CREATE_RECORD_CONCURRENCY)

                async def finish_failed(serial, key):
                    try:
                        async with cleanup_slots:
                            if key is None:
                                payload = next(payload for payload in payloads if payload["device_serial"] == serial)
                                created = await bridge.create_run_async(payload, request.app.state.http)
                                key = (created or {}).get("id")
                            if key is None:
                                return
                            response = await request.app.state.http.post(
                                bridge._url(f"/api/client-runs/{key}/finish"), headers=headers,
                                json={"status": "ERROR", "error": "本地执行线程启动失败", "duration": 0},
                                timeout=2.0,
                            )
                            response.raise_for_status()
                    except Exception:
                        logger.warning("启动失败的云端记录收尾未确认", exc_info=True)

                cleanup_tasks = [asyncio.create_task(finish_failed(serial, key)) for serial, key in failed_records]
                try:
                    await asyncio.wait(cleanup_tasks, timeout=2.0)
                finally:
                    for task in cleanup_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            if not runs:
                return JSONResponse(status_code=500, content={"detail": {
                    "code": "L1004_LAUNCH_RESULT_UNKNOWN", "message": "本地启动失败，请查看执行记录后重新操作",
                    "items": blocked,
                }})
            if single:
                response = _run_payload(target_id, batch_id, runs[0]["run_id"], runs[0]["device_serial"], "Execution started")
                return JSONResponse(content={**response, "runs": runs, "blocked_prechecks": blocked})
            return JSONResponse(content={
                "message": "Batch execution started", "batch_id": batch_id,
                **({"case_id": target_id, "run_ids": [row["run_id"] for row in runs],
                    "device_serials": [row["device_serial"] for row in runs]} if kind == "case" else {}),
                "execution_ids": [row["execution_id"] for row in runs if row["execution_id"] is not None],
                "blocked_prechecks": blocked, "runs": runs, "queued_count": 0,
            })

    if request_id:
        registry = getattr(request.app.state, "launch_requests", None)
        if registry is None:
            registry = request.app.state.launch_requests = LaunchRequestRegistry()
        return await registry.submit(scope, request_id, launch_fingerprint(
            f"{kind}:single" if single else kind, target_id, env_id, serials,
        ), launch)
    return await launch()


@router.post("/api/cases/{case_id}/run")
@router.post("/cases/{case_id}/run", include_in_schema=False)
async def run_test_case(
    request: Request, case_id: int, env_id: Optional[int] = None, device_serial: Optional[str] = None,
) -> JSONResponse:
    """Precheck once and pass the exact input snapshot to the background runner."""
    if not device_serial:
        return JSONResponse(status_code=400, content={"detail": "请选择执行设备"})
    return await _submit_execution(request, "case", case_id, env_id, [device_serial], single=True)


@router.post("/api/cases/{case_id}/run-batch")
@router.post("/cases/{case_id}/run-batch", include_in_schema=False)
async def run_test_case_batch(
    request: Request, case_id: int, payload: CaseRunBatchPayload = Body(default_factory=CaseRunBatchPayload),
) -> JSONResponse:
    return await _submit_execution(request, "case", case_id, payload.env_id, payload.device_serials or [None])


@router.websocket("/ws/run/{case_id}")
async def websocket_run_case(websocket: WebSocket, case_id: int) -> None:
    """WS 实时执行（连接即执行；消息协议对齐 ws_run.py）。"""
    from engine.core import runner_service

    try:
        env_id_raw = websocket.query_params.get("env_id")
        env_id = int(env_id_raw) if env_id_raw not in (None, "") else None
    except (TypeError, ValueError):
        env_id = None
    device_serial = websocket.query_params.get("device_serial") or None
    try:
        start_step_raw = websocket.query_params.get("start_step_index")
        start_step_index = max(0, int(start_step_raw)) if start_step_raw not in (None, "") else 0
    except (TypeError, ValueError):
        start_step_index = 0

    kwargs = {"start_step_index": start_step_index} if start_step_index > 0 else {}
    await runner_service.run_case_via_websocket(
        websocket,
        case_id,
        env_id,
        device_serial,
        auth_headers_from_request(websocket),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
#  执行旁路端点（R4：本地拦截，云端看不到本地 run）
#  - GET  /api/cases/{id}/precheck   启动前预检（本地 runner_service 语义）
#  - GET  /api/runs/active           run_start 后每 3s 轮询（本地 registry 应答）
#  - POST /api/runs/cancel           终止（置 abort_event 并等 runner 退出）
#  形状对齐 backend/api/cases.py precheck_test_case 与 backend/api/runs.py。
# --------------------------------------------------------------------------- #

@router.get("/api/cases/{case_id}/precheck")
@router.get("/cases/{case_id}/precheck", include_in_schema=False)
async def precheck_case(
    request: Request,
    case_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
) -> JSONResponse:
    """本地预检（不再反代云端：云端 WDA/设备探测与本地执行语义不符）。"""
    from engine.core import runner_service

    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        try:
            payload = await _to_thread(runner_service.precheck_for_device, bridge, case_id, device_serial, env_id)
        except Exception as exc:
            return _execution_data_error(exc)
        return JSONResponse(content=payload)


class CancelRunPayload(BaseModel):
    kind: str
    target_id: int
    batch_id: Optional[str] = None
    run_ids: List[str] = Field(default_factory=list)
    execution_ids: List[int] = Field(default_factory=list)
    device_serials: List[str] = Field(default_factory=list)


@router.get("/api/runs/active")
@router.get("/runs/active", include_in_schema=False)
async def active_runs(kind: str = Query(...), target_id: int = Query(...)) -> JSONResponse:
    """本地活跃执行（本地 registry，形状对齐 backend/api/runs.py active_runs）。"""
    from engine.core import run_registry

    normalized_kind = str(kind or "").strip().lower()
    runs = [record.to_dict() for record in run_registry.registry.active(kind=normalized_kind, target_id=target_id)]
    return JSONResponse(content={"items": runs})


@router.post("/api/runs/cancel")
@router.post("/runs/cancel", include_in_schema=False)
async def cancel_runs(payload: CancelRunPayload) -> JSONResponse:
    """终止本地执行：置 abort_event 并等 runner 退出（超时仍返回已请求中止）。"""
    from engine.core import run_registry

    kind = str(payload.kind or "").strip().lower()
    records = run_registry.registry.cancel(
        kind=kind,
        target_id=payload.target_id,
        batch_id=payload.batch_id,
        run_ids=payload.run_ids,
        execution_ids=payload.execution_ids,
        device_serials=payload.device_serials,
    )
    # 等 runner 退出（run_case_local / 场景执行 finally 里 complete 置 done_event）
    await _to_thread(
        run_registry.registry.wait_for_exit,
        records,
        run_registry.DEFAULT_CANCEL_WAIT_SECONDS,
    )
    return JSONResponse(
        content={
            "status": run_registry.ABORTED_STATUS,
            "cancelled_count": len(records),
            # 本地无 DB（云端 TestExecution 由 client-runs 收尾落状态）
            "db_updates": 0,
            "runs": [record.to_dict() for record in records],
        }
    )


# --------------------------------------------------------------------------- #
#  场景批量执行（P2：本地化；形状对齐 backend/api/scenarios.py）
#  - POST /api/scenarios/{id}/run      多设备批量（响应含 execution_ids/blocked_prechecks/runs）
#  - GET  /api/scenarios/{id}/precheck 场景预检（cases[] 键含 alias）
#  - WS   /api/scenarios/ws/run/{id}   连接即执行（log/run_start/run_complete 协议）
#  执行中止复用 run registry/abort（同 R4）。
# --------------------------------------------------------------------------- #

class ScenarioRunBatchPayload(BaseModel):
    env_id: Optional[int] = None
    device_serials: Optional[List[Optional[str]]] = Field(default_factory=list)


@router.get("/api/scenarios/{scenario_id}/precheck")
@router.get("/scenarios/{scenario_id}/precheck", include_in_schema=False)
async def precheck_scenario(
    request: Request,
    scenario_id: int,
    device_serial: str,
    env_id: Optional[int] = None,
) -> JSONResponse:
    """场景本地预检（形状对齐 scenarios.precheck_scenario_execution）。"""
    from engine.core import scenario_service

    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        try:
            payload = await _to_thread(
                scenario_service.precheck_scenario_for_device, bridge, scenario_id, device_serial, env_id
            )
        except Exception as exc:
            return _execution_data_error(exc)
        return JSONResponse(content=payload)


@router.post("/api/scenarios/{scenario_id}/run")
@router.post("/scenarios/{scenario_id}/run", include_in_schema=False)
async def run_scenario_batch(
    request: Request, scenario_id: int, payload: ScenarioRunBatchPayload = Body(default_factory=ScenarioRunBatchPayload),
) -> JSONResponse:
    return await _submit_execution(request, "scenario", scenario_id, payload.env_id, payload.device_serials or [None])


@router.websocket("/api/scenarios/ws/run/{scenario_id}")
async def websocket_run_scenario(websocket: WebSocket, scenario_id: int) -> None:
    """WS 场景实时执行（消息协议对齐 scenarios.websocket_run_scenario）。"""
    from engine.core import scenario_service

    try:
        env_id_raw = websocket.query_params.get("env_id")
        env_id = int(env_id_raw) if env_id_raw not in (None, "") else None
    except (TypeError, ValueError):
        env_id = None
    device_serial = websocket.query_params.get("device_serial") or None

    await scenario_service.run_scenario_via_websocket(
        websocket,
        scenario_id,
        env_id,
        device_serial,
        auth_headers_from_request(websocket),
    )


# --------------------------------------------------------------------------- #
#  App 包安装本地化（P2：云端下载 + 本地 adb install；形状对齐 packages.py）
# --------------------------------------------------------------------------- #

class PackageInstallPayload(BaseModel):
    serial: str


@router.post("/api/packages/{package_id}/install")
@router.post("/packages/{package_id}/install", include_in_schema=False)
async def install_package_to_device(
    request: Request,
    package_id: int,
    payload: PackageInstallPayload,
) -> JSONResponse:
    from engine.core import package_service

    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        try:
            result = await _to_thread(
                package_service.install_package_local, bridge, package_id, payload.serial
            )
        except package_service.PackageInstallError as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
        return JSONResponse(content=result)


# --------------------------------------------------------------------------- #
#  Startup collectors always run on this machine. Never proxy their POST to
#  cloud ADB, even when an old server does not support the new protocol.
# --------------------------------------------------------------------------- #

@router.get("/api/fastbot/startup/devices")
@router.get("/fastbot/startup/devices", include_in_schema=False)
async def startup_devices_local(request: Request) -> JSONResponse:
    from engine.core.startup_service import service

    try:
        with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
            return JSONResponse(content=await service.devices(bridge))
    except httpx.HTTPStatusError as exc:
        return JSONResponse(status_code=exc.response.status_code, content={"detail": "启动设备列表读取失败，请检查登录状态"})
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"detail": "平台暂时不可达，请稍后重试"})


@router.get("/api/fastbot/startup/local-results")
@router.get("/fastbot/startup/local-results", include_in_schema=False)
async def startup_results_local(request: Request) -> JSONResponse:
    from engine.core.startup_service import service

    try:
        with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
            return JSONResponse(content=await service.local_results(bridge))
    except (httpx.HTTPError, ValueError):
        return JSONResponse(status_code=401, content={"detail": "请登录运行所属账号后读取本地结果"})


@router.post("/api/fastbot/startup/run")
@router.post("/fastbot/startup/run", include_in_schema=False)
async def startup_run_local(request: Request, payload: Dict[str, Any]) -> JSONResponse:
    from pydantic import ValidationError
    from engine.core.startup_service import service

    try:
        with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
            result = await service.start(bridge, payload)
            if any(not task.get("id") for task in result.get("tasks", [])):
                return JSONResponse(status_code=503, content={"detail": {
                    "code": "L1004_LAUNCH_RESULT_UNKNOWN",
                    "message": "平台登记结果待确认，本次未采样的设备已停止；本地结果将在网络恢复后同步，请查看任务记录",
                    "tasks": result["tasks"],
                }})
            return JSONResponse(content=result)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": exc.errors(include_context=False)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except httpx.HTTPStatusError as exc:
        return JSONResponse(status_code=exc.response.status_code, content={"detail": "平台拒绝启动，请检查设备归属和客户端版本"})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"detail": "平台暂时不可达，尚未提交启动测试"})
    except Exception as exc:
        logger.exception("本地启动测试提交失败")
        return JSONResponse(status_code=503, content={"detail": str(exc)})


@router.post("/api/fastbot/startup/tasks/{task_id}/stop")
@router.post("/fastbot/startup/tasks/{task_id}/stop", include_in_schema=False)
async def startup_stop_local(request: Request, task_id: int) -> JSONResponse:
    from engine.core.startup_service import service

    # Validate with cloud first. During an outage, only the exact login already
    # authenticated in this process can stop its own local collector.
    try:
        with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
            try:
                response = await asyncio.to_thread(bridge.stop_startup, task_id)
            except (httpx.ConnectError, httpx.TimeoutException):
                if service.cancel_authenticated_local(bridge, task_id):
                    return JSONResponse(content={"task_id": task_id, "cancel_requested": True,
                        "local_cancel_requested": True, "sync_state": "pending",
                        "message": "已请求停止本机采集，保留的部分结果将在网络恢复后同步"})
                raise
        local = service.cancel(task_id)
        return JSONResponse(content={**response, "cancel_requested": True, "local_cancel_requested": local})
    except httpx.HTTPStatusError as exc:
        return JSONResponse(status_code=exc.response.status_code, content={"detail": "无法取消该启动任务"})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"detail": "平台暂时不可达，取消未确认"})


# --------------------------------------------------------------------------- #
#  Fastbot 智能探索本地拦截（P3-2 轻量档：采集本地 + client-fastbot 上报云端）
# --------------------------------------------------------------------------- #

class FastbotRunPayload(BaseModel):
    package_name: str
    duration: int = 600
    throttle: int = 500
    enable_performance_monitor: bool = False
    enable_jank_frame_monitor: bool = False
    enable_local_replay: bool = True
    ignore_crashes: bool = False
    capture_log: bool = True
    device_serial: str
    enable_custom_event_weights: bool = False
    pct_touch: int = 40
    pct_motion: int = 30
    pct_syskeys: int = 5
    pct_majornav: int = 15


@router.post("/api/fastbot/run")
@router.post("/fastbot/run", include_in_schema=False)
async def run_fastbot_task_local(request: Request, payload: FastbotRunPayload) -> JSONResponse:
    """POST /api/fastbot/run 本地拦截（形状对齐 api/fastbot.py run_fastbot → FastbotTaskRead）。

    轻量档：本机 Monkey/logcat/性能采集 + 异常回放录制 + client-fastbot 上报；读接口
    （/api/fastbot/tasks、/reports/{id}）仍走反代云端展示，Crash/ANR 回放视频作为
    产物上传云端（replays/<name>.mp4，平台 /api/fastbot/replays 直接服务）。
    enable_jank_frame_monitor 请求降级（summary.degraded 标注）。
    """
    from engine.core import fastbot_service
    from engine.core.device_stream.manager import device_manager

    device_serial = str(payload.device_serial or "").strip()
    from engine.core.fastbot_ios.tools import is_ios_serial, local_ios_serials, udid_from_serial

    if is_ios_serial(device_serial):
        ios_online = await _to_thread(local_ios_serials)
        if ios_online is None:
            return JSONResponse(
                status_code=400,
                content={"detail": "未检测到 libimobiledevice（brew install libimobiledevice），无法连接 iOS 设备"},
            )
        try:
            udid = udid_from_serial(device_serial)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        if udid not in ios_online:
            return JSONResponse(status_code=400, content={"detail": "iOS 设备未通过 USB 连接或未信任本机"})
    elif not device_manager.get_device(device_serial):
        return JSONResponse(status_code=400, content={"detail": "设备不在线或未就绪"})
    with CloudBridge(auth_headers=auth_headers_from_request(request)) as bridge:
        if not bridge.available:
            return JSONResponse(
                status_code=503,
                content={"detail": "未配置上游服务器，Fastbot 结果无法上报，请先登录后重试"},
            )
        try:
            created = await _to_thread(
                fastbot_service.start_fastbot_run, bridge, payload.model_dump()
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception as exc:
            logger.warning("Fastbot 本地启动失败: %s", exc)
            return JSONResponse(status_code=502, content={"detail": f"Fastbot 启动失败：{exc}"})
        return JSONResponse(content=created)


# --------------------------------------------------------------------------- #
#  兼容性任务创建（升级后链路回放 / installed_replay 已随智能巡检下线）
#  - POST /api/compatibility/runs  通用创建（多设备矩阵/对比等）原样反代云端；
#    巡检回放本地执行（preflight + client-compatibility 上报）已整体移除
# --------------------------------------------------------------------------- #

@router.post("/api/compatibility/runs")
@router.post("/compatibility/runs", include_in_schema=False)
async def compatibility_create_run_local(request: Request) -> Response:
    """兼容性任务创建：执行模式（多设备矩阵/对比等）原样反代云端。"""
    from engine.proxy import proxy_request

    upstream = _upstream_base(request)
    if not upstream:
        return JSONResponse(
            status_code=503,
            content={"detail": "未配置上游服务器（请先在登录页填写服务器地址）"},
        )
    return await proxy_request(request.app.state.http, request, upstream)


def _upstream_base(request: Request) -> Optional[str]:
    from engine import config_store

    return config_store.get_upstream()


# --------------------------------------------------------------------------- #
#  设备流（scrcpy 本地：视频流/触控/清晰度档位）
#  来源语义对齐 backend/device_stream/router.py；归属过滤由「本机即设备归属方」
#  天然满足（本地引擎只绑 127.0.0.1，设备 I/O 全本地）。
# --------------------------------------------------------------------------- #

class TouchEventPayload(BaseModel):
    action: int = 0  # 兼容语义：0=tap, 1=抬起, 2=移动
    x: int
    y: int
    method: str = "scrcpy"  # scrcpy=control socket, adb=adb shell input tap


class StreamProfilePayload(BaseModel):
    profile: str


@router.get("/api/stream/devices")
@router.get("/stream/devices", include_in_schema=False)
async def stream_devices() -> JSONResponse:
    from engine.core.device_stream.manager import device_manager

    return JSONResponse(content=device_manager.get_devices_list())


@router.get("/api/stream/devices/{serial}")
@router.get("/stream/devices/{serial}", include_in_schema=False)
async def stream_device(serial: str) -> JSONResponse:
    from engine.core.device_stream.manager import device_manager

    info = device_manager.get_device(serial)
    if not info:
        return JSONResponse(status_code=404, content={"detail": f"设备 {serial} 未找到"})
    return JSONResponse(content=info)


@router.post("/api/stream/devices/{serial}/touch")
@router.post("/stream/devices/{serial}/touch", include_in_schema=False)
async def stream_touch(serial: str, payload: TouchEventPayload) -> JSONResponse:
    from engine.core.device_stream.manager import device_manager
    from engine.core import device_busy

    try:
        with device_busy.interaction(serial):
            device_manager.send_touch_event(
                serial, payload.action, payload.x, payload.y, method=payload.method
            )
        return JSONResponse(content={"status": "ok"})
    except ValueError as exc:
        return JSONResponse(status_code=409 if device_busy.is_busy(serial) else 404, content={"detail": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.post("/api/stream/devices/{serial}/reconnect")
@router.post("/stream/devices/{serial}/reconnect", include_in_schema=False)
async def stream_reconnect(serial: str) -> JSONResponse:
    from engine.core.device_stream.manager import device_manager

    device_manager.reconnect_device(serial)
    return JSONResponse(content={"status": "reconnecting", "serial": serial})


@router.get("/api/stream/devices/{serial}/stream-profile")
@router.get("/stream/devices/{serial}/stream-profile", include_in_schema=False)
async def stream_profile_get(serial: str) -> JSONResponse:
    from engine.core.device_stream.manager import get_stream_profile_state

    return JSONResponse(content={"serial": serial, **get_stream_profile_state(serial)})


@router.post("/api/stream/devices/{serial}/stream-profile")
@router.post("/stream/devices/{serial}/stream-profile", include_in_schema=False)
async def stream_profile_set(serial: str, payload: StreamProfilePayload) -> JSONResponse:
    from engine.core.device_stream.manager import (
        device_manager,
        get_stream_profile_state,
        set_stream_profile_override,
    )

    profile = str(payload.profile or "").strip().lower()
    try:
        set_stream_profile_override(
            serial, None if profile in ("", "auto", "default") else profile
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    restarting = bool(device_manager.get_device(serial))
    if restarting:
        device_manager.reconnect_device(serial)
    return JSONResponse(
        content={
            "serial": serial,
            "status": "reconnecting" if restarting else "saved",
            **get_stream_profile_state(serial),
        }
    )


# --------------------------------------------------------------------------- #
#  iOS MJPEG 实时画面（engine.core.ios_mjpeg 中继）
#  协议对齐 backend/device_stream/router.py：每条二进制消息一帧完整 JPEG；
#  关闭码 4005 = WDA/MJPEG 上游不可用（reason 含 P1005_WDA_UNAVAILABLE），4004 = 非法设备，
#  4000 = 内部错误。?token= 查询参数容忍忽略（本地端点不鉴权）。
# --------------------------------------------------------------------------- #

IOS_MJPEG_HTTP_UNSUPPORTED_MESSAGE = (
    "客户端的 iOS 实时画面走 WebSocket（/ws/ios-mjpeg/{serial}），不提供 HTTP multipart 流"
)


def _ws_close_reason(text: str) -> str:
    """WebSocket 关闭原因上限 123 字节（中文按字符截断会超限，close 帧直接发不出去）。"""
    return str(text).encode("utf-8")[:120].decode("utf-8", "ignore")


@router.get("/api/stream/ios-mjpeg/{serial}")
@router.get("/stream/ios-mjpeg/{serial}", include_in_schema=False)
async def ios_mjpeg_stream_http(serial: str, token: Optional[str] = Query(default=None)) -> JSONResponse:
    """GET /api/stream/ios-mjpeg/{serial}：前端只用 WS 版，这里显式 503（不透传云端）。"""
    return JSONResponse(status_code=503, content={"detail": IOS_MJPEG_HTTP_UNSUPPORTED_MESSAGE})


async def _wait_ws_disconnect(websocket: WebSocket) -> None:
    """客户端不发消息：receive 只在断开时返回。没有新帧时也要能及时发现断开并退订。"""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


@router.websocket("/ws/ios-mjpeg/{serial}")
async def ios_mjpeg_stream_ws(websocket: WebSocket, serial: str) -> None:
    """推送 iOS 实时画面（WDA MJPEG）。首次连接可能要等 WDA 冷启动（30~90s），期间页面显示连接中。"""
    from engine.core import ios_mjpeg

    await websocket.accept()
    try:
        hub = ios_mjpeg.mjpeg_hub(serial)
    except ValueError as exc:
        try:
            await websocket.close(code=4004, reason=_ws_close_reason(str(exc)))
        except Exception:
            pass
        return

    logger.info("iOS 实时画面 WebSocket 已连接: %s", serial)
    queue = hub.subscribe(asyncio.get_running_loop())
    disconnected = asyncio.create_task(_wait_ws_disconnect(websocket))
    try:
        while True:
            next_frame = asyncio.ensure_future(queue.get())
            await asyncio.wait({next_frame, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected.done():
                next_frame.cancel()
                return
            frame = next_frame.result()
            if frame is None:
                reason = f"P1005_WDA_UNAVAILABLE: {hub.last_error}"
                await websocket.close(code=4005, reason=_ws_close_reason(reason))
                return
            await websocket.send_bytes(frame)
    except Exception as exc:  # noqa: BLE001 - 发送失败多为客户端已断开
        logger.info("iOS 实时画面 WebSocket 结束: serial=%s %s", serial, exc)
    finally:
        disconnected.cancel()
        hub.unsubscribe(queue)


@router.websocket("/ws/scrcpy/{serial}")
async def scrcpy_video_stream(websocket: WebSocket, serial: str) -> None:
    """推送 H.264 原始视频流（协议与 backend/device_stream/router.py 一致）。"""
    import asyncio

    from engine.core.device_stream.manager import device_manager

    await websocket.accept()
    logger.info("视频流 WebSocket 已连接: %s", serial)
    try:
        generator = await asyncio.to_thread(device_manager.get_video_generator, serial)
        loop = asyncio.get_running_loop()

        def _next_chunk():
            try:
                return next(generator)
            except StopIteration:
                return None

        while True:
            # 视频泵走专用线程池（不与 dump/interact/touch 共用默认池）：
            # next() 等帧可能长占调用线程，混用默认池会把本地设备 I/O 饿死。
            chunk = await loop.run_in_executor(VIDEO_PUMP_EXECUTOR, _next_chunk)
            if chunk is None:
                break
            await websocket.send_bytes(chunk)
    except Exception as exc:
        logger.warning("视频流异常: serial=%s %s", serial, exc)
        code = 4004 if isinstance(exc, ValueError) else 4000
        reason = _ws_close_reason(str(exc)) if isinstance(exc, ValueError) else "内部错误"
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass
        return
    try:
        await websocket.close()
    except Exception:
        pass


async def _to_thread(func, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)
