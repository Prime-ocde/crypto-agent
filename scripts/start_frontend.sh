#!/usr/bin/env bash
# macOS / Linux 启动前端（支持局域网其它设备访问）
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${LAN_IP}" ] && LAN_IP="127.0.0.1"

export BACKEND_BASE_URL="http://${LAN_IP}:8000"
echo "[start] 默认后端：${BACKEND_BASE_URL}"
echo "[start] 本机打开： http://localhost:8501"
echo "[start] 其它设备： http://${LAN_IP}:8501"

exec python -m streamlit run frontend/app.py --server.address 0.0.0.0 --server.port 8501
