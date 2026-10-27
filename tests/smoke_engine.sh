#!/usr/bin/env bash
# 引擎 smoke 自测：
#   1. python 起引擎（只绑 127.0.0.1），解析 AUTODROID_ENGINE_PORT
#   2. GET /api/client/info → 200 {"clientMode": true, ...}
#   3. POST /api/auth/token 带 X-AutoDroid-Server: https://example.invalid
#      → 归一化写入 config.json {server}，上游失败按错误传递（5xx + detail）
#   4. 非法服务器地址 → 400 + 归一化错误文案
#   5. GET / → 静态托管（SPA）
#   6. tests/smoke_mock_upstream.py：mock upstream 集成
#      login → sync(register) → heartbeat → client-runs 上报 → logout
set -u
cd "$(dirname "$0")/.."

PY="${AUTODROID_PYTHON:-./.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY=python3
fi

WORKDIR="$(mktemp -d)"
export AUTODROID_USER_DATA="$WORKDIR/userdata"
export AUTODROID_CLIENT_VERSION="smoke-test"
STDOUT_LOG="$WORKDIR/engine.out"
STDERR_LOG="$WORKDIR/engine.err"

"$PY" -m engine >"$STDOUT_LOG" 2>"$STDERR_LOG" &
ENGINE_PID=$!
cleanup() {
  kill "$ENGINE_PID" 2>/dev/null
  wait "$ENGINE_PID" 2>/dev/null
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

PORT=""
for _ in $(seq 1 75); do
  PORT="$(sed -n 's/^AUTODROID_ENGINE_PORT=//p' "$STDOUT_LOG" | head -1)"
  if [ -n "$PORT" ]; then
    break
  fi
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    echo "FAIL: 引擎进程提前退出" >&2
    cat "$STDERR_LOG" >&2
    exit 1
  fi
  sleep 0.2
done
if [ -z "$PORT" ]; then
  echo "FAIL: 未捕获 AUTODROID_ENGINE_PORT" >&2
  cat "$STDERR_LOG" >&2
  exit 1
fi
BASE="http://127.0.0.1:$PORT"
echo "OK: 引擎已启动 $BASE"

# ---- 1. /api/client/info ----
INFO_FILE="$WORKDIR/info.json"
CODE=$(curl -sS -o "$INFO_FILE" -w '%{http_code}' "$BASE/api/client/info")
if [ "$CODE" != "200" ]; then
  echo "FAIL: /api/client/info 状态码 $CODE（期望 200）" >&2
  exit 1
fi
if ! grep -q '"clientMode":true' "$INFO_FILE" && ! grep -q '"clientMode": true' "$INFO_FILE"; then
  echo "FAIL: /api/client/info 未返回 clientMode:true -> $(cat "$INFO_FILE")" >&2
  exit 1
fi
if ! grep -q '"platform"' "$INFO_FILE"; then
  echo "FAIL: /api/client/info 缺少 platform 字段" >&2
  exit 1
fi
echo "OK: /api/client/info -> $(cat "$INFO_FILE")"

# ---- 2. 非法服务器地址 → 400 ----
BAD_FILE="$WORKDIR/bad.json"
CODE=$(curl -sS -o "$BAD_FILE" -w '%{http_code}' -X POST "$BASE/api/auth/token" \
  -H 'X-AutoDroid-Server: example.com' -d 'username=u&password=p')
if [ "$CODE" != "400" ]; then
  echo "FAIL: 非法地址未返回 400 -> $CODE $(cat "$BAD_FILE")" >&2
  exit 1
fi
grep -q '请输入有效的平台地址' "$BAD_FILE" || {
  echo "FAIL: 400 响应缺少归一化错误文案 -> $(cat "$BAD_FILE")" >&2
  exit 1
}
echo "OK: 非法地址 400 + 归一化文案"

# ---- 3. 登录转发 + 上游失败错误传递 ----
TOKEN_FILE="$WORKDIR/token.json"
CODE=$(curl -sS -o "$TOKEN_FILE" -w '%{http_code}' --max-time 30 -X POST "$BASE/api/auth/token" \
  -H 'X-AutoDroid-Server: https://example.invalid' -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=smoke&password=smoke')
case "$CODE" in
  502 | 503 | 504)
    echo "OK: 上游不可达错误已传递 status=$CODE body=$(cat "$TOKEN_FILE")"
    ;;
  *)
    echo "FAIL: 期望上游失败错误传递（5xx）实际 $CODE -> $(cat "$TOKEN_FILE")" >&2
    exit 1
    ;;
esac

# ---- 4. 归一化结果落盘（仅 {server}，无凭据） ----
CONFIG="$AUTODROID_USER_DATA/config.json"
if [ ! -f "$CONFIG" ]; then
  echo "FAIL: 未生成 $CONFIG" >&2
  exit 1
fi
grep -q 'https://example.invalid' "$CONFIG" || {
  echo "FAIL: config.json 未写入归一化 server -> $(cat "$CONFIG")" >&2
  exit 1
}
if grep -q 'smoke' "$CONFIG"; then
  echo "FAIL: config.json 泄露了凭据 -> $(cat "$CONFIG")" >&2
  exit 1
fi
echo "OK: config.json -> $(cat "$CONFIG")"

# ---- 5. 静态托管 / SPA fallback ----
CODE=$(curl -sS -o "$WORKDIR/index.html" -w '%{http_code}' "$BASE/")
if [ "$CODE" != "200" ]; then
  echo "WARN: 静态托管 / 返回 $CODE（前端 dist 未构建时可忽略）"
else
  echo "OK: 静态托管 / 200"
fi

# ---- 6. mock upstream 集成：login → sync → client-runs 上报 → logout ----
if ! "$PY" tests/smoke_mock_upstream.py; then
  echo "FAIL: mock upstream 集成 smoke 失败" >&2
  exit 1
fi

echo "SMOKE PASSED"
