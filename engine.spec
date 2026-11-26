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
import platform
import runpy
import sys

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

# Fixed, checksummed Trace Processor is part of the frozen runtime. Downloads
# happen explicitly in build preparation; a missing/wrong artifact stops here.
trace_platform = ("windows-x64" if sys.platform == "win32" else
                  "mac-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "mac-x64")
trace_name = "trace_processor_shell.exe" if sys.platform == "win32" else "trace_processor_shell"
trace_root = Path(SPECPATH) / "assets" / "trace_processor" / trace_platform
trace_manifest = Path(SPECPATH) / "scripts" / "trace-processor-manifest.json"
trace_fetch = runpy.run_path(str(Path(SPECPATH) / "scripts" / "fetch-trace-processor.py"))
if trace_fetch["main"](["--platform", trace_platform, "--verify-only"]) != 0:
    raise RuntimeError("Trace Processor 校验失败，不能继续构建")
datas += [(str(trace_root / trace_name), f"trace_processor/{trace_platform}"),
          (str(trace_manifest), "trace_processor"),
          (str(Path(SPECPATH) / "scripts" / "trace-processor-LICENSE.txt"), "trace_processor")]

# iOS 驱动（facebook-wda）：引擎里 IOSDriver 是可选导入（缺包只剩 Android），漏打不会构建失败，
# 只会在运行时报「iOS 驱动不可用」——必须显式收集。

hiddenimports = ["onnxruntime", "rapidocr.inference_engine.onnxruntime",
                 "engine.core.startup.contract", "engine.core.startup.runner",
                 "engine.core.startup.android", "engine.core.startup.ios",
                 "engine.core.startup_service", "engine.core.startup_selfcheck",
                 "engine.core.startup_trace_analysis"]
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

# Analysis auto-detects Mach-O/PE files even when supplied as data. Keep the
# already verified official standalone tool byte-for-byte: PyInstaller's
# install-name rewriting/ad-hoc signing would invalidate its pinned SHA256.
trace_destination = f"trace_processor/{trace_platform}/{trace_name}"
trace_collected = [item for item in a.binaries if item[0].replace("\\", "/") == trace_destination]
a.binaries = [item for item in a.binaries if item not in trace_collected]
a.datas = [item for item in a.datas if item[0].replace("\\", "/") != trace_destination]
a.datas.append((trace_destination, str(trace_root / trace_name), "DATA"))

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
