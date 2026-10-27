"""App 包安装本地化测试（P2：云端下载 + 本地 adb install）。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from engine import config_store, presence
from engine.app import create_app
from engine.core import package_service
from engine.core.cloud_bridge import CloudBridge
from engine.core.package_service import PackageInstallError, install_package_local

APK_BYTES = b"PK\x03\x04-fake-apk-"


class _PackageCloud:
    """云端安装包数据面 mock：list / download。"""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                owner.requests.append({"method": "GET", "path": self.path})
                if self.path.startswith("/api/packages/") and self.path.endswith("/download"):
                    self.send_response(200)
                    self.send_header("content-type", "application/vnd.android.package-archive")
                    self.send_header("content-length", str(len(APK_BYTES)))
                    self.end_headers()
                    self.wfile.write(APK_BYTES)
                    return
                if self.path.startswith("/api/packages"):
                    self._send_json(
                        200,
                        {
                            "total": 1,
                            "items": [
                                {
                                    "id": 9,
                                    "platform": "android",
                                    "app_name": "商城",
                                    "package_name": "com.demo.mall",
                                    "version_name": "1.2.3",
                                    "version_code": "123",
                                    "file_path": "packages/mall.apk",
                                    "file_size": len(APK_BYTES),
                                    "is_latest": True,
                                    "upload_time": "2026-10-26T00:00:00",
                                }
                            ],
                        },
                    )
                    return
                self._send_json(404, {"detail": "Not found"})

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
def package_cloud():
    server = _PackageCloud()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def bridge(package_cloud):
    return CloudBridge(upstream=package_cloud.base_url, auth_headers={"Authorization": "Bearer t"})


@pytest.fixture()
def adb(monkeypatch):
    state: Dict[str, Any] = {"calls": [], "outputs": ["Success\n"]}

    def _run(args, timeout=120):
        state["calls"].append(list(args))
        outputs = state["outputs"]
        index = len(state["calls"]) - 1
        return outputs[index] if index < len(outputs) else outputs[-1]

    monkeypatch.setattr(package_service, "_run_adb_command", _run)
    # adb 可执行路径经 engine.adb_scan 唯一入口解析（固定假路径，断言 argv 贯通）
    monkeypatch.setattr("engine.adb_scan.find_adb_path", lambda explicit=None: "/resolved/adb")
    return state


def test_install_local_success_shape(bridge, adb):
    result = install_package_local(bridge, 9, "USB1")

    # 形状对齐 backend/api/packages.py install_app_package_to_device
    assert result["success"] is True
    assert result["msg"] == "商城 v1.2.3 安装成功"
    assert result["package_id"] == 9
    assert result["platform"] == "android"
    assert result["installer"] == "adb"
    assert result["package_name"] == "com.demo.mall"
    assert result["version_name"] == "1.2.3"
    assert result["version_code"] == "123"
    assert result["retried_after_uninstall"] is False

    # 本地 adb install（-r -t -d，对齐平台 install_flags）；adb 走 adb_scan 解析路径
    install_calls = [c for c in adb["calls"] if "install" in c]
    assert len(install_calls) == 1
    assert install_calls[0][:4] == ["/resolved/adb", "-s", "USB1", "install"]
    assert set(install_calls[0][4:7]) == {"-r", "-t", "-d"}


def test_install_downgrade_uninstall_retry(bridge, adb):
    adb["outputs"] = ["Failure [INSTALL_FAILED_VERSION_DOWNGRADE]\n", "Success\n"]
    result = install_package_local(bridge, 9, "USB1")
    assert result["success"] is True
    assert result["retried_after_uninstall"] is True
    uninstalls = [c for c in adb["calls"] if "uninstall" in c]
    assert uninstalls and uninstalls[0][-1] == "com.demo.mall"


def test_install_failure_maps_detail(bridge, adb):
    adb["outputs"] = ["Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]\n"]
    with pytest.raises(PackageInstallError) as exc:
        install_package_local(bridge, 9, "USB1")
    assert exc.value.status_code == 500
    assert "INSTALL_FAILED_INSUFFICIENT_STORAGE" in str(exc.value)


def test_install_ios_package_returns_501(bridge, monkeypatch):
    monkeypatch.setattr(
        CloudBridge,
        "fetch_package",
        lambda self, pid: {"id": pid, "platform": "ios", "app_name": "iOS应用", "package_name": "a.b", "version_name": "1.0", "version_code": "1"},
    )
    with pytest.raises(PackageInstallError) as exc:
        install_package_local(bridge, 9, "ios:udid")
    assert exc.value.status_code == 501


def test_install_missing_package_404(bridge, monkeypatch):
    monkeypatch.setattr(
        CloudBridge, "fetch_package", lambda self, pid: (_ for _ in ()).throw(FileNotFoundError("nope"))
    )
    with pytest.raises(PackageInstallError) as exc:
        install_package_local(bridge, 999, "USB1")
    assert exc.value.status_code == 404


def test_install_requires_serial(bridge, adb):
    with pytest.raises(PackageInstallError) as exc:
        install_package_local(bridge, 9, "")
    assert exc.value.status_code == 400


def test_install_missing_adb_maps_to_503_with_chinese_hint(bridge, monkeypatch):
    """G3：adb 解析失败不得裸抛 FileNotFoundError（会变 500 且指引文案到不了用户）。

    必须映射为 PackageInstallError(统一中文指引, 503)，与原裸 "adb" 回退时期
    _run_adb_command 的 FileNotFoundError → 503 行为保持一致。
    """
    monkeypatch.setattr("engine.adb_scan.find_adb_path", lambda explicit=None: None)
    with pytest.raises(PackageInstallError) as exc:
        install_package_local(bridge, 9, "USB1")
    assert exc.value.status_code == 503
    assert "未检测到 adb" in str(exc.value)
    assert "platform-tools" in str(exc.value)
    assert "内置 adb" in str(exc.value)


def test_install_endpoint_missing_adb_returns_503_detail(client, monkeypatch):
    monkeypatch.setattr("engine.adb_scan.find_adb_path", lambda explicit=None: None)
    resp = client.post("/api/packages/9/install", json={"serial": "USB1"})
    assert resp.status_code == 503
    detail = str(resp.json().get("detail", ""))
    assert "未检测到 adb" in detail
    assert "platform-tools" in detail


# --------------------------- 端点（TestClient） --------------------------- #

@pytest.fixture()
def client(tmp_path, monkeypatch, package_cloud):
    monkeypatch.setenv("AUTODROID_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.delenv("AUTODROID_FRONTEND_DIST", raising=False)
    monkeypatch.setenv("AUTODROID_CLIENT_VERSION", "9.9.9-test")
    config_store.reset_for_tests()
    presence.reset_for_tests()
    config_store.set_upstream(package_cloud.base_url)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    presence.reset_for_tests()
    config_store.reset_for_tests()


def test_install_endpoint_is_local_not_proxied(client, package_cloud, adb):
    resp = client.post("/api/packages/9/install", json={"serial": "USB1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["msg"] == "商城 v1.2.3 安装成功"
    assert data["installer"] == "adb"
    # 下载走云端数据面，安装端点本身不被反代
    assert any(r["path"].endswith("/download") for r in package_cloud.requests)
    assert not [r for r in package_cloud.requests if r["path"].endswith("/install")]
    assert adb["calls"] and adb["calls"][0][0] == "/resolved/adb"
