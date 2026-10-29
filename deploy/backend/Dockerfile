# 后端（FastAPI 行情服务）通用镜像：Koyeb / Zeabur / Fly.io / Railway / 自建 VPS 都能直接用
# 只装 requirements-backend.txt（无 Streamlit / Plotly / 图表包），构建快、内存小
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

COPY requirements-backend.txt ./
RUN pip install -r requirements-backend.txt

COPY backend ./backend
COPY core ./core

# 行情源：阶段二起统一走币安"仅公共行情"域名；fapi 仅用于资金费/多空比
ENV RUN_MODE=live \
    SYMBOLS=BTCUSDT,ETHUSDT \
    INTERVALS=1m,5m,1h,1d,1w,1M \
    BINANCE_WS_URL=wss://data-stream.binance.vision/ws \
    BINANCE_WS_FALLBACKS=wss://stream.binance.com:9443/ws \
    BINANCE_REST_URL=https://data-api.binance.vision \
    BINANCE_REST_FALLBACK=https://api.binance.com \
    WS_IDLE_TIMEOUT=30 \
    REST_REFRESH_SECONDS=30

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
