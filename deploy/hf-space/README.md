---
title: Crypto Grid Quant
emoji: 📈
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# 加密货币多策略量化回测系统（模拟盘）

网格 / 马丁格尔 / 趋势跟踪 · 现货与合约（杠杆/强平/资金费）· 多空双向 · 情绪与新闻。

- 纯模拟（Paper Trading），不接真实资金、不下单；
- 行情来自 Binance 公共接口，接口不可用时自动降级为缓存/合成行情；
- 本 Space 为 Docker 部署：容器内同时运行 FastAPI（127.0.0.1:8000）与 Streamlit（0.0.0.0:7860）。

完整项目与使用说明见 GitHub：<https://github.com/Prime-ocde/crypto-agent>
