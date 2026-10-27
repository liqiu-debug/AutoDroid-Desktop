"""集成 smoke：真实引擎进程 + mock upstream，跑通 login → sync → 执行上报 → 模板上传。

流程（契约要求的最终验证项）：
1. 起 mock upstream（python http.server）：/auth/token、/api/devices/、
   /api/client-devices/register|heartbeat|logout、/api/client-runs/**、/api/client-templates、
   /api/client-tasks、/api/scenarios|cases|settings（定时任务触发链路）
2. 起引擎子进程（只绑 127.0.0.1，心跳周期调成 1s）
3. login：POST /api/auth/token（X-AutoDroid-Server → mock）→ mock 收到 /auth/token，
   且 X-AutoDroid-Server 不得透传给上游；响应含 access_token
4. sync：POST /api/devices/sync → 回源 mock /api/devices/，响应 {synced, online, offline, devices}
   register 线路（sync 内部逐台上报）：POST /api/client-devices/register 带 client_instance_id
5. heartbeat：登录后引擎心跳循环 POST /api/client-devices/heartbeat（Authorization: Bearer …）
6. 单例执行上报：LocalRunContext（runner 同款上报链路）create → steps → finish，
   模板图经 /api/client-templates 上传；finish 显式 status 且返回 report_id
7. 场景执行上报：LocalRunContext(kind=scenario, scenario_id) create → steps（键含 alias，
   同一 case 多 alias 分批上报）→ finish（kind=scenario 形状：create 无 case_id）
8. 定时任务触发一次执行上报（P3-1 集成）：登录后引擎拉取 GET /api/client-tasks →
   本地 APScheduler 按 ONCE 触发 → precheck → run_scenario_local →
   client-runs create(kind=scenario, name=「定时任务: …」)→steps→finish 全链路上报
9. logout：SIGTERM 停引擎 → mock 收到 /api/client-devices/logout

设备扫描：经 PYTHONPATH sitecustomize 桩把 scan_devices 钉成一台假设备
（仅测试进程注入，不改业务代码），使 sync register 与心跳 serials 确定可断言。
定时任务执行的设备 I/O 同理：桩掉 uiautomator2.connect（sleep 步骤不触设备），
业务链路（调度→预检→执行编排→上报）全部走真实代码。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # 允许 `python tests/smoke_mock_upstream.py` 直接运行（import engine）
ACCESS_TOKEN = "smoke-access-token"


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D102 — 静默访问日志
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self, body: bytes) -> None:
        server = self.server
        server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        self._record(body)
        path = self.path.split("?", 1)[0]
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            payload = {}

        if path == "/auth/token":
            self._send_json(200, {"access_token": ACCESS_TOKEN, "token_type": "bearer", "user_id": 1})
            return
        if path == "/api/client-devices/register":
            self._send_json(
                200,
                {
                    "id": 1,
                    "serial": payload.get("serial"),
                    "platform": payload.get("platform", "android"),
                    "model": payload.get("model", "Unknown"),
                    "brand": payload.get("brand", ""),
                    "android_version": payload.get("android_version", ""),
                    "os_version": payload.get("os_version", ""),
                    "resolution": payload.get("resolution", ""),
                    "status": "IDLE",
                    "owner_id": 1,
                    "client_instance_id": payload.get("client_instance_id"),
                    "last_seen_at": "2026-10-26T00:00:00",
                    "created_at": "2026-10-26T00:00:00",
                    "updated_at": "2026-10-26T00:00:00",
                },
            )
            return
        if path == "/api/client-devices/heartbeat":
            serials = payload.get("serials") or []
            self._send_json(200, {"updated": len(serials)})
            return
        if path == "/api/client-devices/logout":
            self._send_json(200, {"updated": 1})
            return
        if path == "/api/client-runs/":
            # 逐条递增云端 id（定时任务触发的执行上报与手工上报并发，恒 7 会串线）
            assigned = self.server.next_run_id  # type: ignore[attr-defined]
            self.server.next_run_id = assigned + 1  # type: ignore[attr-defined]
            client_run_id = str(payload.get("run_id") or "")
            if client_run_id:
                self.server.run_key_map[client_run_id] = assigned  # type: ignore[attr-defined]
            self._send_json(
                200,
                {
                    "run_id": payload.get("run_id"),
                    "id": assigned,
                    "execution_id": assigned,
                    "status": "RUNNING",
                    "created": True,
                    "start_time": "2026-10-26T00:00:00",
                },
            )
            return
        if path.startswith("/api/client-runs/") and path.endswith("/steps"):
            steps = payload.get("steps") or []
            self._send_json(
                200,
                {
                    "run_id": "smoke-run",
                    "execution_id": 7,
                    "status": "RUNNING",
                    "accepted_steps": len(steps),
                    "total_steps": len(steps),
                },
            )
            return
        if path.startswith("/api/client-runs/") and path.endswith("/finish"):
            self._send_json(
                200,
                {
                    "run_id": "smoke-run",
                    "execution_id": 7,
                    "status": payload.get("status") or "PASS",
                    "report_id": "rep-smoke",
                    "duration": payload.get("duration"),
                    "finished": True,
                },
            )
            return
        if path == "/api/client-templates":
            self._send_json(200, {"image_path": "static/images/element_smokehash.png"})
            return
        self._send_json(404, {"detail": f"mock 未实现: POST {path}"})

    def do_GET(self):  # noqa: N802
        self._record(b"")
        path = self.path.split("?", 1)[0]
        if path == "/api/devices/":
            self._send_json(
                200,
                [
                    {
                        "id": 1,
                        "serial": "SMOKE-MOCK-1",
                        "platform": "android",
                        "model": "MockPhone",
                        "brand": "MOCK",
                        "status": "IDLE",
                        "owner_id": 1,
                        "client_instance_id": "smoke-instance",
                        "last_seen_at": "2026-10-26T00:00:00",
                        "created_at": "2026-10-26T00:00:00",
                        "updated_at": "2026-10-26T00:00:00",
                    }
                ],
            )
            return
        # ---- 定时任务本地触发链路（P3-1） ---- #
        if path == "/api/client-tasks":
            # ONCE 任务 8s 后触发（serve 时刻计算，避开过期过滤；晚于步骤 6/7 断言窗口）
            run_date = (datetime.now() + timedelta(seconds=8)).isoformat()
            self._send_json(
                200,
                {
                    "total": 1,
                    "items": [
                        {
                            "id": 9,
                            "name": "定时冒烟任务",
                            "scenario_id": 3,
                            "device_serial": "SMOKE-LOCAL-1",
                            "device_serials": ["SMOKE-LOCAL-1"],
                            "strategy": "ONCE",
                            "strategy_config": {
                                "run_date": run_date,
                                "_task_type": "ui",
                                "env_id": None,
                            },
                            "is_active": True,
                            "enable_notification": False,
                            "next_run_time": None,
                            "user_id": 1,
                            "created_at": "2026-10-26T00:00:00",
                            "updated_at": "2026-10-26T00:00:00",
                        }
                    ],
                },
            )
            return
        if path == "/api/scenarios/3":
            self._send_json(200, {"id": 3, "name": "冒烟定时场景"})
            return
        if path == "/api/scenarios/3/steps":
            self._send_json(
                200,
                [{"id": 31, "scenario_id": 3, "order": 1, "case_id": 5, "alias": "定时步骤"}],
            )
            return
        if path == "/api/cases/5":
            self._send_json(200, {"id": 5, "name": "定时用例", "variables": [], "steps": []})
            return
        if path == "/api/cases/5/steps":
            # sleep 步骤：预检 PASS 且执行不触设备（设备 I/O 已桩）
            self._send_json(
                200,
                [
                    {
                        "id": 51,
                        "case_id": 5,
                        "order": 1,
                        "action": "sleep",
                        "args": {"seconds": 0},
                        "value": "0",
                        "execute_on": ["android"],
                        "timeout": 5,
                        "error_strategy": "ABORT",
                        "retry_count": 0,
                        "description": "定时冒烟等待",
                    }
                ],
            )
            return
        if path == "/api/settings/":
            self._send_json(200, [])
            return
        self._send_json(404, {"detail": f"mock 未实现: GET {path}"})


class MockUpstream:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.next_run_id = 7  # type: ignore[attr-defined]
        self.server.run_key_map = {}  # type: ignore[attr-defined]  # client run_id -> 云端 id
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def cloud_run_id(self, client_run_id: str) -> Optional[int]:
        return self.server.run_key_map.get(str(client_run_id))  # type: ignore[attr-defined]

    def find(self, method: str, path_suffix: str) -> List[Dict[str, Any]]:
        return [
            item
            for item in self.server.requests  # type: ignore[attr-defined]
            if item["method"] == method and item["path"].split("?", 1)[0].endswith(path_suffix)
        ]

    def find_run_reports(self, cloud_run_id: int, kind: str) -> List[Dict[str, Any]]:
        """按云端 run id 精确取 steps/finish 上报（steps/finish 载荷不带 run_id，id 在 URL）。"""
        suffix = f"/{int(cloud_run_id)}/{kind}"
        return [
            item
            for item in self.server.requests  # type: ignore[attr-defined]
            if item["method"] == "POST" and item["path"].split("?", 1)[0].endswith(suffix)
        ]

    def wait_for(self, method: str, path_suffix: str, timeout: float = 10.0) -> List[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.find(method, path_suffix)
            if found:
                return found
            time.sleep(0.2)
        return []

    def wait_for_body(
        self, method: str, path_suffix: str, needle: bytes, timeout: float = 30.0
    ) -> List[Dict[str, Any]]:
        """按请求体内容等待（定时任务触发的执行上报 run_id 预先未知，按名称定位）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = [item for item in self.find(method, path_suffix) if needle in item["body"]]
            if found:
                return found
            time.sleep(0.2)
        return []


def _request(method: str, url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            return exc.code, {"raw": raw}


def _write_scan_stub() -> str:
    """sitecustomize 桩：让引擎子进程的 scan_devices 确定性返回一台假设备。

    sync 的逐台 register 与心跳 serials 都依赖本机 adb 扫描；真机/无 adb 环境
    不可复现，故在子进程启动前注入（仅测试进程 PYTHONPATH，不改业务代码）。
    同理桩掉 uiautomator2.connect：定时任务触发的场景执行走 sleep 步骤，
    不触设备 I/O，但 AndroidDriver 构造仍要求 u2.connect 成功。
    """
    stub_dir = tempfile.mkdtemp(prefix="autodroid-mock-stub-")
    stub_code = """
def _fake_scan(adb=None):
    return [{
        "platform": "android",
        "serial": "SMOKE-FAKE-1",
        "model": "MockPhone",
        "brand": "MOCK",
        "android_version": "14",
        "os_version": "14",
        "resolution": "1080x2400",
        "connection_type": "usb",
    }]

try:
    import engine.adb_scan as _adb_scan
    _adb_scan.scan_devices = _fake_scan
    # T4：定时触发前按本机 adb 清单过滤设备——桩掉 local_serials 与假设备对齐，
    # 否则真机 adb 清单不含 SMOKE-FAKE-1，定时任务被本地过滤（不建 run）。
    _adb_scan.local_serials = lambda adb=None: {"SMOKE-FAKE-1", "SMOKE-LOCAL-1"}
except Exception:
    pass

try:
    import uiautomator2 as _u2

    class _FakeU2Device(object):
        info = {"sdkInt": 34, "displayWidth": 1080, "displayHeight": 2400}

        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    _u2.connect = lambda *args, **kwargs: _FakeU2Device()
except Exception:
    pass
"""
    with open(os.path.join(stub_dir, "sitecustomize.py"), "w", encoding="utf-8") as fp:
        fp.write(stub_code)
    return stub_dir


def main() -> int:
    mock = MockUpstream()
    mock_url = mock.start()
    engine_env = dict(os.environ)
    userdata = tempfile.mkdtemp(prefix="autodroid-mock-smoke-")
    engine_env["AUTODROID_USER_DATA"] = userdata
    engine_env["AUTODROID_CLIENT_VERSION"] = "mock-smoke"
    engine_env["AUTODROID_HEARTBEAT_INTERVAL"] = "1"
    engine_env.pop("AUTODROID_FRONTEND_DIST", None)
    stub_dir = _write_scan_stub()
    engine_env["PYTHONPATH"] = stub_dir + os.pathsep + engine_env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "engine"],
        cwd=ROOT,
        env=engine_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        port = None
        for _ in range(100):
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("AUTODROID_ENGINE_PORT="):
                port = int(line.strip().split("=", 1)[1])
                break
        if not port:
            print("FAIL: 未捕获 AUTODROID_ENGINE_PORT")
            return 1
        base = f"http://127.0.0.1:{port}"

        # ---- 1. login（X-AutoDroid-Server 选 upstream，不透传给上游） ----
        status, token_payload = _request(
            "POST",
            f"{base}/api/auth/token",
            data=b"username=smoke&password=smoke",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-AutoDroid-Server": mock_url,
            },
        )
        assert status == 200, (status, token_payload)
        assert token_payload.get("access_token") == ACCESS_TOKEN, token_payload
        login_calls = mock.find("POST", "/auth/token")
        assert login_calls, "mock 未收到 /auth/token"
        for call in login_calls:
            assert "x-autodroid-server" not in call["headers"], "X-AutoDroid-Server 泄露给上游"
        print(f"OK: login -> access_token（mock /auth/token ×{len(login_calls)}，无 X-AutoDroid-Server 透传）")

        auth_headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

        # ---- 2. sync（回源设备列表 + register 上报线路） ----
        status, sync_payload = _request("POST", f"{base}/api/devices/sync", data=b"", headers=auth_headers)
        assert status == 200, (status, sync_payload)
        for key in ("synced", "online", "offline", "devices"):
            assert key in sync_payload, (key, sync_payload)
        assert isinstance(sync_payload["devices"], list) and sync_payload["devices"], sync_payload
        assert mock.find("GET", "/api/devices/"), "sync 未回源 /api/devices/"
        assert sync_payload["synced"] >= 1, sync_payload
        sync_register_calls = [
            call
            for call in mock.find("POST", "/api/client-devices/register")
            if b"SMOKE-FAKE-1" in call["body"]
        ]
        assert sync_register_calls, "sync 未对本机扫描设备调用 register"
        sync_register_payload = json.loads(sync_register_calls[-1]["body"].decode("utf-8"))
        assert sync_register_payload.get("client_instance_id"), sync_register_payload
        print(f"OK: sync -> {sync_payload['synced']} synced / {sync_payload['online']} online / {len(sync_payload['devices'])} devices（register 带 client_instance_id）")

        register_body = json.dumps(
            {
                "platform": "android",
                "serial": "SMOKE-LOCAL-1",
                "model": "MockPhone",
                "brand": "MOCK",
                "android_version": "14",
                "os_version": "14",
                "resolution": "1080x2400",
                "client_instance_id": "smoke-instance",
            }
        ).encode("utf-8")
        status, register_payload = _request(
            "POST",
            f"{base}/api/client-devices/register",
            data=register_body,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert status == 200, (status, register_payload)
        assert register_payload.get("serial") == "SMOKE-LOCAL-1", register_payload
        assert register_payload.get("client_instance_id") == "smoke-instance", register_payload
        register_calls = mock.find("POST", "/api/client-devices/register")
        assert register_calls, "mock 未收到 register"
        for call in register_calls:
            assert "x-autodroid-server" not in call["headers"], "X-AutoDroid-Server 泄露给上游"
        print(f"OK: register -> DeviceRead 形状（mock /api/client-devices/register ×{len(register_calls)}）")

        # ---- 3. heartbeat（登录后心跳循环，周期 1s） ----
        heartbeat_calls = mock.wait_for("POST", "/api/client-devices/heartbeat", timeout=15.0)
        assert heartbeat_calls, "登录后心跳循环未上报 /api/client-devices/heartbeat"
        call = heartbeat_calls[-1]
        assert call["headers"].get("authorization") == f"Bearer {ACCESS_TOKEN}", call["headers"]
        heartbeat_payload = json.loads(call["body"].decode("utf-8") or "{}")
        assert isinstance(heartbeat_payload.get("serials"), list) and heartbeat_payload["serials"], heartbeat_payload
        assert "SMOKE-FAKE-1" in heartbeat_payload["serials"], heartbeat_payload
        print(f"OK: heartbeat -> {heartbeat_payload}（Authorization: Bearer …）")

        # ---- 4. client-runs 上报（runner 同款 LocalRunContext 链路） ----
        from engine.core.cloud_bridge import CloudBridge
        from engine.core.runner_service import LocalRunContext

        bridge = CloudBridge(upstream=mock_url, auth_headers=auth_headers)

        async def _noop_emit(_message):
            return None

        ctx = LocalRunContext(
            case_id=5,
            device_serial="SMOKE-LOCAL-1",
            env_id=None,
            run_id="smoke-run-id",
            batch_id="smoke-batch-id",
            emit=_noop_emit,
            bridge=bridge,
        )
        ctx.create_cloud_run("登录用例", "android")
        assert ctx.run_key == mock.cloud_run_id("smoke-run-id"), ctx.run_key
        ctx.report_step_results(
            "登录用例",
            True,
            [
                {
                    "order": 1,
                    "action": "click",
                    "report_status": "PASS",
                    "duration": 0.2,
                    "attempts": 1,
                    "error": None,
                    "error_code": None,
                    "screenshot_base64": None,
                    "output": None,
                }
            ],
            {"USER": "u1"},
        )
        report_id = ctx.finish(True, False, 1.23, notify=True)
        assert report_id == "rep-smoke", report_id

        create_calls = mock.find("POST", "/api/client-runs/")
        steps_calls = mock.find("POST", "/steps")
        finish_calls = mock.find("POST", "/finish")
        assert create_calls and steps_calls and finish_calls, (create_calls, steps_calls, finish_calls)
        create_payload = json.loads(create_calls[-1]["body"].decode("utf-8"))
        assert create_payload.get("run_id") == "smoke-run-id", create_payload
        assert create_payload.get("run_source") == "platform", create_payload
        steps_payload = json.loads(steps_calls[-1]["body"].decode("utf-8"))
        assert steps_payload.get("name") == "登录用例", steps_payload
        assert steps_payload["steps"][0]["status"] == "PASS", steps_payload
        finish_payload = json.loads(finish_calls[-1]["body"].decode("utf-8"))
        assert finish_payload.get("status") == "PASS", finish_payload
        assert finish_payload.get("notify") is True, finish_payload
        for call in create_calls + steps_calls + finish_calls:
            assert call["headers"].get("authorization") == f"Bearer {ACCESS_TOKEN}", call["headers"]
        print("OK: client-runs 上报 create→steps→finish（run_source=platform, status=PASS, report_id=rep-smoke）")

        image_path = bridge.upload_template(b"\x89PNG\r\n\x1a\n" + b"0" * 32, "element_smoke.png")
        assert image_path.startswith("static/images/element_") and image_path.endswith(".png"), image_path
        assert mock.find("POST", "/api/client-templates"), "mock 未收到 /api/client-templates"
        print(f"OK: client-templates 上传 -> {image_path}")

        # ---- 5. 场景执行上报（kind=scenario；键含 alias，同 case 多 alias 分批） ----
        scenario_ctx = LocalRunContext(
            case_id=None,
            device_serial="SMOKE-LOCAL-1",
            env_id=None,
            run_id="smoke-scenario-run-id",
            batch_id="smoke-scenario-batch-id",
            emit=_noop_emit,
            bridge=bridge,
            kind="scenario",
            scenario_id=3,
        )
        scenario_ctx.create_cloud_run("冒烟场景", "android")
        assert scenario_ctx.run_key == mock.cloud_run_id("smoke-scenario-run-id"), scenario_ctx.run_key
        scenario_step = {
            "order": 1,
            "action": "click",
            "report_status": "PASS",
            "duration": 0.1,
            "attempts": 1,
            "error": None,
            "error_code": None,
            "screenshot_base64": None,
            "output": None,
        }
        # 同一 case 两个 alias 分批上报（平台 _case_state_key 含 alias，分开成条）
        scenario_ctx.report_step_results(
            "登录用例", True, [scenario_step], {"USER": "u1"}, case_id=5, alias="步骤 A"
        )
        scenario_ctx.report_step_results(
            "登录用例", True, [dict(scenario_step, order=2)], {"USER": "u2"}, case_id=5, alias="步骤 B"
        )
        scenario_report_id = scenario_ctx.finish(True, False, 2.5, notify=True)
        assert scenario_report_id == "rep-smoke", scenario_report_id

        # create_calls 已含单例那条；场景 steps/finish 按云端 run id 精确匹配
        #（定时任务上报与手工上报并发，不得用 [-1] 位置断言）
        scenario_cloud_id = mock.cloud_run_id("smoke-scenario-run-id")
        assert scenario_cloud_id is not None, mock.server.run_key_map  # type: ignore[attr-defined]
        all_create_calls = mock.find("POST", "/api/client-runs/")
        assert len(all_create_calls) >= 2, all_create_calls
        scenario_creates = [
            json.loads(call["body"].decode("utf-8"))
            for call in all_create_calls
            if b"smoke-scenario-run-id" in call["body"]
        ]
        assert len(scenario_creates) == 1, scenario_creates
        scenario_create_payload = scenario_creates[0]
        assert scenario_create_payload.get("kind") == "scenario", scenario_create_payload
        assert scenario_create_payload.get("scenario_id") == 3, scenario_create_payload
        assert scenario_create_payload.get("case_id") in (None, ""), scenario_create_payload
        assert scenario_create_payload.get("run_id") == "smoke-scenario-run-id", scenario_create_payload
        assert scenario_create_payload.get("run_source") == "platform", scenario_create_payload

        all_steps_calls = mock.find("POST", "/steps")
        assert len(all_steps_calls) >= 3, all_steps_calls  # 单例 1 + 场景 2
        scenario_steps_payloads = [
            json.loads(call["body"].decode("utf-8"))
            for call in mock.find_run_reports(scenario_cloud_id, "steps")
        ]
        assert [p.get("alias") for p in scenario_steps_payloads] == ["步骤 A", "步骤 B"], scenario_steps_payloads
        for payload in scenario_steps_payloads:
            assert payload.get("case_id") == 5, payload
            assert payload.get("name") == "登录用例", payload
            assert payload.get("success") is True, payload
            assert payload["steps"][0]["status"] == "PASS", payload

        all_finish_calls = mock.find("POST", "/finish")
        assert len(all_finish_calls) >= 2, all_finish_calls
        scenario_finishes = [
            json.loads(call["body"].decode("utf-8"))
            for call in mock.find_run_reports(scenario_cloud_id, "finish")
        ]
        assert len(scenario_finishes) == 1, scenario_finishes
        scenario_finish_payload = scenario_finishes[0]
        assert scenario_finish_payload.get("status") == "PASS", scenario_finish_payload
        assert scenario_finish_payload.get("aborted") is False, scenario_finish_payload
        print(
            "OK: 场景执行上报 create(kind=scenario)→steps(2×alias)→finish"
            f"（status={scenario_finish_payload.get('status')}, report_id={scenario_report_id}）"
        )

        # ---- 6. 定时任务触发一次执行上报（P3-1 集成链路） ----
        # 登录后引擎拉取 /api/client-tasks → 本地 APScheduler ONCE 触发 →
        # precheck → run_scenario_local → client-runs create→steps→finish 上报。
        assert mock.find("GET", "/api/client-tasks"), "引擎未拉取 /api/client-tasks"
        timer_create_calls = mock.wait_for_body(
            "POST", "/api/client-runs/", "定时任务:".encode("utf-8"), timeout=30.0
        )
        assert timer_create_calls, "定时任务触发后未上报 client-runs create"
        timer_create_payload = json.loads(timer_create_calls[-1]["body"].decode("utf-8"))
        timer_run_id = str(timer_create_payload.get("run_id") or "")
        timer_cloud_id = mock.cloud_run_id(timer_run_id)
        assert timer_create_payload.get("name") == "定时任务: 定时冒烟任务", timer_create_payload
        assert timer_create_payload.get("kind") == "scenario", timer_create_payload
        assert timer_create_payload.get("scenario_id") == 3, timer_create_payload
        assert timer_create_payload.get("run_source") == "platform", timer_create_payload
        assert timer_create_calls[-1]["headers"].get("authorization") == f"Bearer {ACCESS_TOKEN}"

        deadline = time.time() + 15.0
        timer_steps_payloads: List[Dict[str, Any]] = []
        timer_finishes: List[Dict[str, Any]] = []
        while time.time() < deadline:
            timer_steps_payloads = [
                json.loads(call["body"].decode("utf-8"))
                for call in mock.find_run_reports(timer_cloud_id, "steps")
            ]
            timer_finishes = [
                json.loads(call["body"].decode("utf-8"))
                for call in mock.find_run_reports(timer_cloud_id, "finish")
            ]
            if timer_steps_payloads and timer_finishes:
                break
            time.sleep(0.2)
        assert timer_steps_payloads, "定时任务执行未上报 steps"
        timer_steps_payload = timer_steps_payloads[-1]
        assert timer_steps_payload.get("alias") == "定时步骤", timer_steps_payload
        assert timer_steps_payload.get("name") == "定时用例", timer_steps_payload
        assert timer_steps_payload.get("success") is True, timer_steps_payload
        assert timer_finishes, "定时任务执行未上报 finish"
        timer_finish_payload = timer_finishes[-1]
        assert timer_finish_payload.get("status") == "PASS", timer_finish_payload
        print(
            "OK: 定时任务触发一次执行上报（client-tasks 拉取→ONCE 触发→precheck→本地执行→"
            f"create(name=定时任务:…)→steps→finish status={timer_finish_payload.get('status')}）"
        )

        # ---- 7. logout（SIGTERM → lifespan shutdown） ----

        # ---- 6. logout（SIGTERM → lifespan shutdown） ----
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        logout_calls = mock.wait_for("POST", "/api/client-devices/logout", timeout=8.0)
        assert logout_calls, "引擎退出时未上报 /api/client-devices/logout"
        print(f"OK: logout -> mock /api/client-devices/logout ×{len(logout_calls)}")

        print("MOCK UPSTREAM SMOKE PASSED")
        return 0
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        mock.stop()


if __name__ == "__main__":
    sys.exit(main())
