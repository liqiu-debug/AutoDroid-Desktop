#!/usr/bin/env bash
# ============================================================
# 出包前置硬校验（G2 闭环）：缺件直接 fail fast，拒绝出死包。
#
# electron-builder 对 extraResources 的缺失源目录只告警跳过（fileMatcher
# "file source doesn't exist"）且退出码 0——fresh checkout 直接 dist 会静默
# 打出无引擎/无 UI/无内置 adb 的死包。这里统一硬校验三类前置产物：
#   1. 前端产物 ../AutoDroid/frontend/dist/index.html
#   2. 本地引擎 release-engine/autodroid-engine[.exe]（PyInstaller 产物）
#   3. 内置 platform-tools（assets/platform-tools/<平台>/adb[.exe]）
#
# 用法：bash scripts/check-dist-prereqs.sh [darwin|windows|all]
#       （默认 all；与 fetch-platform-tools.sh 的目标参数一致）
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"
case "${TARGET}" in
  darwin|windows|all) ;;
  *)
    echo "用法：bash scripts/check-dist-prereqs.sh [darwin|windows|all]" >&2
    exit 2
    ;;
esac

fail() {
  echo "出包前置校验失败：$1" >&2
  exit 1
}

# ---------- 1. 前端产物 ----------
FRONTEND_INDEX="${REPO_ROOT}/../AutoDroid/frontend/dist/index.html"
if [ ! -f "${FRONTEND_INDEX}" ]; then
  fail "未找到前端产物 ${FRONTEND_INDEX}。请先在 ../AutoDroid/frontend 执行 npm run build"
fi

# ---------- 2. 本地引擎（PyInstaller onedir：release-engine/autodroid-engine/…） ----------
check_engine() {
  local platform="$1"
  local exe_name="autodroid-engine"
  if [ "${platform}" = "windows" ]; then
    exe_name="autodroid-engine.exe"
  fi
  if [ -f "${REPO_ROOT}/release-engine/autodroid-engine/${exe_name}" ] ||
    [ -f "${REPO_ROOT}/release-engine/${exe_name}" ]; then
    return 0
  fi
  if [ "${platform}" = "windows" ]; then
    fail "未找到 Windows 引擎二进制 release-engine/${exe_name}（PyInstaller 不跨平台）。Windows 出包请在 Windows 上执行 build-windows.ps1（全链一键）；mac 单平台出包请用 npm run dist:mac 或 bash scripts/build-mac.sh"
  fi
  fail "未找到引擎二进制 release-engine/${exe_name}。请先执行 npm run dist:engine"
}

# ---------- 3. 内置 platform-tools ----------
check_platform_tools() {
  local platform="$1"
  local exe_name="adb"
  if [ "${platform}" = "windows" ]; then
    exe_name="adb.exe"
  fi
  if [ -x "${REPO_ROOT}/assets/platform-tools/${platform}/${exe_name}" ]; then
    return 0
  fi
  fail "未找到内置 adb：assets/platform-tools/${platform}/${exe_name}。请先执行 bash scripts/fetch-platform-tools.sh ${platform}（或安装 Android platform-tools 后设置 AUTODROID_ADB）"
}

if [ "${TARGET}" = "darwin" ] || [ "${TARGET}" = "all" ]; then
  check_engine darwin
  check_platform_tools darwin
fi
if [ "${TARGET}" = "windows" ] || [ "${TARGET}" = "all" ]; then
  check_engine windows
  check_platform_tools windows
fi

echo "出包前置校验通过（目标: ${TARGET}）：前端产物 + 引擎二进制 + 内置 adb 就绪"
