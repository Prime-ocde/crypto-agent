#!/usr/bin/env bash
# Hugging Face Space 入口：同容器内跑 FastAPI + Streamlit（对外只暴露 7860）
set -euo pipefail

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning &

# 等后端起来（最多 30 秒）
for _ in $(seq 1 30); do
  if python - <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  then
    echo "backend ready"
    break
  fi
  sleep 1
done

exec python -m streamlit run frontend/app.py \
  --server.port 7860 \
  --server.address 0.0.0.0 \
  --server.headless true
