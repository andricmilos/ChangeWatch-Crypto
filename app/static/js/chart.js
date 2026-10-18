"use strict";

// History (REST) and live candles (/ws/market) arrive from two sources at different times.
// Rules that keep them consistent:
// - every loadHistory call gets a loadId; responses of superseded calls are dropped;
// - kline messages that arrive while history is loading are buffered and applied after setData;
// - series.update() only runs for bar.time >= lastBarTime (the library throws on older bars).
// Prices arrive as strings; Number() is used only to draw them.

const chartPanel = (() => {
  const elements = {
    container: document.getElementById("chart"),
    symbol: document.getElementById("chart-symbol"),
    message: document.getElementById("chart-message"),
    stale: document.getElementById("chart-stale"),
    intervalButtons: document.querySelectorAll("[data-interval]"),
    typeButtons: document.querySelectorAll("[data-chart-type]"),
  };

  const state = {
    symbol: null,
    interval: "1m",
    chartType: "candles", // "candles" | "line"
    loadId: 0,
    loading: false,
    buffer: [],
    lastBarTime: null,
  };

  function showMessage(text) {
    elements.message.textContent = text;
    elements.message.hidden = text === "";
  }

  if (typeof LightweightCharts === "undefined") {
    showMessage("Biblioteka za grafikone nije učitana.");
    console.error("LightweightCharts is not available; the chart is disabled");
    return { select() {}, handleKline() {}, setStale() {} };
  }

  const { createChart, CandlestickSeries, LineSeries } = LightweightCharts;
  const chart = createChart(elements.container, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#0f1420" }, textColor: "#c9d1e0" },
    grid: { vertLines: { color: "#1c2333" }, horzLines: { color: "#1c2333" } },
    timeScale: { timeVisible: true, secondsVisible: false },
  });
  let series = createSeries();

  function createSeries() {
    return state.chartType === "line"
      ? chart.addSeries(LineSeries, { color: "#58a6ff", lineWidth: 2 })
      : chart.addSeries(CandlestickSeries);
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : null;
  }

  // Returns null for a bar with a missing or invalid price, so it is skipped instead of drawn as 0.
  function toBar(time, candle) {
    if (!Number.isInteger(time)) return null;
    const close = toNumber(candle.close);
    if (close === null) return null;
    if (state.chartType === "line") return { time, value: close };

    const open = toNumber(candle.open);
    const high = toNumber(candle.high);
    const low = toNumber(candle.low);
    if (open === null || high === null || low === null) return null;
    return { time, open, high, low, close };
  }

  function applyBar(bar) {
    // Same time replaces the last bar, a newer one appends; an older one would make
    // Lightweight Charts throw "Cannot update oldest data", so it is skipped.
    if (state.lastBarTime !== null && bar.time < state.lastBarTime) return;
    series.update(bar);
    state.lastBarTime = bar.time;
  }

  async function loadHistory(symbol, interval) {
    const loadId = ++state.loadId;
    state.loading = true;
    state.buffer = [];
    state.lastBarTime = null;
    series.setData([]);
    showMessage("Učitavanje grafikona…");

    let candles;
    try {
      const params = new URLSearchParams({ interval, limit: "300" });
      const response = await fetch(`/api/markets/${encodeURIComponent(symbol)}/candles?${params}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      candles = await response.json();
    } catch (error) {
      if (loadId !== state.loadId) return;
      console.warn(`Could not load candles for ${symbol} ${interval}`, error);
      state.loading = false;
      state.buffer = [];
      showMessage("Istorija sveća trenutno nije dostupna.");
      return;
    }
    // The user picked another pair, interval or chart type in the meantime.
    if (loadId !== state.loadId) return;

    const bars = candles.map((candle) => toBar(candle.time, candle)).filter((bar) => bar !== null);
    series.setData(bars);
    chart.timeScale().fitContent();
    state.lastBarTime = bars.length > 0 ? bars[bars.length - 1].time : null;
    state.loading = false;
    showMessage(bars.length > 0 ? "" : "Nema istorijskih sveća za ovaj par.");

    const buffered = state.buffer;
    state.buffer = [];
    for (const bar of buffered) applyBar(bar);
  }

  function reload() {
    if (state.symbol !== null) loadHistory(state.symbol, state.interval);
  }

  function select(symbol, displaySymbol) {
    if (symbol === state.symbol) return;
    state.symbol = symbol;
    elements.symbol.textContent = displaySymbol;
    reload();
  }

  function selectInterval(interval) {
    if (interval === state.interval) return;
    state.interval = interval;
    for (const button of elements.intervalButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.interval === interval));
    }
    reload();
  }

  function setChartType(chartType) {
    if (chartType === state.chartType) return;
    state.chartType = chartType;
    for (const button of elements.typeButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.chartType === chartType));
    }
    chart.removeSeries(series);
    series = createSeries();
    reload();
  }

  function handleKline(kline) {
    if (kline.symbol !== state.symbol || kline.interval !== state.interval) return;
    // Binance times are UTC milliseconds; the server sends open_time as an ISO string.
    const time = Math.floor(Date.parse(kline.open_time) / 1000);
    const bar = toBar(time, kline);
    if (bar === null) return;
    if (state.loading) {
      state.buffer.push(bar);
      return;
    }
    applyBar(bar);
  }

  function setStale(stale) {
    elements.stale.hidden = !stale;
  }

  for (const button of elements.intervalButtons) {
    button.addEventListener("click", () => selectInterval(button.dataset.interval));
  }
  for (const button of elements.typeButtons) {
    button.addEventListener("click", () => setChartType(button.dataset.chartType));
  }

  return { select, handleKline, setStale };
})();
