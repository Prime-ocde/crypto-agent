/* 自建 Streamlit 组件：Lightweight Charts v5，增量更新 + 保留缩放 + OHLC 悬浮框。
 *
 * 与 streamlit-lightweight-charts-pro 的关键差异：
 *   - 组件实例只创建一次图表；后续 render 走 series.update()/setData()，绝不 destroy 重建
 *     → 用户缩放/平移的视口不会被价格刷新打断
 *   - 悬浮十字光标时显示开高低收/涨跌幅/成交量（该库 0.3.0 没提供）
 *
 * 时间戳约定：Python 侧已把时间平移到「本地墙上时间」，所以这里用 UTC getter 格式化，
 * 画出来就是用户本机的时间。
 */

const LWC = window.LightweightCharts;

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let overlaySeries = [];       // [{series, key}]
let lastCandles = [];
let lastVolume = [];
let paneSeries = {};
let lineSeries = {};
let lastLineSig = "";
let refHandles = [];
let lastPaneSig = "";         // 上一帧数据，用于增量 diff
let lastOverlaySig = "";
let markerApi = null;

const el = document.getElementById("chart");
const tip = document.getElementById("tip");

const CHART_OPTIONS = {
  layout: {
    background: { type: "solid", color: "#ffffff" },
    textColor: "#131722",
    fontSize: 11,
    attributionLogo: false,
  },
  grid: { vertLines: { visible: false }, horzLines: { color: "rgba(0,0,0,0.06)" } },
  rightPriceScale: {
    borderColor: "rgba(0,0,0,0.12)",
    scaleMargins: { top: 0.08, bottom: 0.24 },
  },
  // timeVisible 必须为 true，否则分钟级数据的时间轴只会显示「几号」，看不到时分
  timeScale: { borderColor: "rgba(0,0,0,0.12)", timeVisible: true, secondsVisible: false },
  crosshair: { mode: LWC.CrosshairMode.Normal },
  autoSize: true,
  localization: { locale: "zh-CN" },
};

function pad2(n) { return String(n).padStart(2, "0"); }

/* 时间戳是「本地墙上时间的秒」，用 UTC getter 取出来正好是本地时间 */
function fmtTime(sec) {
  const d = new Date(sec * 1000);
  return `${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}
function fmtDay(sec) {
  const d = new Date(sec * 1000);
  return `${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
}

function ensureChart(height) {
  if (chart) return;
  chart = LWC.createChart(el, CHART_OPTIONS);
  candleSeries = chart.addSeries(LWC.CandlestickSeries, {
    upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    lastValueVisible: false, priceLineVisible: false,
  });
  volumeSeries = chart.addSeries(LWC.HistogramSeries, {
    priceScaleId: "volume", priceFormat: { type: "volume" },
    lastValueVisible: false, priceLineVisible: false,
  });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

  chart.subscribeCrosshairMove(onCrosshair);
  lastCandles = [];
  lastVolume = [];
  lastOverlaySig = "";
  overlaySeries = [];
  paneSeries = {};
  lastPaneSig = "";
  lineSeries = {};
  lastLineSig = "";
  refHandles = [];
  if (markerApi) { markerApi.detach?.(); markerApi = null; }
}

function onCrosshair(param) {
  if (!param || !param.time || !param.point) { tip.style.display = "none"; return; }
  const bar = param.seriesData.get(candleSeries);
  if (!bar) { tip.style.display = "none"; return; }
  const vol = param.seriesData.get(volumeSeries);
  const up = bar.close >= bar.open;
  const cls = up ? "up" : "down";
  const pct = bar.open ? ((bar.close / bar.open - 1) * 100).toFixed(2) : "0.00";
  const amp = bar.open ? (((bar.high - bar.low) / bar.open) * 100).toFixed(2) : "0.00";
  const body = [
    `<div class="row"><span class="k">时间</span><span>${fmtTime(param.time)}</span></div>`,
    `<div class="row"><span class="k">开</span><span>${bar.open.toFixed(2)}</span></div>`,
    `<div class="row"><span class="k">高</span><span>${bar.high.toFixed(2)}</span></div>`,
    `<div class="row"><span class="k">低</span><span>${bar.low.toFixed(2)}</span></div>`,
    `<div class="row"><span class="k">收</span><span class="${cls}">${bar.close.toFixed(2)}</span></div>`,
    `<div class="row"><span class="k">涨跌</span><span class="${cls}">${up ? "+" : ""}${pct}%</span></div>`,
    `<div class="row"><span class="k">振幅</span><span>${amp}%</span></div>`,
  ];
  if (vol && typeof vol.value === "number") {
    body.push(`<div class="row"><span class="k">量</span><span>${vol.value.toFixed(2)}</span></div>`);
  }
  tip.innerHTML = body.join("");
  tip.style.display = "block";
  const box = el.getBoundingClientRect();
  const x = Math.min(param.point.x + 16, box.width - 180);
  const y = Math.min(param.point.y + 16, box.height - 140);
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}

/* 前 n-1 根不变时，只 update 尾部，避免 setData 触发全量重绘 */
function samePrefix(prev, next) {
  if (next.length < prev.length) return false;
  if (prev.length === 0) return false;
  if (prev[0].time !== next[0].time) return false;
  const idx = prev.length - 1;
  return next[idx].time === prev[idx].time;
}

function applyCandles(candles, resetView) {
  if (!candles || candles.length === 0) return;
  if (resetView || !samePrefix(lastCandles, candles)) {
    candleSeries.setData(candles);
    lastCandles = candles;
    if (resetView) chart.timeScale().fitContent();
    return;
  }
  for (let i = lastCandles.length - 1; i < candles.length; i += 1) {
    candleSeries.update(candles[i]);
  }
  lastCandles = candles;
}

function applyVolume(volume, resetView) {
  if (!volume || volume.length === 0) return;
  if (resetView || !samePrefix(lastVolume, volume)) {
    volumeSeries.setData(volume);
    lastVolume = volume;
    return;
  }
  // 只更新尾部（含新增那根），绝不能用更旧的时间戳调 update —— LWC 会直接抛错
  for (let i = lastVolume.length - 1; i < volume.length; i += 1) {
    volumeSeries.update(volume[i]);
  }
  lastVolume = volume;
}

function applyOverlays(overlays) {
  const sig = JSON.stringify((overlays || []).map((o) => [o.color, o.width, o.points.length, o.points.at(-1)?.time]));
  if (sig !== lastOverlaySig) {
    overlaySeries.forEach(({ series }) => chart.removeSeries(series));
    overlaySeries = (overlays || []).map((o) => {
      const series = chart.addSeries(LWC.LineSeries, {
        color: o.color || "#4fc3f7", lineWidth: o.width || 1,
        lastValueVisible: false, priceLineVisible: false,
      });
      series.setData(o.points || []);
      return { series, key: o.key };
    });
    lastOverlaySig = sig;
    return;
  }
  (overlays || []).forEach((o, idx) => {
    const entry = overlaySeries[idx];
    if (!entry) return;
    const lastPoint = (o.points || []).at(-1);
    if (lastPoint) entry.series.update(lastPoint);
  });
}

/* 指标副图：pane 0 是 K 线（含成交量叠加），指标从 pane 1 开始。
 * 结构不变时只 setData（不会触发 fitContent，视口保持）；结构变了才重建这些 series。 */
function applyPanes(panes) {
  const spec = panes || [];
  const sig = JSON.stringify(
    spec.map((p) => [p.index, Boolean(p.hist), (p.lines || []).map((l) => [l.key, l.color])]),
  );
  const rebuild = sig !== lastPaneSig;
  if (rebuild) {
    Object.values(paneSeries).forEach((series) => {
      try { chart.removeSeries(series); } catch (e) { /* 已随 pane 销毁 */ }
    });
    paneSeries = {};
    lastPaneSig = sig;
  }
  spec.forEach((p) => {
    const idx = p.index;
    const scaleId = `pane${idx}`;
    if (p.hist) {
      let series = paneSeries[`${idx}-hist`];
      if (!series) {
        series = chart.addSeries(LWC.HistogramSeries, {
          priceScaleId: scaleId, lastValueVisible: false, priceLineVisible: false,
        }, idx);
        paneSeries[`${idx}-hist`] = series;
      }
      series.setData(p.hist.points || []);
    }
    (p.lines || []).forEach((line, i) => {
      const key = `${idx}-line${i}`;
      let series = paneSeries[key];
      if (!series) {
        series = chart.addSeries(LWC.LineSeries, {
          color: line.color || "#ffb74d", lineWidth: line.width || 1,
          lastValueVisible: false, priceLineVisible: false,
        }, idx);
        paneSeries[key] = series;
      }
      series.applyOptions({ color: line.color || "#ffb74d", lineWidth: line.width || 1 });
      series.setData(line.points || []);
    });
  });
}


/* 纯折线/面积图：净值、策略对比、情绪曲线。
 * 与 K 线图共用同一个实例：pane 0 起，不涉及蜡烛与成交量。 */
function applyLineSeries(specs) {
  const sig = JSON.stringify((specs || []).map((s) => [s.key, s.type, s.pane, s.color]));
  if (sig !== lastLineSig) {
    Object.values(lineSeries).forEach((series) => {
      try { chart.removeSeries(series); } catch (e) { /* 已销毁 */ }
    });
    lineSeries = {};
    lastLineSig = sig;
  }
  (specs || []).forEach((spec, i) => {
    const key = spec.key || `line${i}`;
    let series = lineSeries[key];
    if (!series) {
      if (spec.type === "area") {
        series = chart.addSeries(LWC.AreaSeries, {
          lineColor: spec.color || "#ef5350",
          topColor: spec.fill || "rgba(239,83,80,0.25)",
          bottomColor: spec.fillBottom || "rgba(239,83,80,0.02)",
          lineWidth: spec.width || 2,
          lastValueVisible: false,
          priceLineVisible: false,
        }, spec.pane || 0);
      } else {
        series = chart.addSeries(LWC.LineSeries, {
          color: spec.color || "#4fc3f7",
          lineWidth: spec.width || 2,
          lastValueVisible: false,
          priceLineVisible: false,
        }, spec.pane || 0);
      }
      lineSeries[key] = series;
    }
    series.setData(spec.points || []);
  });
}

function applyRefLines(specs, resetView) {
  refHandles.forEach((h) => {
    try { h.series.removePriceLine(h.handle); } catch (e) { /* 忽略 */ }
  });
  refHandles = [];
  const target = Object.values(lineSeries)[0];
  if (!target) return;
  (specs || []).forEach((spec) => {
    refHandles.push({
      series: target,
      handle: target.createPriceLine({
        price: spec.price,
        color: spec.color || "rgba(0,0,0,0.35)",
        lineWidth: 1,
        lineStyle: LWC.LineStyle.Dotted,
        axisLabelVisible: true,
        title: spec.title || "",
      }),
    });
  });
  if (resetView) chart.timeScale().fitContent();
}

function applyExtraPriceLines(specs) {
  (applyExtraPriceLines.handles || []).forEach((h) => {
    try { candleSeries.removePriceLine(h); } catch (e) { /* 忽略 */ }
  });
  applyExtraPriceLines.handles = (specs || []).map((spec) =>
    candleSeries.createPriceLine({
      price: spec.price,
      color: spec.color || "rgba(79,195,247,0.45)",
      lineWidth: 1,
      lineStyle: LWC.LineStyle.Dashed,
      axisLabelVisible: false,
      title: spec.title || "",
    }),
  );
}

function applyPriceLine(spec) {
  if (!spec || !spec.price) return;
  if (applyPriceLine.handle) {
    candleSeries.removePriceLine(applyPriceLine.handle);
    applyPriceLine.handle = null;
  }
  applyPriceLine.handle = candleSeries.createPriceLine({
    price: spec.price,
    color: spec.color || "#ffb74d",
    lineWidth: 2,
    lineStyle: LWC.LineStyle.Solid,
    axisLabelVisible: true,
    title: spec.title || "最新",
  });
}

function applyMarkers(markers) {
  if (markerApi) { markerApi.detach?.(); markerApi = null; }
  if (!markers || markers.length === 0) return;
  markerApi = LWC.createSeriesMarkers(candleSeries, markers);
}

function render(args) {
  const cfg = (args && args.config) || {};
  ensureChart(cfg.height);
  const resetView = Boolean(cfg.resetView);
  if (!(cfg.candles && cfg.candles.length)) {
    applyLineSeries(cfg.lineSeries || []);
    applyRefLines(cfg.refLines || [], resetView);
    setHeight(cfg.height || 420);
    return;
  }
  applyCandles(cfg.candles || [], resetView || lastCandles.length === 0);
  applyVolume(cfg.volume || [], resetView || lastCandles.length === 0);
  applyOverlays(cfg.overlays || []);
  applyPanes(cfg.panes || []);
  applyPriceLine(cfg.priceLine);
  applyExtraPriceLines(cfg.priceLines || []);
  applyMarkers(cfg.markers || []);
  lastHeight = 0; // 高度由 render 决定，强制刷新一次
  setHeight(cfg.height || 520);
}

/* Streamlit 组件协议（手写，不依赖 streamlit-component-lib）
 * 自定义组件里并不自动提供 window.Streamlit，必须用 postMessage 协议：
 *   - componentReady 告诉前端"我准备好了"
 *   - 监听 streamlit:render 收 args
 *   - setFrameHeight 让 iframe 有高度（否则容器 0 高，图画不出来）
 */
let lastHeight = 0;

function sendToStreamlit(type, data) {
  const message = { isStreamlitMessage: true, type, ...data };
  window.parent.postMessage(message, "*");
}

function setHeight(height) {
  const h = Math.max(160, Math.round(height || 520));
  if (h === lastHeight) return;
  lastHeight = h;
  document.body.style.height = `${h}px`;
  sendToStreamlit("streamlit:setFrameHeight", { height: h });
}

function onMessage(event) {
  const data = event.data;
  if (!data || data.type !== "streamlit:render") return;
  try {
    render(data.args || {});
  } catch (err) {
    // 把错误显示出来，避免"空白画布"无从排查
    const box = document.getElementById("tip");
    if (box) {
      box.style.display = "block";
      box.style.left = "12px";
      box.style.top = "12px";
      box.textContent = `组件渲染失败：${err && err.message ? err.message : err}`;
    }
  }
}

window.addEventListener("message", onMessage);
sendToStreamlit("streamlit:componentReady", { apiVersion: 1 });
setHeight(520);

/* 供自动化测试读取视口（生产无副作用） */
window.__lwc = {
  getRange: () => (chart ? chart.timeScale().getVisibleLogicalRange() : null),
  setRange: (from, to) => chart && chart.timeScale().setVisibleLogicalRange({ from, to }),
  candleCount: () => lastCandles.length,
  lastClose: () => (lastCandles.length ? lastCandles[lastCandles.length - 1].close : null),
};
