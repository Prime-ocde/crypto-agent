# 云端部署 Runbook（Render + Streamlit Cloud）

> 目标：得到一个**永久公网地址**，不依赖本机、不会 60 分钟过期。
> 需要你本人完成 3 次账号授权（GitHub / Render / Streamlit Cloud），其余步骤照抄即可。

---

## 0. 前置

- GitHub 仓库（公开，或私有 + 给 Render 授权 GitHub App）
- Render 账号（用 GitHub 登录）
- Streamlit Community Cloud 账号（用 GitHub 登录）

---

## 1. 推代码到 GitHub

```powershell
cd D:\Projects\crypto-agent
git remote add origin https://github.com/<你的账号>/crypto-agent.git
git push -u origin main
```

> 仓库里已经包含 `render.yaml`、`Procfile`、`requirements.txt`、`.python-version`。

---

## 2. 部署后端（Render）

1. Render → **New → Blueprint** → 选择 `crypto-agent` 仓库（会自动读取 `render.yaml`）；
   或 **New → Web Service**，手动填：

   | 字段 | 值 |
   | --- | --- |
   | Runtime | Python |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT` |
   | Health Check Path | `/api/health` |
   | Instance | Free |

2. 环境变量（Blueprint 已内置，手动部署时照抄）：

   ```
   RUN_MODE=live
   SYMBOLS=BTCUSDT,ETHUSDT
   INTERVALS=1m,1d,1w
   BINANCE_WS_URL=wss://data-stream.binance.vision/ws
   BINANCE_WS_FALLBACKS=wss://stream.binance.com:9443/ws
   BINANCE_REST_URL=https://data-api.binance.vision
   BINANCE_REST_FALLBACK=https://api.binance.com
   FEE_RATE=0.001
   WS_IDLE_TIMEOUT=30
   PYTHON_VERSION=3.12.6
   ```

3. 等构建完成，记下后端地址，例如 `https://crypto-agent-api.onrender.com`。

4. 验证：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\deploy_check.ps1 `
     -Backend https://crypto-agent-api.onrender.com `
     -Frontend https://placeholder.streamlit.app
   ```

   > 前端还没部署时，最后一项会失败，其余应为 OK。

---

## 3. 部署前端（Streamlit Community Cloud）

1. <https://share.streamlit.io> → **New app** → 选同一个 GitHub 仓库；
2. Main file path：`frontend/app.py`；
3. Advanced settings → Python **3.12**；
4. Secrets（TOML 格式）：

   ```toml
   BACKEND_BASE_URL = "https://crypto-agent-api.onrender.com"
   ```

5. Deploy，记下前端地址，例如 `https://crypto-agent.streamlit.app`。

---

## 4. 全链路验证

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_check.ps1 `
  -Backend https://crypto-agent-api.onrender.com `
  -Frontend https://crypto-agent.streamlit.app
```

全部 OK 后，打开前端地址，确认：

- [ ] 状态条显示「🟢 后端实时」，不是「离线模拟」
- [ ] 右上角显示策略 / 现货或合约
- [ ] 切到「回测分析」→「运行回测」能出指标卡
- [ ] 「情绪 · 新闻」有 FOMO 仪表盘与新闻

---

## 5. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 前端显示「后端不可达 / 离线模拟」 | Secrets 里 `BACKEND_BASE_URL` 没填或填错 | 改 Secrets 后 **Reboot app** |
| 后端 502 / 构建失败 | `requirements.txt` 装不上 | 看 Render Logs；本项目依赖是宽松版本，3.12 可装 |
| 首次打开很慢（20~50s） | Render 免费套餐冷启动 | 演示前 5 分钟先访问 `/api/health` 唤醒 |
| 后端 `status: degraded` | WebSocket 未连上 | 检查 `BINANCE_WS_URL` 是否用 `data-stream.binance.vision` |
| Streamlit 页面空白 | Python 版本太高/太低 | 用 Advanced settings 选 3.12 |
| 情绪页显示「部分指标使用默认值」 | 部署机房访问不了币安合约接口 | 正常降级，页面已标注；不影响主流程 |

---

## 6. 关于"让 Agent 直接部署"

云端部署的每一步都需要**你本人账号的 OAuth 授权**（GitHub App、Render、Streamlit Cloud），
这些同意页面无法由自动化代替。若希望全自动，可提供以下任一凭据：

- Render API Key（Account Settings → API Keys）：可自动创建服务、读取部署状态；
- Hugging Face Token：可自动创建 Space（Streamlit / Docker）并上传代码。

拿到 token 后，脚本可以完成创建、部署、轮询状态与验收。
