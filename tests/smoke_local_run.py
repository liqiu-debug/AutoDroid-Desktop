"""本地录制/执行链路 smoke：真实引擎进程 + 无设备依赖的端点。

流程：起引擎子进程 →
1. GET  /api/client/info                 → 200 clientMode
2. POST /api/device/crop_template        → 503（R6：无 upstream 无法上云，阻断提示重试）
3. POST /api/cases/{id}/run 无设备        → 400 请选择执行设备
4. POST /api/cases/{id}/run-batch 无 upstream → 400 C1001_CASE_PRECHECK_FAILED
5. GET  /api/device/dump?include_* =false → 本地拦截（无设备时 500 detail，而非 502 未配置上游）
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _png_b64() -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color=(9, 9, 9)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _request(method: str, url: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body or "{}")
        except ValueError:
            return exc.code, {"raw": body}


def main() -> int:
    env = dict(os.environ)
    env["AUTODROID_USER_DATA"] = tempfile.mkdtemp(prefix="autodroid-local-smoke-")
    env["AUTODROID_CLIENT_VERSION"] = "local-smoke"
    env.pop("AUTODROID_FRONTEND_DIST", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine"],
        cwd=str(ROOT),
        env=env,
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

        status, info = _request("GET", f"{base}/api/client/info")
        assert status == 200 and info.get("clientMode") is True, (status, info)
        print("OK: /api/client/info", info)

        status, payload = _request(
            "POST",
            f"{base}/api/device/crop_template",
            {"screenshot_base64": _png_b64(), "x1": 1, "y1": 1, "x2": 6, "y2": 6},
        )
        # R6：无 upstream 时模板无法上云，必须阻断（不再回退悬空本地文件名）
        assert status == 503, (status, payload)
        assert "image_path" not in payload, payload
        print("OK: /api/device/crop_template 无 upstream 阻断 503")

        status, payload = _request("POST", f"{base}/api/cases/5/run")
        assert status == 400 and "请选择执行设备" in str(payload.get("detail")), (status, payload)
        print("OK: /api/cases/5/run 无设备 400")

        status, payload = _request(
            "POST", f"{base}/api/cases/5/run-batch", {"device_serials": ["smoke-dev"]}
        )
        assert status == 400, (status, payload)
        detail = payload.get("detail")
        assert isinstance(detail, dict) and detail.get("code") == "C1001_CASE_PRECHECK_FAILED", (status, payload)
        print("OK: /api/cases/5/run-batch 预检失败 400 + C1001")

        status, payload = _request("GET", f"{base}/api/device/dump?include_hierarchy=false&include_screenshot=false")
        assert status in (200, 500), (status, payload)
        if status == 500:
            # 无设备/无 u2 连接：本地拦截返回 500 detail，而不是 502「未配置上游服务器」
            assert "未配置上游服务器" not in str(payload.get("detail")), (status, payload)
        print("OK: /api/device/dump 本地拦截 status=", status)

        print("LOCAL RUN SMOKE PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
