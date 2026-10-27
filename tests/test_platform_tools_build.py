"""内置 platform-tools 构建侧契约测试（脚本语法/下载 URL/打包配置/不入库）。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DARWIN_URL = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
WINDOWS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"


def test_fetch_platform_tools_script_syntax_ok():
    script = ROOT / "scripts" / "fetch-platform-tools.sh"
    assert script.is_file(), "缺少 scripts/fetch-platform-tools.sh"
    bash = shutil.which("bash")
    assert bash, "需要 bash 做语法检查"
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_check_dist_prereqs_script_syntax_ok():
    script = ROOT / "scripts" / "check-dist-prereqs.sh"
    assert script.is_file(), "缺少 scripts/check-dist-prereqs.sh（G2 出包前置硬校验）"
    bash = shutil.which("bash")
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_build_mac_script_syntax_ok():
    script = ROOT / "scripts" / "build-mac.sh"
    assert script.is_file(), "缺少 scripts/build-mac.sh（mac 全链出包入口）"
    bash = shutil.which("bash")
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_fetch_platform_tools_downloads_google_darwin_zip():
    content = (ROOT / "scripts" / "fetch-platform-tools.sh").read_text(encoding="utf-8")
    # Google 官方下载源（macOS）；产物解压到 assets/platform-tools/<平台>/
    assert DARWIN_URL in content
    assert WINDOWS_URL in content
    assert "assets/platform-tools" in content.replace("${REPO_ROOT}/", "")
    assert "platform-tools.zip" in content
    # G2：按构建目标拉对应平台 zip（darwin / windows 分平台目录，杜绝跨平台混装）
    assert "darwin" in content and "windows" in content


def test_check_dist_prereqs_hard_fails_on_missing_pieces():
    content = (ROOT / "scripts" / "check-dist-prereqs.sh").read_text(encoding="utf-8")
    # G2：fresh checkout 缺前端/引擎/内置 adb 时必须 fail fast（electron-builder
    # 对缺失 from 只告警跳过，会静默打出无引擎无 UI 的死包）
    assert "frontend/dist/index.html" in content
    assert "release-engine" in content
    assert "platform-tools" in content
    assert "exit 1" in content
    # 中文指引：缺什么、先跑什么
    assert "未找到" in content and "请先执行" in content


def test_build_mac_covers_full_chain():
    content = (ROOT / "scripts" / "build-mac.sh").read_text(encoding="utf-8")
    # mac 与 build-windows.ps1 对齐的全链入口：platform-tools + 引擎 + 前端 + 打包
    assert "fetch-platform-tools.sh" in content
    assert "dist:engine" in content
    assert "npm run build" in content
    assert "dist:mac" in content


def test_build_windows_fetches_platform_tools():
    content = (ROOT / "build-windows.ps1").read_text(encoding="utf-8")
    # Windows 等价步骤：-windows.zip + 解压到 assets\platform-tools\windows（构建前置）
    assert WINDOWS_URL in content
    assert "platform-tools" in content
    assert "Expand-Archive" in content
    assert "adb.exe" in content
    assert "platform-tools\\windows" in content


def _package_json_build():
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    return pkg["build"]


def _platform_tools_entry(build, platform: str):
    for entry in build[platform]["extraResources"]:
        if "platform-tools" in str(entry.get("from", "")):
            return entry
    return None


def test_package_json_ships_platform_tools_per_platform():
    build = _package_json_build()
    # G2：分平台目录各自引用——mac 只带 darwin 产物、win 只带 windows 产物，
    # 在 fetch 过 darwin 的机器上出 win 包（npm run dist）也不会混入 mac adb。
    win_entry = _platform_tools_entry(build, "win")
    mac_entry = _platform_tools_entry(build, "mac")
    assert win_entry is not None and mac_entry is not None
    assert win_entry["from"] == "assets/platform-tools/windows", win_entry
    assert mac_entry["from"] == "assets/platform-tools/darwin", mac_entry
    assert win_entry["to"] == "platform-tools" and mac_entry["to"] == "platform-tools"
    win_filters = win_entry.get("filter") or []
    mac_filters = mac_entry.get("filter") or []
    assert any(p.endswith("*.exe") for p in win_filters) and any(p.endswith("*.dll") for p in win_filters)
    # mac 过滤任意深度排除 exe/dll（子目录里的 Windows 二进制不得混入）
    assert "**/*" in mac_filters and "!**/*.exe" in mac_filters and "!**/*.dll" in mac_filters
    # 全局 extraResources 不再混装平台二进制
    global_extra = {(e.get("from"), e.get("to")) for e in build["extraResources"]}
    assert not any("platform-tools" in str(frm) for frm, _ in global_extra)


def test_package_json_dist_scripts_fetch_platform_tools_first():
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = pkg["scripts"]
    # G2：fresh checkout 打包前先按目标拉内置 platform-tools（已存在则脚本自行跳过），
    # 再硬校验前端/引擎/内置 adb，缺件拒绝出包。build-windows.ps1 自带下载，不挂 pre 钩子。
    expected_targets = {
        "predist": "all",
        "predist:mac": "darwin",
        "predist:win": "windows",
    }
    for name, target in expected_targets.items():
        assert "fetch-platform-tools.sh" in scripts[name], name
        assert f"fetch-platform-tools.sh {target}" in scripts[name], name
        assert "check-dist-prereqs.sh" in scripts[name], name
        assert f"check-dist-prereqs.sh {target}" in scripts[name], name
        hook_target = name[len("pre") :]
        assert scripts[hook_target].startswith("electron-builder"), hook_target


def test_package_json_keeps_platform_tools_out_of_asar_and_assets_copy():
    build = _package_json_build()
    # Google 二进制不进 app.asar（spawn 不可用），也不随 assets 二次拷贝（避免双份体积）
    assert "!assets/platform-tools/**" in build["files"]
    assets_entry = next(e for e in build["extraResources"] if e.get("from") == "assets")
    filters = assets_entry.get("filter") or []
    assert "!platform-tools" in filters and "!platform-tools/**/*" in filters


def test_gitignore_excludes_platform_tools():
    content = (ROOT / ".gitignore").read_text(encoding="utf-8")
    # Google 二进制不入库
    assert "assets/platform-tools/" in content


def test_main_js_injects_autodroid_adb():
    content = (ROOT / "main.js").read_text(encoding="utf-8")
    # resolveEngineEnv 注入 AUTODROID_ADB（shell_policies.applyBundledAdbEnv，存在才设）
    assert "applyBundledAdbEnv(env" in content
    assert "AUTODROID_ENGINE_ASSETS_DIR" in content
