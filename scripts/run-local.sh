#!/usr/bin/env bash
# 无 Docker 环境下的本地启动脚本：分别启动上游与代理两个单进程服务。
# 可用环境变量覆盖：UPSTREAM_PORT（默认 8001）、PROXY_PORT（默认 8000）。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
UPSTREAM_PORT="${UPSTREAM_PORT:-8001}"
PROXY_PORT="${PROXY_PORT:-8000}"
LOG_DIR="$(pwd)/.run"
mkdir -p "$LOG_DIR"

wait_healthz() {
  local port="$1"
  for _ in $(seq 1 50); do
    if curl -sf "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "service on :${port} did not become healthy" >&2
  return 1
}

echo "starting upstream on :${UPSTREAM_PORT} ..."
PYTHONUNBUFFERED=1 "$PYTHON" -m uvicorn upstream.app:app \
  --host 127.0.0.1 --port "$UPSTREAM_PORT" \
  >"$LOG_DIR/upstream.log" 2>&1 &
echo $! > "$LOG_DIR/upstream.pid"
wait_healthz "$UPSTREAM_PORT"

echo "starting proxy on :${PROXY_PORT} (upstream=http://127.0.0.1:${UPSTREAM_PORT}) ..."
UPSTREAM_BASE="http://127.0.0.1:${UPSTREAM_PORT}" PYTHONUNBUFFERED=1 \
  "$PYTHON" -m uvicorn proxy.app:app \
  --host 127.0.0.1 --port "$PROXY_PORT" \
  >"$LOG_DIR/proxy.log" 2>&1 &
echo $! > "$LOG_DIR/proxy.pid"
wait_healthz "$PROXY_PORT"

cat <<EOF

服务已启动：
  演示页（浏览器入口）: http://127.0.0.1:${PROXY_PORT}/
  代理健康检查        : http://127.0.0.1:${PROXY_PORT}/healthz
  本地上游            : http://127.0.0.1:${UPSTREAM_PORT}/
日志目录: $LOG_DIR
停止: scripts/stop-local.sh
EOF
