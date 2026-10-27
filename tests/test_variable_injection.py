"""全局变量执行注入核对（P2）。

核对点：
- 拉取端点为 GET /api/environments/{env_id}/variables，形状 GlobalVariableRead（key/value）
- 单例执行（run_case_local）把 env 变量 + 用例变量注入步骤（{{VAR}} 渲染）
- 预检预测导出变量名（exported_variables）
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from engine.core import runner_service
from engine.core.cloud_bridge import CloudBridge


class _EnvCloud:
    def __init__(self) -> None:
        self.paths: List[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):  # noqa: N802
                owner.paths.append(self.path)
                body = json.dumps(
                    [
                        # GlobalVariableRead 形状（含富字段，key/value 之外应被忽略）
                        {"id": 1, "env_id": 3, "key": "ENV_K", "value": "env-v",
                         "is_secret": False, "description": None, "created_at": "t"},
                        {"id": 2, "env_id": 3, "key": "SECRET_K", "value": None,
                         "is_secret": True, "description": "s", "created_at": "t"},
                    ]
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def env_cloud():
    server = _EnvCloud()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_fetch_env_variables_endpoint_and_shape(env_cloud):
    bridge = CloudBridge(upstream=env_cloud.base_url)
    variables = bridge.fetch_env_variables(3)
    assert env_cloud.paths == ["/api/environments/3/variables"]
    assert variables == {"ENV_K": "env-v", "SECRET_K": ""}
    assert bridge.fetch_env_variables(None) == {}


class _VarBridge:
    def __init__(self):
        self.step_payloads: List[tuple] = []
        self.created: List[Dict[str, Any]] = []
        self.finished: List[tuple] = []

    def fetch_case(self, case_id):
        return {"id": case_id, "name": "变量用例", "variables": [{"key": "USER", "value": "u1"}]}

    def fetch_case_steps(self, case_id):
        return [
            {
                "order": 1,
                "action": "input",
                "args": {"selector": "#name", "value": "{{ENV_K}}-{{USER}}"},
                "platform_overrides": {"android": {"selector": "#name", "by": "id"}},
                "execute_on": ["android"],
                "timeout": 10,
                "error_strategy": "ABORT",
                "description": "输入 {{USER}}",
                "retry_count": 0,
            }
        ]

    def fetch_env_variables(self, env_id):
        return {"ENV_K": "env-v"}

    def fetch_app_key_mapping(self):
        return {}

    def ensure_template_local(self, path):
        return path

    def create_run(self, payload):
        self.created.append(payload)
        return {"id": 1, "run_id": payload.get("run_id")}

    def report_steps(self, run_key, payload):
        self.step_payloads.append((run_key, payload))
        return {}

    def finish_run(self, run_key, payload):
        self.finished.append((run_key, payload))
        return {"report_id": "rep"}


def test_run_case_local_injects_env_and_case_variables(monkeypatch):
    recorded: List[Dict[str, Any]] = []

    class CaptureRunner:
        def __init__(self, platform=None, device_id=None, abort_event=None, **kwargs):
            self.runtime_variables = {}

            class _D:
                def screenshot(self):
                    return b"png"

            self.driver = _D()

        def run_step(self, step):
            recorded.append(step)
            return {"status": "PASS", "duration": 0.01, "attempts": 1, "step": step}

        def disconnect(self):
            pass

    monkeypatch.setattr(runner_service, "CrossPlatformRunner", CaptureRunner)
    monkeypatch.setattr(
        runner_service.recording_service, "localize_step_images", lambda step, ensure: step
    )

    bridge = _VarBridge()
    messages: List[Dict[str, Any]] = []

    async def emit(message):
        messages.append(message)

    result = asyncio.run(
        runner_service.run_case_local(
            case_id=5, env_id=3, device_serial="USB1", emit=emit, bridge=bridge
        )
    )
    assert result["success"] is True
    dumped = json.dumps(recorded[0], ensure_ascii=False)
    # env 变量 + 用例变量已注入；不再残留 {{VAR}} 模板
    assert "env-v" in dumped and "u1" in dumped
    assert "{{ENV_K}}" not in dumped and "{{USER}}" not in dumped

    # 上报的 exported_variables 含 env + case 变量（场景链路复用同一口径）
    _, steps_payload = bridge.step_payloads[0]
    assert steps_payload["exported_variables"]["ENV_K"] == "env-v"
    assert steps_payload["exported_variables"]["USER"] == "u1"


def test_precheck_predicts_exported_variable_names():
    bridge = _VarBridge()

    def fake_fetch_steps(case_id):
        return [
            {
                "order": 1,
                "action": "extract_by_ocr",
                "args": {"output_var": "OCR_OUT", "region": "#ocr-area"},
                "execute_on": ["android"],
                "timeout": 10,
                "error_strategy": "ABORT",
                "description": "提取",
                "retry_count": 0,
            }
        ]

    bridge.fetch_case_steps = fake_fetch_steps
    payload = runner_service.precheck_for_device(bridge, 5, "USB1", 3)
    assert payload["ok"] is True
    exported = payload["exported_variables"]
    assert exported["ENV_K"] == "env-v"
    assert exported["USER"] == "u1"
    assert "OCR_OUT" in exported
