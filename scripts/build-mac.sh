#!/usr/bin/env bash
# ============================================================
# macOS 出包统一入口（与 build-windows.ps1 对齐的全链路）：
#   1. 内置 platform-tools（darwin，已存在则跳过）
#   2. 本地引擎（release-engine 缺失才跑 PyInstaller，首次约 2-5 分钟）
#   3. 前端产物（../AutoDroid/frontend/dist 缺失才跑 npm run build）
#   4. electron-builder --mac（npm run dist:mac，内含 predist:mac 硬校验）
#
# 用法：bash scripts/build-mac.sh
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

step() { echo; echo "==== $1 ===="; }

step "1/4 内置 platform-tools（darwin）"
bash scripts/fetch-platform-tools.sh darwin

step "2/4 本地引擎（release-engine）"
if [ -f release-engine/autodroid-engine/autodroid-engine ] || [ -f release-engine/autodroid-engine ]; then
  echo "引擎已存在（跳过 PyInstaller；删除 release-engine/ 可强制重建）"
else
  npm run dist:engine
fi

step "3/4 前端产物（../AutoDroid/frontend/dist）"
if [ -f ../AutoDroid/frontend/dist/index.html ]; then
  echo "前端产物已存在（跳过 vite build）"
else
  (cd ../AutoDroid/frontend && npm run build)
fi

step "4/4 打包（electron-builder --mac）"
npm run dist:mac

echo
echo "OK: macOS 出包完成，产物见 release/"
