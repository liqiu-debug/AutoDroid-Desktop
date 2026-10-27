# 来源：AutoDroid backend/api/recording.py（服务逻辑抽取）。
# 剥离点：DB 会话/Device 表查询改为入参注入与本机判定；iOS 录制后置（仅 Android）；
# crop_template 落盘改为 userData/templates 本地缓存（云端上传由 cloud_bridge 负责）。
"""录制与单步执行服务（桌面引擎本地设备 I/O）。

对齐 backend/api/recording.py 的语义：
- dump：截图(base64) + 层级XML + 设备信息（Android 层级/截图并行）
- inspect：坐标 → 元素 + 定位策略 + 可录制步骤
- interact：分析 → 执行操作 → 等待 UI 稳定 → 返回新 dump
- execute_step：跨端 Runner 执行单步 + 新 dump
- crop_template：手动框选裁剪模板图（本地缓存 + 云端上传由调用方完成）
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from engine.core.device_transport_metrics import device_transport_metrics
from engine.core.drivers.cross_platform_runner import TestCaseRunner as CrossPlatformRunner
from engine.core.execution_service import resolve_device_platform
from engine.core.step_contract import (
    legacy_step_to_standard,
    normalize_error_strategy,
    normalize_execute_on,
    normalize_platform_overrides,
    standard_step_to_legacy,
)
from engine.core.utils import calculate_element_from_coordinates

logger = logging.getLogger(__name__)

CLICK_IMAGE_REQUIRED_DETAIL = "添加步骤失败：当前点击区域无 Desc/Text，请使用图像点击步骤。"
CLICK_TARGET_NOT_FOUND_DETAIL = "添加步骤失败：当前点击区域未识别到可录制元素，请重试或使用图像点击步骤。"

# Android u2 连接缓存空闲回收时间；u2 Device 本身无长连接资源，仅避免无限膨胀
RECORDING_ANDROID_IDLE_TTL_SECONDS = 300.0

# dump 截图统一编码为 JPEG 的质量（与上游一致：模板裁剪素材源，不缩放）
RECORDING_SCREENSHOT_JPEG_QUALITY = 90
NETWORK_DEVICE_SERIAL_RE = re.compile(r"^[\w.\-]+:\d+$")

# 大 JSON 响应 gzip 阈值（与上游一致）
RESPONSE_GZIP_MIN_BYTES = 2048


class RecordingError(RuntimeError):
    """录制链路错误（映射为 HTTP 4xx/5xx 由路由层决定）。"""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RecordingAndroidDevicePool:
    """Android u2 连接缓存：per-serial 复用 Device 对象（语义同上游 recording.py）。"""

    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, serial: Optional[str]) -> Any:
        import uiautomator2 as u2

        device_id = str(serial or "").strip()
        now = time.time()
        with self._lock:
            self._evict_stale_locked(now)
            cached = self._entries.get(device_id)
            if cached is not None:
                self._entries[device_id] = (cached[0], now)
                return cached[0]
        device = u2.connect(device_id) if device_id else u2.connect()
        with self._lock:
            self._entries[device_id] = (device, time.time())
        return device

    def invalidate(self, serial: Optional[str], device: Optional[Any] = None) -> None:
        device_id = str(serial or "").strip()
        with self._lock:
            cached = self._entries.get(device_id)
            if cached is None:
                return
            if device is not None and cached[0] is not device:
                return
            self._entries.pop(device_id, None)

    def _evict_stale_locked(self, now: float) -> None:
        for device_id, (_, last_used) in list(self._entries.items()):
            if (now - last_used) >= RECORDING_ANDROID_IDLE_TTL_SECONDS:
                self._entries.pop(device_id, None)


_recording_android_device_pool = _RecordingAndroidDevicePool()


def _image_bytes_format(raw: bytes) -> str:
    """按魔数识别压缩图像字节流格式，未知时按 png 处理。"""
    if raw[:2] == b"\xff\xd8":
        return "jpeg"
    return "png"


def _take_screenshot_payload(device, serial: Optional[str] = None) -> Tuple[str, str]:
    """截取设备屏幕，返回 (base64, 图像格式)。语义同上游 recording.py。"""
    started_at = time.monotonic()
    try:
        image = device.screenshot()
        if isinstance(image, (bytes, bytearray)):
            raw = bytes(image)
            device_transport_metrics.record(
                str(serial or ""), "recording_screenshot", time.monotonic() - started_at, byte_count=len(raw)
            )
            return base64.b64encode(raw).decode("utf-8"), _image_bytes_format(raw)
        buffered = io.BytesIO()
        image.convert("RGB").save(buffered, format="JPEG", quality=RECORDING_SCREENSHOT_JPEG_QUALITY)
        raw = buffered.getvalue()
        device_transport_metrics.record(
            str(serial or ""), "recording_screenshot", time.monotonic() - started_at, byte_count=len(raw)
        )
        return base64.b64encode(raw).decode("utf-8"), "jpeg"
    except Exception:
        device_transport_metrics.record(
            str(serial or ""), "recording_screenshot", time.monotonic() - started_at, success=False
        )
        raise


def _take_screenshot_base64(device) -> str:
    return _take_screenshot_payload(device)[0]


def _connect_recording_device(serial: Optional[str]) -> Tuple[str, Any, Optional[Any]]:
    """剥离点：DB 判定平台 → 本机判定；iOS 录制后置。"""
    platform = resolve_device_platform(str(serial or ""))
    if platform != "android":
        raise RecordingError(
            "P2001_RECORDING_ANDROID_ONLY: iOS 设备仅支持执行，不支持录制。请切换到 Android 设备。",
            status_code=400,
        )
    device = _recording_android_device_pool.acquire(serial)
    return platform, device, None


def _invalidate_recording_device(platform: Optional[str], serial: Optional[str], device: Optional[Any]) -> None:
    if platform != "ios":
        _recording_android_device_pool.invalidate(serial, device)


def _get_device_hierarchy_xml(device, platform: str) -> str:
    return device.dump_hierarchy()


def _get_device_info_payload(device, platform: str, serial: Optional[str]) -> Dict[str, Any]:
    return device.info


def _build_hierarchy_payload(device, platform: str, serial: Optional[str] = None) -> Dict[str, str]:
    started_at = time.monotonic()
    try:
        hierarchy_xml = _get_device_hierarchy_xml(device, platform=platform)
    except Exception:
        device_transport_metrics.record(
            str(serial or ""), "hierarchy", time.monotonic() - started_at, success=False
        )
        raise
    device_transport_metrics.record(
        str(serial or ""),
        "hierarchy",
        time.monotonic() - started_at,
        byte_count=len(hierarchy_xml.encode("utf-8")) if hierarchy_xml else 0,
    )
    payload = {"hierarchy_xml": hierarchy_xml}
    if hierarchy_xml:
        payload["hierarchy_hash"] = hashlib.sha1(hierarchy_xml.encode("utf-8")).hexdigest()
    return payload


def _build_device_dump_payload(
    device,
    platform: str,
    serial: Optional[str],
    include_device_info: bool = True,
    include_hierarchy: bool = True,
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    # Android 层级（uiautomator jsonrpc）与截图（adb screencap）并行（同上游语义）
    if platform != "ios" and include_hierarchy and include_screenshot:
        with ThreadPoolExecutor(max_workers=2) as executor:
            hierarchy_future = executor.submit(_build_hierarchy_payload, device, platform, serial)
            screenshot_future = executor.submit(_take_screenshot_payload, device, serial)
            if include_device_info:
                payload["device_info"] = _get_device_info_payload(device, platform=platform, serial=serial)
            hierarchy_payload = hierarchy_future.result()
            screenshot_b64, screenshot_format = screenshot_future.result()
        payload.update(hierarchy_payload)
        payload["screenshot"] = screenshot_b64
        payload["screenshot_format"] = screenshot_format
        return payload

    if include_device_info:
        payload["device_info"] = _get_device_info_payload(device, platform=platform, serial=serial)
    if include_hierarchy:
        payload.update(_build_hierarchy_payload(device, platform=platform, serial=serial))
    if include_screenshot:
        screenshot_b64, screenshot_format = _take_screenshot_payload(device, serial)
        payload["screenshot"] = screenshot_b64
        payload["screenshot_format"] = screenshot_format
    return payload


def _get_recording_coordinate_scale(device, platform: str) -> float:
    return 1.0


def _get_recording_post_action_delay(platform: str, operation: str) -> float:
    operation_text = str(operation or "").strip().lower()
    if operation_text in ("start_app", "stop_app"):
        return 0.6
    return 0.2


def _screenshot_hash(device) -> str:
    image = device.screenshot()
    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
    else:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        raw = buf.getvalue()
    return hashlib.md5(raw).hexdigest()


def _wait_ui_stable(
    device,
    platform: str,
    operation: str,
    timeout: float = 3.0,
    serial: Optional[str] = None,
) -> None:
    """等待设备 UI 稳定（同上游语义：最小延迟 + 截图哈希轮询；网络设备跳过轮询）。"""
    min_delay = _get_recording_post_action_delay(platform, operation)
    time.sleep(min_delay)

    if NETWORK_DEVICE_SERIAL_RE.match(str(serial or "")):
        return

    poll_interval = 0.25
    deadline = time.monotonic() + (timeout - min_delay)

    prev_hash = _screenshot_hash(device)
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        curr_hash = _screenshot_hash(device)
        if curr_hash == prev_hash:
            return
        prev_hash = curr_hash


def _perform_device_operation(device, platform: str, operation: str, x: int, y: int, action_data: Optional[str]) -> None:
    operation = str(operation or "").strip().lower()
    if operation == "click":
        device.click(x, y)
        return
    if operation == "start_app":
        device.app_start(action_data)
        return
    if operation == "stop_app":
        device.app_stop(action_data)
        return
    if operation == "back":
        device.press("back")
        return
    if operation == "home":
        device.press("home")
        return
    if operation == "swipe":
        device.swipe_ext(action_data or "up", scale=0.8)
        return

    raise RecordingError(f"不支持的设备操作: {operation}", status_code=400)


def _build_step_from_inspect(inspect_res: dict, operation: str = "click") -> dict:
    element = inspect_res.get("element", {})
    strategy = inspect_res["strategy"]

    if operation == "click":
        has_semantic_locator = bool(
            str(element.get("text") or "").strip()
            or str(element.get("description") or "").strip()
        )
        if strategy not in {"text", "description"} or not has_semantic_locator:
            raise RecordingError(CLICK_IMAGE_REQUIRED_DETAIL, status_code=400)

    return {
        "action": "click" if operation == "click" else operation,
        "selector": inspect_res["selector"],
        "selector_type": strategy,
        "value": "",
        "description": "",
        "error_strategy": "ABORT"
    }


def _build_click_step_from_inspect_result(inspect_res: dict) -> dict:
    if "error" in inspect_res:
        raise RecordingError(CLICK_TARGET_NOT_FOUND_DETAIL, status_code=400)
    return _build_step_from_inspect(inspect_res, operation="click")


def crop_template_image(screenshot_base64: str, x1: int, y1: int, x2: int, y2: int) -> Tuple[bytes, str]:
    """裁剪截图区域为 PNG 模板；返回 (png_bytes, 建议文件名)。剥离点：不直接落 static/。"""
    from PIL import Image as _Image

    try:
        img = _Image.open(io.BytesIO(base64.b64decode(screenshot_base64)))
    except Exception as e:
        raise RecordingError(f"无法解析截图: {e}", status_code=400)

    width_s, height_s = img.size
    rx1 = max(0, min(x1, width_s))
    ry1 = max(0, min(y1, height_s))
    rx2 = max(0, min(x2, width_s))
    ry2 = max(0, min(y2, height_s))

    if rx2 <= rx1 or ry2 <= ry1:
        raise RecordingError(f"裁剪区域无效: [{rx1},{ry1},{rx2},{ry2}]", status_code=400)

    cropped = img.crop((rx1, ry1, rx2, ry2))
    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")
    return buffer.getvalue(), f"element_{uuid.uuid4().hex[:8]}.png"


def dump_device(
    serial: Optional[str] = None,
    include_device_info: bool = True,
    include_hierarchy: bool = True,
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    platform = None
    device = None
    try:
        platform, device, _ = _connect_recording_device(serial)
        return _build_device_dump_payload(
            device,
            platform=platform,
            serial=serial,
            include_device_info=include_device_info,
            include_hierarchy=include_hierarchy,
            include_screenshot=include_screenshot,
        )
    except RecordingError:
        raise
    except Exception as e:
        _invalidate_recording_device(platform, serial, device)
        raise RecordingError(str(e), status_code=500)


def inspect_device(x: int, y: int, serial: Optional[str] = None) -> Dict[str, Any]:
    platform = None
    device = None
    try:
        platform, device, _ = _connect_recording_device(serial)
        xml_dump = _get_device_hierarchy_xml(device, platform=platform)
        inspect_res = calculate_element_from_coordinates(
            xml_dump,
            x,
            y,
            coordinate_scale=_get_recording_coordinate_scale(device, platform),
        )

        step = _build_click_step_from_inspect_result(inspect_res)

        return {
            "step": step,
            "element": inspect_res.get("element", {}),
            "selector": inspect_res["selector"],
            "strategy": inspect_res["strategy"],
        }
    except RecordingError:
        raise
    except Exception as e:
        _invalidate_recording_device(platform, serial, device)
        raise RecordingError(str(e), status_code=500)


def interact_device(
    x: int,
    y: int,
    operation: str = "click",
    action_data: Optional[str] = None,
    xml_dump: Optional[str] = None,
    device_serial: Optional[str] = None,
    record_step: bool = True,
    include_screenshot: bool = True,
    include_device_info: bool = True,
) -> Dict[str, Any]:
    platform = None
    device = None
    try:
        platform, device, _ = _connect_recording_device(device_serial)

        inspect_res = {}
        step_info = None
        if operation == "click" and record_step:
            xml_dump = xml_dump or _get_device_hierarchy_xml(device, platform=platform)
            coordinate_scale = _get_recording_coordinate_scale(device, platform)
            inspect_res = calculate_element_from_coordinates(
                xml_dump,
                x,
                y,
                coordinate_scale=coordinate_scale,
            )

            # 前端传入的 XML 过期时用新 dump 重试（同上游语义）
            if "error" in inspect_res:
                logger.info("使用缓存XML分析失败，重新获取...")
                xml_dump = _get_device_hierarchy_xml(device, platform=platform)
                inspect_res = calculate_element_from_coordinates(
                    xml_dump,
                    x,
                    y,
                    coordinate_scale=coordinate_scale,
                )

        if operation == "click" and record_step:
            step_info = _build_click_step_from_inspect_result(inspect_res)
        elif operation != "click":
            step_info = {
                "action": operation,
                "selector": action_data or "",
                "selector_type": "text" if operation in ["start_app", "stop_app", "swipe"] else "resourceId",
                "value": "",
                "description": "",
                "error_strategy": "ABORT",
            }

        _perform_device_operation(device, platform=platform, operation=operation, x=x, y=y, action_data=action_data)

        _wait_ui_stable(
            device,
            platform=platform,
            operation=operation,
            serial=device_serial,
        )

        return {
            "step": step_info,
            "dump": _build_device_dump_payload(
                device,
                platform=platform,
                serial=device_serial,
                include_screenshot=include_screenshot,
                include_device_info=include_device_info,
            ),
        }
    except RecordingError:
        raise
    except Exception as e:
        _invalidate_recording_device(platform, device_serial, device)
        logger.exception("设备交互失败: operation=%s device_serial=%s", operation, device_serial)
        raise RecordingError(str(e), status_code=500)


def _merge_execution_variables(
    env_variables: Optional[Dict[str, str]],
    variables: Optional[List[dict]],
) -> Dict[str, Any]:
    """剥离点：DB GlobalVariable → 云端拉取的 env_variables + 入参 variables。"""
    variables_map: Dict[str, Any] = dict(env_variables or {})

    for v in variables or []:
        if not isinstance(v, dict):
            continue
        key = v.get("key")
        if key:
            variables_map[key] = v.get("value")
    return variables_map


def _normalize_single_step_for_runner(
    raw_step: Dict[str, Any],
    *,
    case_id: Optional[int],
    default_platform: str,
) -> Dict[str, Any]:
    step_data = dict(raw_step or {})
    standard_step = legacy_step_to_standard(
        step_data,
        case_id=int(case_id or 0),
        order=1,
    )

    args = step_data.get("args")
    if isinstance(args, dict):
        standard_step["args"] = dict(args)

    try:
        standard_step["execute_on"] = normalize_execute_on(
            step_data.get("execute_on") or [default_platform]
        )
    except Exception:
        standard_step["execute_on"] = [default_platform]

    try:
        standard_step["platform_overrides"] = normalize_platform_overrides(
            step_data.get("platform_overrides")
        )
    except Exception:
        pass

    if step_data.get("timeout") is not None:
        try:
            timeout_value = int(step_data.get("timeout") or 10)
            if timeout_value > 0:
                standard_step["timeout"] = timeout_value
        except Exception:
            pass
    if step_data.get("error_strategy") is not None:
        standard_step["error_strategy"] = normalize_error_strategy(step_data.get("error_strategy"))
    if step_data.get("description") is not None:
        standard_step["description"] = step_data.get("description")
    if step_data.get("value") is not None:
        standard_step["value"] = step_data.get("value")

    return standard_step


def _cross_platform_result_to_legacy_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    status = str(result.get("status") or "").upper()
    return {
        "step": standard_step_to_legacy(result.get("step") or {}),
        "success": status == "PASS",
        "error": result.get("error"),
        "duration": float(result.get("duration") or 0),
        "status": status,
        "platform": result.get("platform"),
        "device_id": result.get("device_id"),
        "output": result.get("output"),
    }


def _unwrap_runner_dump_device(driver, platform: str):
    """跨端 Runner 的 Android 驱动内部持有 u2 设备，dump/截图需使用原始设备对象。"""
    if platform == "android":
        return getattr(driver, "_device", driver)
    return driver


def localize_step_images(step: Dict[str, Any], ensure_local) -> Dict[str, Any]:
    """把步骤里的模板 image_path 归一为本地绝对路径（ensure_local: str -> str）。

    图像步骤的 image_path 形如 static/images/element_x.png（云端引用格式）；
    本地 Runner 需要真实文件：先查 userData/templates 缓存，缺失回源云端。
    """
    step_item = dict(step or {})
    args = dict(step_item.get("args") or {})
    changed = False
    for key in ("image_path", "path"):
        value = args.get(key)
        if not value:
            continue
        text = str(value)
        # 仅处理非绝对路径（云端引用）；已本地化/纯 URL 不动
        if not text.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", text):
            try:
                args[key] = ensure_local(text)
                changed = True
            except Exception as exc:
                logger.warning("模板本地化失败 %s: %s", text, exc)
    if changed or args:
        step_item["args"] = args
    return step_item


def execute_single_step(
    step: Dict[str, Any],
    device_serial: Optional[str] = None,
    case_id: Optional[int] = None,
    env_variables: Optional[Dict[str, str]] = None,
    variables: Optional[List[dict]] = None,
    include_screenshot: bool = True,
    include_device_info: bool = True,
    ensure_local=None,
) -> Dict[str, Any]:
    """执行单个步骤并返回最新 UI 快照（统一走跨端 Runner）。"""
    if not device_serial:
        raise RecordingError("请选择执行设备", status_code=400)

    platform = resolve_device_platform(str(device_serial))
    if platform != "android":
        raise RecordingError("iOS 执行在桌面引擎后置（P1 仅 Android）", status_code=400)
    variables_map = _merge_execution_variables(env_variables, variables)

    runner = None
    try:
        standard_step = _normalize_single_step_for_runner(
            step,
            case_id=case_id,
            default_platform=platform,
        )
        if ensure_local is not None:
            standard_step = localize_step_images(standard_step, ensure_local)
        runner = CrossPlatformRunner(
            platform=platform,
            device_id=device_serial,
        )
        runner.runtime_variables.update(
            {
                str(key): "" if value is None else str(value)
                for key, value in variables_map.items()
                if str(key).strip()
            }
        )
        step_result = runner.run_step(standard_step)
        dump_device_ = _unwrap_runner_dump_device(runner.driver, platform)
        _wait_ui_stable(
            dump_device_,
            platform=platform,
            operation=standard_step.get("action", ""),
            serial=device_serial,
        )
        return {
            "result": _cross_platform_result_to_legacy_payload(step_result),
            "dump": _build_device_dump_payload(
                dump_device_,
                platform=platform,
                serial=device_serial,
                include_screenshot=include_screenshot,
                include_device_info=include_device_info,
            ),
        }
    except RecordingError:
        raise
    except Exception as e:
        logger.exception("单步执行失败: device_serial=%s", device_serial)
        raise RecordingError(str(e), status_code=500)
    finally:
        if runner is not None and hasattr(runner, "disconnect"):
            try:
                runner.disconnect()
            except Exception:
                logger.exception("Runner 断开失败")
