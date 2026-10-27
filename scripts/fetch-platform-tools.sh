#!/usr/bin/env bash
# ============================================================
# 下载 Google 官方 Android platform-tools 到 assets/platform-tools/<平台>/（构建前置）
#
# 分平台目录（G2 闭环）：darwin / windows 各自独立目录，供 electron-builder
# 的 per-platform extraResources 引用——在 mac 上出 win 包时也能带上
# windows 的 adb.exe（反之亦然），绝不混装对方平台的二进制。
#
# - macOS：https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
# - Windows：https://dl.google.com/android/repository/platform-tools-latest-windows.zip
#   （Windows 本地出包走 build-windows.ps1，其下载步骤与此脚本幂等兼容）
# - 产物（Google 二进制）只进打包资源、不进 git（.gitignore 已排除
#   assets/platform-tools/），运行时由 engine.adb_scan 优先解析为内置 adb。
#
# 用法：bash scripts/fetch-platform-tools.sh [darwin|windows|all]
#       （默认 all；目标已存在 adb 时跳过，删除对应目录可强制更新）
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="${REPO_ROOT}/assets/platform-tools"

TARGET="${1:-all}"
case "${TARGET}" in
  darwin|windows|all) ;;
  *)
    echo "用法：bash scripts/fetch-platform-tools.sh [darwin|windows|all]" >&2
    exit 2
    ;;
esac

# Google 官方 platform-tools 最新包；可用环境变量覆盖（如换镜像）
DARWIN_URL="${PLATFORM_TOOLS_DARWIN_URL:-https://dl.google.com/android/repository/platform-tools-latest-darwin.zip}"
WINDOWS_URL="${PLATFORM_TOOLS_WINDOWS_URL:-https://dl.google.com/android/repository/platform-tools-latest-windows.zip}"

fetch_one() {
  local platform="$1" url="$2" exe_name="$3"
  local dest_dir="${BASE_DIR}/${platform}"

  if [ -x "${dest_dir}/${exe_name}" ]; then
    echo "platform-tools(${platform}) 已存在: ${dest_dir}/${exe_name}（跳过下载；删除该目录可强制更新）"
    return 0
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '${tmp_dir}'" RETURN

  echo "下载 platform-tools(${platform}): ${url}"
  curl -fL --retry 3 -o "${tmp_dir}/platform-tools.zip" "${url}"

  echo "解压到 ${dest_dir}"
  unzip -q -o "${tmp_dir}/platform-tools.zip" -d "${tmp_dir}/out"
  # zip 内是顶层 platform-tools/ 目录
  local src_dir="${tmp_dir}/out/platform-tools"
  if [ ! -d "${src_dir}" ]; then
    echo "解压结果异常：未找到 platform-tools/ 目录" >&2
    exit 1
  fi
  rm -rf "${dest_dir}"
  mkdir -p "${BASE_DIR}"
  mv "${src_dir}" "${dest_dir}"
  chmod +x "${dest_dir}"/* 2>/dev/null || true

  if [ ! -x "${dest_dir}/${exe_name}" ]; then
    echo "解压结果异常：${dest_dir}/${exe_name} 不可执行" >&2
    exit 1
  fi
  echo "OK: ${dest_dir}/${exe_name}"
}

if [ "${TARGET}" = "darwin" ] || [ "${TARGET}" = "all" ]; then
  fetch_one darwin "${DARWIN_URL}" adb
fi
if [ "${TARGET}" = "windows" ] || [ "${TARGET}" = "all" ]; then
  fetch_one windows "${WINDOWS_URL}" adb.exe
fi
