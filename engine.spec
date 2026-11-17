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
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# OCR 是客户端内置能力，缺依赖或模型时构建失败，不能静默生成不可用安装包。
import rapidocr
import onnxruntime

ocr_root = Path(rapidocr.__file__).resolve().parent
for resource in (
    "config.yaml", "default_models.yaml", "models/ppocr_keys_v1.txt",
    "models/ch_PP-OCRv4_det_infer.onnx", "models/ch_PP-OCRv4_rec_infer.onnx",
    "models/ch_ppocr_mobile_v2.0_cls_infer.onnx",
):
    if not (ocr_root / resource).is_file():
        raise RuntimeError(f"OCR 打包资源缺失: {resource}；请安装 requirements.txt 后重建")

# uiautomator2 的包内数据（assets/u2.jar、app-uiautomator.apk 等）必须随二进制打包：
# 运行时 with_package_resource 经 importlib files("uiautomator2")/assets/... 定位，
# frozen 态若不带 datas 会报「Resource assets/u2.jar not found in uiautomator2 package」。
datas = collect_data_files("uiautomator2") + collect_data_files("findit") + collect_data_files("rapidocr")

# iOS 驱动（facebook-wda）：引擎里 IOSDriver 是可选导入（缺包只剩 Android），漏打不会构建失败，
# 只会在运行时报「iOS 驱动不可用」——必须显式收集。

hiddenimports = ["onnxruntime", "rapidocr.inference_engine.onnxruntime"]
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
    "wda",
    "engine",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:  # pragma: no cover - 个别包无子模块也可接受
        pass

a = Analysis(
    ["engine_entry.py"],
    pathex=[],
    binaries=collect_dynamic_libs("onnxruntime"),
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
