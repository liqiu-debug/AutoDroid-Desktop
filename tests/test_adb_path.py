"""adb 路径解析（engine.adb_scan.find_adb_path 唯一入口）优先级与注入测试。

优先级契约：显式参数 > env AUTODROID_ADB > env ADBUTILS_ADB_PATH
> 内置 platform-tools（打包态 resourcesPath/platform-tools；开发态 assets/platform-tools）
> PATH > 常见 SDK 安装路径兜底；解析结果注入 ADBUTILS_ADB_PATH 统一 adbutils/uiautomator2。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from engine import adb_scan


@pytest.fixture()
def clean_env(monkeypatch):
    """清空 adb 相关环境并隔离内置/SDK 候选与 PATH 探测（测试内显式铺设假 adb）。"""
    for name in (
        "AUTODROID_ADB",
        "ADBUTILS_ADB_PATH",
        "AUTODROID_ENGINE_ASSETS_DIR",
        "LOCALAPPDATA",
        "ANDROID_HOME",
        "ANDROID_SDK_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/tmp/autodroid-no-home")
    monkeypatch.setattr(adb_scan, "_bundled_platform_tools_candidates", lambda: [])
    monkeypatch.setattr(adb_scan, "_sdk_candidates", lambda: [])
    monkeypatch.setattr(adb_scan.shutil, "which", lambda name: None)
    return monkeypatch


def _fake_adb(tmp_path: Path, name: str = "adb") -> str:
    fake = tmp_path / name
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    return str(fake)


# ==================== 优先级顺序 ====================


def test_explicit_argument_wins_over_env(tmp_path, clean_env):
    explicit = _fake_adb(tmp_path, "explicit-adb")
    clean_env.setenv("AUTODROID_ADB", _fake_adb(tmp_path, "env-adb"))
    assert adb_scan.find_adb_path(explicit) == explicit


def test_autodroid_adb_env_wins_over_adbutils_env(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    clean_env.setenv("ADBUTILS_ADB_PATH", _fake_adb(tmp_path, "adbutils-adb"))
    assert adb_scan.find_adb_path() == winner


def test_env_wins_over_bundled(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    clean_env.setattr(
        adb_scan, "_bundled_platform_tools_candidates", lambda: [_fake_adb(tmp_path, "bundled-adb")]
    )
    assert adb_scan.find_adb_path() == winner


def test_bundled_wins_over_path(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "bundled-adb")
    clean_env.setattr(adb_scan, "_bundled_platform_tools_candidates", lambda: [winner])
    clean_env.setattr(adb_scan.shutil, "which", lambda name: _fake_adb(tmp_path, "path-adb"))
    assert adb_scan.find_adb_path() == winner


def test_path_wins_over_sdk_heuristic(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "path-adb")
    clean_env.setattr(adb_scan.shutil, "which", lambda name: winner)
    clean_env.setattr(adb_scan, "_sdk_candidates", lambda: [_fake_adb(tmp_path, "sdk-adb")])
    assert adb_scan.find_adb_path() == winner


def test_sdk_heuristic_is_last_resort(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "sdk-adb")
    clean_env.setattr(adb_scan, "_sdk_candidates", lambda: [winner])
    assert adb_scan.find_adb_path() == winner


# ==================== 无效候选跳过 ====================


def test_missing_env_value_falls_through(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "adbutils-adb")
    clean_env.setenv("AUTODROID_ADB", str(tmp_path / "nope-missing"))
    clean_env.setenv("ADBUTILS_ADB_PATH", winner)
    assert adb_scan.find_adb_path() == winner


def test_missing_explicit_falls_through(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    assert adb_scan.find_adb_path(str(tmp_path / "nope-missing")) == winner


def test_not_found_returns_none(clean_env):
    assert adb_scan.find_adb_path() is None


def test_resolve_adb_binary_missing_raises_chinese_hint(clean_env):
    """G3：解析不到不再回退裸 "adb"，抛统一中文指引（含安装/内置 adb 指引文案）。"""
    with pytest.raises(FileNotFoundError) as excinfo:
        adb_scan.resolve_adb_binary()
    message = str(excinfo.value)
    assert "未检测到 adb" in message
    assert "platform-tools" in message
    assert "内置 adb" in message


def test_resolve_adb_binary_uses_resolution(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    assert adb_scan.resolve_adb_binary() == winner


# ==================== 内置 platform-tools 候选 ====================


def test_bundled_candidates_include_assets_dir_sibling(tmp_path, monkeypatch):
    for name in ("AUTODROID_ADB", "ADBUTILS_ADB_PATH"):
        monkeypatch.delenv(name, raising=False)
    resources = tmp_path / "Resources"
    (resources / "assets").mkdir(parents=True)
    monkeypatch.setenv("AUTODROID_ENGINE_ASSETS_DIR", str(resources / "assets"))
    exe_name = "adb.exe" if os.name == "nt" else "adb"
    candidates = adb_scan._bundled_platform_tools_candidates()
    # 打包态：resourcesPath/assets 同级的 platform-tools/adb[.exe]（extraResources 映射）
    assert str(resources / "platform-tools" / exe_name) in candidates


def test_bundled_candidates_include_repo_assets(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTODROID_ENGINE_ASSETS_DIR", raising=False)
    exe_name = "adb.exe" if os.name == "nt" else "adb"
    candidates = adb_scan._bundled_platform_tools_candidates()
    # 开发态：分平台目录 assets/platform-tools/<darwin|windows>/（fetch 脚本产物）优先，
    # 兼容旧扁平布局 assets/platform-tools/
    repo_root = Path(adb_scan.__file__).resolve().parent.parent
    per_platform = str(
        repo_root / "assets" / "platform-tools" / adb_scan._platform_tools_dir_name() / exe_name
    )
    flat = str(repo_root / "assets" / "platform-tools" / exe_name)
    assert per_platform in candidates
    assert flat in candidates
    assert candidates.index(per_platform) < candidates.index(flat)


def test_bundled_beats_path_via_assets_dir(tmp_path, monkeypatch):
    """打包态锚点（AUTODROID_ENGINE_ASSETS_DIR 同级）真实生效，且优先于 PATH。"""
    for name in ("AUTODROID_ADB", "ADBUTILS_ADB_PATH"):
        monkeypatch.delenv(name, raising=False)
    resources = tmp_path / "Resources"
    bundled_dir = resources / "platform-tools"
    bundled_dir.mkdir(parents=True)
    exe_name = "adb.exe" if os.name == "nt" else "adb"
    winner = _fake_adb(bundled_dir, exe_name)
    monkeypatch.setenv("AUTODROID_ENGINE_ASSETS_DIR", str(resources / "assets"))
    monkeypatch.setattr(adb_scan.shutil, "which", lambda name: _fake_adb(tmp_path, "path-adb"))
    assert adb_scan.find_adb_path() == winner


# ==================== adbutils 注入与 start-server 自愈 ====================


def test_apply_adb_path_injects_adbutils_env(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    assert adb_scan.apply_adb_path() == winner
    assert os.environ["ADBUTILS_ADB_PATH"] == winner


def test_apply_adb_path_overwrites_stale_adbutils_env(tmp_path, clean_env):
    """解析结果是唯一权威：陈旧的 ADBUTILS_ADB_PATH 被统一覆盖（全链路一个 adb）。"""
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    clean_env.setenv("ADBUTILS_ADB_PATH", _fake_adb(tmp_path, "stale-adb"))
    assert adb_scan.apply_adb_path() == winner
    assert os.environ["ADBUTILS_ADB_PATH"] == winner


def test_ensure_server_started_self_heals_once(tmp_path, clean_env):
    winner = _fake_adb(tmp_path, "env-adb")
    clean_env.setenv("AUTODROID_ADB", winner)
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))

        class _Proc:
            returncode = 0

        return _Proc()

    clean_env.setattr(adb_scan.subprocess, "run", _run)
    clean_env.setattr(adb_scan, "_server_ready", False)

    assert adb_scan.ensure_server_started() is True
    assert adb_scan.ensure_server_started() is True
    assert calls == [[winner, "start-server"]]
    assert os.environ["ADBUTILS_ADB_PATH"] == winner


def test_ensure_server_started_without_adb(clean_env):
    clean_env.setattr(adb_scan, "_server_ready", False)
    assert adb_scan.ensure_server_started() is False


# ==================== G3：SDK 兜底启发式与缺失指引 ====================


def test_sdk_candidates_include_env_sdk_roots(tmp_path, monkeypatch):
    """ANDROID_HOME / ANDROID_SDK_ROOT 的 platform-tools 进兜底候选（用户显式配置优先）。"""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANDROID_HOME", os.path.join(str(tmp_path), "sdk-home"))
    monkeypatch.setenv("ANDROID_SDK_ROOT", os.path.join(str(tmp_path), "sdk-root"))
    exe_name = "adb.exe" if os.name == "nt" else "adb"
    candidates = adb_scan._sdk_candidates()
    home_sdk = os.path.join(str(tmp_path), "sdk-home", "platform-tools", exe_name)
    root_sdk = os.path.join(str(tmp_path), "sdk-root", "platform-tools", exe_name)
    assert home_sdk in candidates and root_sdk in candidates
    # env 指向的 SDK 排在猜测路径之前（ANDROID_HOME 最先）
    assert candidates[0] == home_sdk and candidates[1] == root_sdk


def test_sdk_candidates_include_home_android_platform_tools(tmp_path, monkeypatch):
    """~/Android/platform-tools（用户机器真实安装位置）进 unix 兜底候选。"""
    if os.name == "nt":
        pytest.skip("unix 兜底启发式")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    candidates = adb_scan._sdk_candidates()
    assert str(tmp_path / "Android" / "platform-tools" / "adb") in candidates


def test_find_adb_path_hits_home_android_platform_tools(tmp_path, monkeypatch):
    """真实兜底链生效：~/Android/platform-tools/adb 能被 find_adb_path 兜到。"""
    if os.name == "nt":
        pytest.skip("unix 兜底启发式")
    for name in ("AUTODROID_ADB", "ADBUTILS_ADB_PATH", "AUTODROID_ENGINE_ASSETS_DIR", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(adb_scan, "_bundled_platform_tools_candidates", lambda: [])
    monkeypatch.setattr(adb_scan.shutil, "which", lambda name: None)
    bundled_dir = tmp_path / "Android" / "platform-tools"
    bundled_dir.mkdir(parents=True)
    winner = _fake_adb(bundled_dir, "adb")
    assert adb_scan.find_adb_path() == winner


def test_scan_devices_missing_adb_logs_hint(clean_env, caplog):
    """G3：scan_devices 在 adb 缺失时日志携带统一中文指引。"""
    clean_env.setattr(adb_scan, "_server_ready", False)
    with caplog.at_level(logging.WARNING):
        assert adb_scan.scan_devices() == []
    assert any("未检测到 adb" in record.getMessage() for record in caplog.records)


def test_scan_devices_device_list_failure_logs_hint(clean_env, monkeypatch, caplog):
    """G3：设备列表获取失败时日志同样携带 adb 指引（先确认 adb 可用再排查）。"""
    import adbutils

    monkeypatch.setattr(adb_scan, "ensure_server_started", lambda adb=None: True)

    def _boom():
        raise RuntimeError("adb server 异常")

    monkeypatch.setattr(adbutils.adb, "device_list", _boom)
    with caplog.at_level(logging.WARNING):
        assert adb_scan.scan_devices() == []
    assert any("未检测到 adb" in record.getMessage() for record in caplog.records)
