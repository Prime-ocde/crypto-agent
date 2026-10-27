#!/usr/bin/env bash
# macOS / Linux 启动后端（绑定 0.0.0.0）
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

[ -f ".env" ] || cp .env.example .env

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${LAN_IP}" ] && LAN_IP="127.0.0.1"
echo "[start] 本机文档： http://127.0.0.1:8000/docs"
echo "[start] 其它设备： http://${LAN_IP}:8000/docs"

exec python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
