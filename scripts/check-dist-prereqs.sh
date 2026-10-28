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

# ---------- 4. 新鲜度校验：存在 ≠ 可用 ----------
# 教训（2026-10-28）：dmg 重建时只跑了 predist，release-engine 里还是旧引擎
# （含回退裸 "adb" 的旧行为），装包后投屏报 No such file or directory: 'adb'。
# 源码比产物新 → 拒绝出包，逼重建。
check_freshness() {
  local artifact="$1"
  shift
  local stale
  stale=$(find "$@" -type f \( -name "*.py" -o -name "*.spec" -o -name "*.js" -o -name "*.mjs" -o -name "*.vue" -o -name "*.css" -o -name "*.html" -o -name "*.json" \) -newer "${artifact}" 2>/dev/null | head -1)
  if [ -n "${stale}" ]; then
    fail "产物过期：${stale} 等源文件比 ${artifact} 新。引擎请先 npm run dist:engine（或 bash scripts/build-mac.sh 全链），前端请在 ../AutoDroid/frontend 执行 npm run build"
  fi
}

check_engine_freshness() {
  local exe_name="autodroid-engine"
  local bin=""
  for candidate in \
    "${REPO_ROOT}/release-engine/autodroid-engine/${exe_name}" \
    "${REPO_ROOT}/release-engine/${exe_name}" \
    "${REPO_ROOT}/release-engine/autodroid-engine/${exe_name}.exe" \
    "${REPO_ROOT}/release-engine/${exe_name}.exe"; do
    if [ -f "${candidate}" ]; then
      bin="${candidate}"
      break
    fi
  done
  if [ -z "${bin}" ]; then
    return 0  # 存在性校验已报错
  fi
  check_freshness "${bin}" "${REPO_ROOT}/engine" "${REPO_ROOT}/engine.spec" "${REPO_ROOT}/engine_entry.py"
}

check_frontend_freshness() {
  if [ -f "${FRONTEND_INDEX}" ]; then
    check_freshness "${FRONTEND_INDEX}" "${REPO_ROOT}/../AutoDroid/frontend/src" "${REPO_ROOT}/../AutoDroid/frontend/package.json"
  fi
}

check_engine_freshness
check_frontend_freshness

if [ "${TARGET}" = "darwin" ] || [ "${TARGET}" = "all" ]; then
  check_engine darwin
  check_platform_tools darwin
fi
if [ "${TARGET}" = "windows" ] || [ "${TARGET}" = "all" ]; then
  check_engine windows
  check_platform_tools windows
fi

echo "出包前置校验通过（目标: ${TARGET}）：前端产物 + 引擎二进制 + 内置 adb 就绪"
