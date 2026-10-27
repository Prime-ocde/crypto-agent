#!/usr/bin/env bash
# 一键部署 FastAPI 后端到 Linux 云服务器（Ubuntu/Debian）
#
# 用法（在服务器上，root）：
#   bash bootstrap.sh [端口] [分支]
#   bash bootstrap.sh 8000 main
#
# 说明：
# - 代码从 GitHub 公开仓库拉取；服务器需能访问 github.com 与 Binance；
# - 推荐香港/新加坡节点：免备案，且能连 Binance 公共行情；
# - 装成 systemd 服务，开机自启、崩溃自动重启；
# - 只暴露 8000 端口（大陆 80/443 需备案，这里不碰）。

set -euo pipefail

PORT="${1:-8000}"
BRANCH="${2:-main}"
REPO="${REPO:-https://github.com/Prime-ocde/crypto-agent.git}"
APP_DIR="${APP_DIR:-/opt/crypto-agent}"
SERVICE="crypto-agent-api"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行：sudo bash bootstrap.sh" >&2
  exit 1
fi

echo "==> 安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y git python3 python3-venv python3-pip curl
else
  echo "只支持 apt 系发行版（Ubuntu/Debian）" >&2
  exit 1
fi

echo "==> 创建运行用户"
if ! id crypto >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin crypto
fi

echo "==> 拉取代码"
rm -rf "$APP_DIR"
git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR"

echo "==> 创建虚拟环境并安装依赖"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R crypto:crypto "$APP_DIR"

echo "==> 写入 systemd 服务"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Crypto Agent API (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
User=crypto
WorkingDirectory=${APP_DIR}
Environment=RUN_MODE=live
Environment=SYMBOLS=BTCUSDT,ETHUSDT
Environment=INTERVALS=1m,1d,1w
Environment=BINANCE_WS_URL=wss://data-stream.binance.vision/ws
Environment=BINANCE_WS_FALLBACKS=wss://stream.binance.com:9443/ws
Environment=BINANCE_REST_URL=https://data-api.binance.vision
Environment=BINANCE_REST_FALLBACK=https://api.binance.com
Environment=WS_IDLE_TIMEOUT=30
ExecStart=${APP_DIR}/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo "==> 放行防火墙端口 ${PORT}"
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" || true
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" || true
  firewall-cmd --reload || true
fi

echo "==> 等待服务就绪"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "后端已就绪：http://<服务器公网IP>:${PORT}/api/health"
    echo "接下来：Streamlit Cloud → Settings → Secrets 填 BACKEND_BASE_URL=http://<服务器公网IP>:${PORT} → Reboot"
    exit 0
  fi
  sleep 2
done

echo "服务未在预期时间内就绪，查看日志：journalctl -u ${SERVICE} -n 100 --no-pager" >&2
exit 1
