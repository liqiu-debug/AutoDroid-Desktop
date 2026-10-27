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
- POST /api/fastbot/run            Fastbot 本地轻量档采集 + client-fastbot 上报
- GET  /api/stream/ios-mjpeg/{serial}  iOS MJPEG 本地拦截（显式降级，T7）
- WS   /ws/ios-mjpeg/{serial}          同上（close 4000 + 降级原因，T7）
- POST /api/compatibility/replay-preflight  回放预检本地化（P4-2）
- POST /api/compatibility/runs              installed_replay 本地执行 + client-compatibility 上报（P4-2）
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from engine.core import recording_service
from engine.core.cloud_bridge import CloudBridge, auth_headers_from_request, template_cache_dir
from engine.core.recording_service import RecordingError

logger = logging.getLogger(__name__)

router = APIRouter()


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
    x: int,
    y: int,
    serial: Optional[str] = None,
) -> JSONResponse:
    """审查模式：返回指定坐标处的最佳元素和定位策略。"""
    try:
        payload = await _to_thread(recording_service.inspect_device, x, y, serial)
    except RecordingError as exc:
        return _error_response(exc)
    return JSONResponse(content=payload)


@router.post("/api/device/interact")
@router.post("/device/interact", include_in_schema=False)
async def interact_with_device(request: Request, payload: InteractionPayload) -> Response:
    """交互模式：分析元素 → 执行点击 → 等待 UI 稳定 → 返回新状态。"""
    try:
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
        )
    except RecordingError as exc:
        return _error_response(exc)
    return _json_response_with_optional_gzip(request, result)


@router.post("/api/device/execute_step")
@router.post("/device/execute_step", include_in_schema=False)
async def execute_single_step(request: Request, payload: SingleStepPayload) -> Response:
    """执行单个步骤并返回最新 UI 快照（本地跨端 Runner）。"""
    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
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

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
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
    return JSONResponse(content={"image_path": image_path})


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


@router.post("/api/cases/{case_id}/run")
@router.post("/cases/{case_id}/run", include_in_schema=False)
async def run_test_case(
    request: Request,
    case_id: int,
    env_id: Optional[int] = None,
    device_serial: Optional[str] = None,
) -> JSONResponse:
    """Quick run a test case in background（本地执行 + client-runs 上报）。"""
    from engine.core import runner_service

    if not device_serial:
        return JSONResponse(status_code=400, content={"detail": "请选择执行设备"})

    import uuid

    run_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    runner_service.start_background_case_run(
        case_id=case_id,
        env_id=env_id,
        device_serial=device_serial,
        run_id=run_id,
        batch_id=batch_id,
        auth_headers=auth_headers_from_request(request),
    )
    return JSONResponse(content=_run_payload(case_id, batch_id, run_id, device_serial, "Execution started"))


@router.post("/api/cases/{case_id}/run-batch")
@router.post("/cases/{case_id}/run-batch", include_in_schema=False)
async def run_test_case_batch(
    request: Request,
    case_id: int,
    payload: CaseRunBatchPayload = Body(default_factory=CaseRunBatchPayload),
) -> JSONResponse:
    """Run one case on multiple devices as one batch（形状对齐 cases.py run_test_case_batch）。"""
    import uuid

    from engine.core import runner_service

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))

    requested_serials = payload.device_serials or [None]
    runnable_serials: List[str] = []
    blocked_prechecks: List[Dict[str, Any]] = []
    for serial in requested_serials:
        if not serial:
            blocked_prechecks.append({"device_serial": serial, "reason": "请选择执行设备"})
            continue
        try:
            precheck = await _to_thread(
                runner_service.precheck_for_device, bridge, case_id, serial, payload.env_id
            )
        except Exception as exc:
            detail = str(exc)
            if "404" in detail:
                return JSONResponse(status_code=404, content={"detail": "用例不存在"})
            blocked_prechecks.append({"device_serial": serial, "reason": detail})
            continue
        if precheck.get("ok"):
            runnable_serials.append(serial)
        else:
            blocked_prechecks.append(
                {
                    "device_serial": serial,
                    "reason": "case precheck failed",
                    "precheck": precheck,
                }
            )

    if not runnable_serials:
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": "C1001_CASE_PRECHECK_FAILED",
                    "message": "case precheck failed for all selected devices",
                    "items": blocked_prechecks,
                }
            },
        )

    batch_id = str(uuid.uuid4())
    run_ids: List[str] = []
    runs_payload: List[Dict[str, Any]] = []
    for serial in runnable_serials:
        run_id = str(uuid.uuid4())
        run_ids.append(run_id)
        runs_payload.append({"run_id": run_id, "device_serial": serial, "queued": False, "queue_position": None})
        runner_service.start_background_case_run(
            case_id=case_id,
            env_id=payload.env_id,
            device_serial=serial,
            run_id=run_id,
            batch_id=batch_id,
            auth_headers=auth_headers_from_request(request),
        )

    return JSONResponse(
        content={
            "message": "Batch execution started",
            "case_id": case_id,
            "batch_id": batch_id,
            "run_ids": run_ids,
            "device_serials": runnable_serials,
            "blocked_prechecks": blocked_prechecks,
            "runs": runs_payload,
            "queued_count": 0,
        }
    )


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

    await runner_service.run_case_via_websocket(
        websocket,
        case_id,
        env_id,
        device_serial,
        auth_headers_from_request(websocket),
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

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
    try:
        payload = await _to_thread(runner_service.precheck_for_device, bridge, case_id, device_serial, env_id)
    except Exception as exc:
        detail = str(exc)
        if "404" in detail:
            return JSONResponse(status_code=404, content={"detail": "用例不存在"})
        return JSONResponse(status_code=502, content={"detail": f"预检失败: {detail}"})
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

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
    try:
        payload = await _to_thread(
            scenario_service.precheck_scenario_for_device, bridge, scenario_id, device_serial, env_id
        )
    except Exception as exc:
        detail = str(exc)
        if "404" in detail:
            return JSONResponse(status_code=404, content={"detail": "场景不存在"})
        return JSONResponse(status_code=502, content={"detail": f"预检失败: {detail}"})
    return JSONResponse(content=payload)


@router.post("/api/scenarios/{scenario_id}/run")
@router.post("/scenarios/{scenario_id}/run", include_in_schema=False)
async def run_scenario_batch(
    request: Request,
    scenario_id: int,
    payload: ScenarioRunBatchPayload = Body(default_factory=ScenarioRunBatchPayload),
) -> JSONResponse:
    """场景在多设备上的批量执行（响应形状对齐 scenarios.run_scenario_api）。"""
    import uuid as _uuid

    from engine.core import scenario_service

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
    try:
        await _to_thread(bridge.fetch_scenario, scenario_id)
    except Exception as exc:
        detail = str(exc)
        if "404" in detail:
            return JSONResponse(status_code=404, content={"detail": "场景不存在"})
        return JSONResponse(status_code=502, content={"detail": f"拉取场景失败: {detail}"})

    requested_serials = payload.device_serials or [None]
    runnable_serials: List[str] = []
    blocked_prechecks: List[Dict[str, Any]] = []
    for serial in requested_serials:
        if not serial:
            blocked_prechecks.append({"device_serial": serial, "reason": "请选择执行设备"})
            continue
        try:
            precheck = await _to_thread(
                scenario_service.precheck_scenario_for_device, bridge, scenario_id, serial, payload.env_id
            )
        except Exception as exc:
            blocked_prechecks.append({"device_serial": serial, "reason": str(exc)})
            continue
        if precheck.get("ok"):
            runnable_serials.append(serial)
        else:
            blocked_prechecks.append(
                {
                    "device_serial": serial,
                    "reason": scenario_service.summarize_precheck_failure(precheck),
                    "precheck": precheck,
                }
            )

    if not runnable_serials:
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": "S1001_SCENARIO_PRECHECK_FAILED",
                    "message": "scenario precheck failed for all selected devices",
                    "items": blocked_prechecks,
                }
            },
        )

    batch_id = str(_uuid.uuid4())
    execution_ids: List[Any] = []
    runs_payload: List[Dict[str, Any]] = []
    for serial in runnable_serials:
        run_id = str(_uuid.uuid4())
        # 先在云端建执行记录（kind=scenario），拿到 execution_id 后随响应返回
        # （形状对齐 scenarios.run_scenario_api 的 runs[]/execution_ids）。
        context_key: Optional[Any] = None
        try:
            created = await _to_thread(
                bridge.create_run,
                {
                    "run_id": run_id,
                    "kind": "scenario",
                    "case_id": None,
                    "scenario_id": int(scenario_id),
                    "device_serial": serial,
                    "platform": None,
                    "run_source": "platform",
                    "started_at": datetime.now().isoformat(),
                    "name": None,
                    "env_id": payload.env_id,
                },
            )
            context_key = (created or {}).get("id") or None
        except Exception as exc:
            logger.warning("client-runs 预创建失败（本地继续执行）: %s", exc)
        # S6：仅有效 id 入 execution_ids（平台契约 List[int]，不得出现 null）
        if context_key is not None:
            execution_ids.append(context_key)
        runs_payload.append(
            {
                "execution_id": context_key,
                "device_serial": serial,
                "queued": False,
                "queue_position": None,
            }
        )
        scenario_service.start_background_scenario_run(
            scenario_id=scenario_id,
            env_id=payload.env_id,
            device_serial=serial,
            run_id=run_id,
            batch_id=batch_id,
            auth_headers=auth_headers_from_request(request),
            preset_run_key=context_key,
        )

    return JSONResponse(
        content={
            "message": "Batch execution started",
            "batch_id": batch_id,
            "execution_ids": execution_ids,
            "blocked_prechecks": blocked_prechecks,
            "runs": runs_payload,
            "queued_count": 0,
        }
    )


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

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
    try:
        result = await _to_thread(
            package_service.install_package_local, bridge, package_id, payload.serial
        )
    except package_service.PackageInstallError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
    return JSONResponse(content=result)


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

    轻量档：本机 Monkey/logcat/性能采集 + client-fastbot 上报；读接口
    （/api/fastbot/tasks、/reports/{id}）仍走反代云端展示。
    enable_jank_frame_monitor / enable_local_replay 请求降级（summary.degraded 标注）。
    """
    from engine.core import fastbot_service
    from engine.core.device_stream.manager import device_manager

    device_serial = str(payload.device_serial or "").strip()
    if not device_manager.get_device(device_serial):
        return JSONResponse(status_code=400, content={"detail": "设备不在线或未就绪"})
    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
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
#  兼容性「升级后链路回放」（P4-2 按可行边界）
#  - POST /api/compatibility/replay-preflight  预检本地化（设备快照本机 adb +
#    冻结链路云端只读，形状对齐 CompatibilityReplayPreflightRead）
#  - POST /api/compatibility/runs             installed_replay 本地执行 + 上报
#    （云端 client-compatibility 建档 → 冻结链路本地回放 → report 收尾，P4-2 收尾）；
#    其余执行模式（多设备矩阵/对比）保留云端 → 原样反代
# --------------------------------------------------------------------------- #

class CompatibilityReplayPreflightPayload(BaseModel):
    inspection_run_id: int
    branch_key: str
    device_serial: str
    max_chains: int = 20


@router.post("/api/compatibility/replay-preflight")
@router.post("/compatibility/replay-preflight", include_in_schema=False)
async def compatibility_replay_preflight_local(
    request: Request, payload: CompatibilityReplayPreflightPayload
) -> JSONResponse:
    """回放预检本地化：设备安装包快照在本机读取（云端无本机设备，透传会误报未安装）。"""
    from engine.core import compatibility_service

    bridge = CloudBridge(auth_headers=auth_headers_from_request(request))
    if not bridge.available:
        return JSONResponse(
            status_code=503,
            content={"detail": "未配置上游服务器，无法拉取巡检来源数据，请先登录后重试"},
        )
    try:
        result = await _to_thread(
            compatibility_service.local_replay_preflight,
            bridge,
            inspection_run_id=payload.inspection_run_id,
            branch_key=payload.branch_key,
            device_serial=payload.device_serial,
            max_chains=payload.max_chains,
        )
    except compatibility_service.CompatibilityPreflightError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception as exc:
        logger.warning("回放预检本地化失败: %s", exc)
        return JSONResponse(status_code=502, content={"detail": f"回放预检失败: {exc}"})
    return JSONResponse(content=result)


def _cloud_error_response(exc: Exception) -> Optional[JSONResponse]:
    """httpx 上游错误 → 透传状态码与 detail（含 dict 形状的预检 blockers）。"""
    import httpx

    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    status_code = int(exc.response.status_code or 502)
    try:
        detail = exc.response.json().get("detail")
    except Exception:
        detail = None
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail if detail is not None else str(exc)},
    )


async def _create_local_replay_run(request: Request, body: Dict[str, Any]) -> Response:
    """installed_replay 创建：云端建档（client-compatibility）→ 本地后台回放 + 上报。"""
    from engine.core import compatibility_service

    auth_headers = auth_headers_from_request(request)
    bridge = CloudBridge(auth_headers=auth_headers)
    if not bridge.available:
        return JSONResponse(
            status_code=503,
            content={"detail": "未配置上游服务器（请先在登录页填写服务器地址）"},
        )
    device_serials = [
        str(item or "").strip()
        for item in (body.get("device_serials") or [])
        if str(item or "").strip()
    ]
    if not device_serials:
        return JSONResponse(status_code=400, content={"detail": "请选择执行设备"})

    create_payload = dict(body)
    create_payload["device_serials"] = device_serials
    create_payload["execution_mode"] = "installed_replay"
    try:
        installed = await _to_thread(
            compatibility_service.build_client_installed_package,
            bridge,
            inspection_run_id=int(body.get("inspection_run_id") or 0),
            device_serial=device_serials[0],
        )
        create_payload["installed_package"] = installed
        run = await _to_thread(bridge.create_client_compatibility_run, create_payload)
    except compatibility_service.CompatibilityPreflightError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception as exc:
        cloud_error = _cloud_error_response(exc)
        if cloud_error is not None:
            return cloud_error
        logger.warning("兼容性回放 run 创建失败: %s", exc)
        return JSONResponse(status_code=502, content={"detail": f"回放任务创建失败: {exc}"})

    # 后台逐链路本地回放 + report 收尾（前端轮询 GET /api/compatibility/runs/{id}）
    compatibility_service.start_background_replay_run(run, auth_headers=auth_headers)
    logger.info(
        "兼容性回放本地执行已启动: run=%s devices=%s",
        run.get("id"),
        device_serials,
    )
    return JSONResponse(content=run)


@router.post("/api/compatibility/runs")
@router.post("/compatibility/runs", include_in_schema=False)
async def compatibility_create_run_local(request: Request) -> Response:
    """执行类端点本地拦截：installed_replay 本地执行 + 上报；其余模式原样反代。"""
    from engine.proxy import proxy_request

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    execution_mode = str(body.get("execution_mode") or "").strip().lower()
    if execution_mode == "installed_replay":
        return await _create_local_replay_run(request, body)
    if execution_mode in {"comparison", "page_compare", "device_compare"}:
        logger.info("兼容性对比模式保留云端，原样反代: execution_mode=%s", execution_mode)

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

    try:
        device_manager.send_touch_event(
            serial, payload.action, payload.x, payload.y, method=payload.method
        )
        return JSONResponse(content={"status": "ok"})
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
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
#  iOS MJPEG 实时画面（T7：本地拦截，显式降级）
#  协议形状对齐 backend/device_stream/router.py（WS /ws/ios-mjpeg/{serial} +
#  GET /api/stream/ios-mjpeg/{serial}，?token= 查询参数容忍忽略）。
#  本地引擎暂无 WDA/MJPEG relay 能力（遗留 C：iOS 全链路），不再透传云端
#  （云端无本机设备只会得到误导性失败），改为显式降级提示——与 T6 同风格。
# --------------------------------------------------------------------------- #

IOS_MJPEG_DEGRADED_MESSAGE = (
    "客户端模式暂不支持 iOS 实时画面（本地化 MJPEG 链路后续版本提供）"
)


@router.get("/api/stream/ios-mjpeg/{serial}")
@router.get("/stream/ios-mjpeg/{serial}", include_in_schema=False)
async def ios_mjpeg_stream_http(serial: str, token: Optional[str] = Query(default=None)) -> JSONResponse:
    """GET /api/stream/ios-mjpeg/{serial} 本地拦截：显式降级（形状对齐 router.py 错误面）。

    - 503：上游/能力不可用（对齐 IOSMjpegStreamError 的 503 口径）；
    - ?token= 兼容 <img src> 查询通道（本地端点不鉴权，直接容忍）。
    """
    logger.info("iOS MJPEG HTTP 本地拦截（降级）: serial=%s", serial)
    return JSONResponse(status_code=503, content={"detail": IOS_MJPEG_DEGRADED_MESSAGE})


@router.websocket("/ws/ios-mjpeg/{serial}")
async def ios_mjpeg_stream_ws(websocket: WebSocket, serial: str) -> None:
    """WS /ws/ios-mjpeg/{serial} 本地拦截：accept 后以 4000 关闭并给出降级原因。

    关闭码对齐 router.py 语义（4000=服务侧不可用/内部错误，前端不自动重连）；
    reason 显式说明客户端模式边界（不带 P1005_WDA_UNAVAILABLE，避免误导到
    「设备中心启动 WDA」路径）。
    """
    logger.info("iOS MJPEG WS 本地拦截（降级）: serial=%s", serial)
    await websocket.accept()
    try:
        await websocket.close(code=4000, reason=IOS_MJPEG_DEGRADED_MESSAGE[:120])
    except Exception:
        pass


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
            chunk = await loop.run_in_executor(None, _next_chunk)
            if chunk is None:
                break
            await websocket.send_bytes(chunk)
    except Exception as exc:
        logger.warning("视频流异常: serial=%s %s", serial, exc)
        code = 4004 if isinstance(exc, ValueError) else 4000
        reason = str(exc)[:120] if isinstance(exc, ValueError) else "内部错误"
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
