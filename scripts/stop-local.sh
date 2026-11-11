#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
LOG_DIR="$(pwd)/.run"
for name in proxy upstream; do
  if [[ -f "$LOG_DIR/$name.pid" ]]; then
    pid="$(cat "$LOG_DIR/$name.pid")"
    kill "$pid" 2>/dev/null || true
    rm -f "$LOG_DIR/$name.pid"
    echo "stopped $name (pid $pid)"
  fi
done
