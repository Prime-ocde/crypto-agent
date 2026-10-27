FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RUN_MODE=live \
    SYMBOLS=BTCUSDT,ETHUSDT \
    INTERVALS=1m,1d,1w \
    BINANCE_WS_URL=wss://data-stream.binance.vision/ws \
    BINANCE_WS_FALLBACKS=wss://stream.binance.com:9443/ws \
    BINANCE_REST_URL=https://data-api.binance.vision \
    BINANCE_REST_FALLBACK=https://api.binance.com \
    FEE_RATE=0.001 \
    WS_IDLE_TIMEOUT=30 \
    BACKEND_BASE_URL=http://127.0.0.1:8000 \
    STREAMLIT_SERVER_PORT=7860 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["bash", "deploy/hf-space/start.sh"]
