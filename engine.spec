# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：engine 单文件（onefile）。

用法：
    .venv/bin/python -m PyInstaller --clean --noconfirm \
        --distpath release-engine --workpath build-engine engine.spec
产物：release-engine/autodroid-engine/（macOS/Linux 目录）/ autodroid-engine/autodroid-engine.exe（Windows）。

约定：
- 资源（assets/fastbot、assets/scrcpy-server.jar）不打进二进制，随 electron-builder
  extraResources（resources/assets）分发，由 AUTODROID_ENGINE_ASSETS_DIR 指向；
- 运行时数据（模板缓存等）走 AUTODROID_USER_DATA（Electron userData）。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# uiautomator2 的包内数据（assets/u2.jar、app-uiautomator.apk 等）必须随二进制打包：
# 运行时 with_package_resource 经 importlib files("uiautomator2")/assets/... 定位，
# frozen 态若不带 datas 会报「Resource assets/u2.jar not found in uiautomator2 package」。
datas = collect_data_files("uiautomator2") + collect_data_files("findit")

hiddenimports = []
for package in (
    "uvicorn",
    "apscheduler",
    "fastapi",
    "pydantic",
    "adbutils",
    "httpx",
    "websockets",
    "uiautomator2",
    "findit",
    "engine",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:  # pragma: no cover - 个别包无子模块也可接受
        pass

a = Analysis(
    ["engine_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onedir：产物为目录（可执行体 + _internal/），启动免去 onefile 的整包解压，
# 冷启动实测 onefile 28s / onedir 预期 1-2s。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="autodroid-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="autodroid-engine",
)
