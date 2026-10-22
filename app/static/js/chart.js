"use strict";

// History (REST) and live candles (/ws/market) arrive from two sources at different times.
// Rules that keep them consistent:
// - every loadHistory call gets a loadId; responses of superseded calls are dropped;
// - kline messages that arrive while history is loading are buffered and applied after setData;
// - series.update() only runs for bar.time >= lastBarTime (the library throws on older bars).
// Prices arrive as strings; Number() is used only to draw them.
// Notes (/api/notes) load after the history of the same loadId and follow the same rules:
// note messages that arrive while notes are loading are buffered and applied afterwards.
// A note is drawn on the candle that contains it; note text is only ever set via textContent.

const chartPanel = (() => {
  const elements = {
    container: document.getElementById("chart"),
    symbol: document.getElementById("chart-symbol"),
    message: document.getElementById("chart-message"),
    stale: document.getElementById("chart-stale"),
    intervalButtons: document.querySelectorAll("[data-interval]"),
    typeButtons: document.querySelectorAll("[data-chart-type]"),
    notes: document.getElementById("chart-notes"),
  };

  const INTERVAL_MS = { "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000 };
  const MARKER_COLOR = "#d29922";
  const noteTimeFormat = new Intl.DateTimeFormat("sr-Latn-RS", {
    timeZone: "UTC",
    dateStyle: "short",
    timeStyle: "short",
  });

  const state = {
    symbol: null,
    interval: "1m",
    chartType: "candles", // "candles" | "line"
    loadId: 0,
    loading: false,
    buffer: [],
    lastBarTime: null,
    // id -> note of the selected pair, from notesFromMs onwards.
    notes: new Map(),
    notesFromMs: null,
    notesLoading: false,
    noteBuffer: [],
    // Candle time under the crosshair, so the note list follows live note changes.
    hoverTime: null,
  };

  function showMessage(text) {
    elements.message.textContent = text;
    elements.message.hidden = text === "";
  }

  if (typeof LightweightCharts === "undefined") {
    showMessage("Biblioteka za grafikone nije učitana.");
    console.error("LightweightCharts is not available; the chart is disabled");
    return { select() {}, handleKline() {}, handleNoteMessage() {}, setStale() {} };
  }

  const { createChart, createSeriesMarkers, CandlestickSeries, LineSeries } = LightweightCharts;
  const chart = createChart(elements.container, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#0f1420" }, textColor: "#c9d1e0" },
    grid: { vertLines: { color: "#1c2333" }, horzLines: { color: "#1c2333" } },
    timeScale: { timeVisible: true, secondsVisible: false },
  });
  let series = createSeries();
  let markers = createSeriesMarkers(series, []);

  function createSeries() {
    return state.chartType === "line"
      ? chart.addSeries(LineSeries, { color: "#58a6ff", lineWidth: 2 })
      : chart.addSeries(CandlestickSeries);
  }

  // The one conversion from UTC milliseconds to chart time, shared by candles and notes.
  function toChartTime(ms) {
    return Math.floor(ms / 1000);
  }

  // Start of the candle on the active interval that contains the moment.
  function candleTime(ms) {
    const intervalMs = INTERVAL_MS[state.interval];
    return toChartTime(Math.floor(ms / intervalMs) * intervalMs);
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
    const newCandle = bar.time !== state.lastBarTime;
    series.update(bar);
    state.lastBarTime = bar.time;
    // A note in a candle that had not started yet gets its marker once the candle exists.
    if (newCandle && state.notes.size > 0) renderNotes();
  }

  // ---------- Notes ----------

  // candle time -> notes in that candle, oldest first. Candles not on the chart are left out.
  function notesByCandle() {
    const sorted = [...state.notes.values()].sort((a, b) => a.time_ms - b.time_ms || a.id - b.id);
    const groups = new Map();
    for (const note of sorted) {
      const time = candleTime(note.time_ms);
      if (state.lastBarTime === null || time > state.lastBarTime) continue;
      if (!groups.has(time)) groups.set(time, []);
      groups.get(time).push(note);
    }
    return groups;
  }

  function renderNotes() {
    const groups = notesByCandle();
    // Map keys keep the order of the time-sorted notes, so markers are sorted ascending.
    markers.setMarkers(
      [...groups].map(([time, notes]) => ({
        time,
        position: "aboveBar",
        shape: "circle",
        color: MARKER_COLOR,
        text: notes.length > 1 ? String(notes.length) : "",
      })),
    );
    renderNoteList(groups.get(state.hoverTime) ?? []);
  }

  function renderNoteList(notes) {
    const items = notes.map((note) => {
      const item = document.createElement("li");
      const time = document.createElement("time");
      time.dateTime = new Date(note.time_ms).toISOString();
      time.textContent = noteTimeFormat.format(note.time_ms);
      const text = document.createElement("span");
      // textContent, never innerHTML: note text is user input.
      text.textContent = note.text;
      item.append(time, text);
      return item;
    });
    elements.notes.replaceChildren(...items);
    elements.notes.hidden = items.length === 0;
  }

  function resetNotes() {
    // Every load starts a new markers object; the old one must not stay on the series.
    markers.detach();
    markers = createSeriesMarkers(series, []);
    state.notes = new Map();
    state.notesFromMs = null;
    state.notesLoading = true;
    state.noteBuffer = [];
    renderNoteList([]);
  }

  function isNoteForChart(note) {
    return (
      note.symbol === state.symbol && state.notesFromMs !== null && note.time_ms >= state.notesFromMs
    );
  }

  function applyNoteMessage(message) {
    const { type, data } = message;
    if (type === "note_deleted") {
      state.notes.delete(data.id);
    } else if (isNoteForChart(data)) {
      state.notes.set(data.id, data);
    } else {
      // Another pair's note or one older than the chart; delete is a no-op for those.
      state.notes.delete(data.id);
    }
  }

  async function loadNotes(symbol, loadId, fromMs) {
    let notes;
    try {
      // No to_ms: live candles keep extending the chart, so the range stays open-ended.
      const params = new URLSearchParams({ symbol, from_ms: String(fromMs) });
      const response = await fetch(`/api/notes?${params}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      notes = await response.json();
    } catch (error) {
      if (loadId !== state.loadId) return;
      console.warn(`Could not load notes for ${symbol}`, error);
      state.notesLoading = false;
      state.noteBuffer = [];
      return;
    }
    if (loadId !== state.loadId) return;

    state.notesFromMs = fromMs;
    state.notes = new Map(notes.map((note) => [note.id, note]));
    state.notesLoading = false;
    const buffered = state.noteBuffer;
    state.noteBuffer = [];
    for (const message of buffered) applyNoteMessage(message);
    renderNotes();
  }

  function handleNoteMessage(message) {
    if (state.notesLoading) {
      state.noteBuffer.push(message);
      return;
    }
    // Without a loaded range (no candles or a failed load) there is nothing to draw on.
    if (state.notesFromMs === null) return;
    applyNoteMessage(message);
    renderNotes();
  }

  function handleCrosshairMove(param) {
    state.hoverTime = param.time ?? null;
    renderNoteList(notesByCandle().get(state.hoverTime) ?? []);
  }

  async function loadHistory(symbol, interval) {
    const loadId = ++state.loadId;
    state.loading = true;
    state.buffer = [];
    state.lastBarTime = null;
    series.setData([]);
    resetNotes();
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
      state.notesLoading = false;
      state.noteBuffer = [];
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

    if (bars.length > 0) {
      loadNotes(symbol, loadId, bars[0].time * 1000);
    } else {
      state.notesLoading = false;
      state.noteBuffer = [];
    }
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
    markers.detach();
    chart.removeSeries(series);
    series = createSeries();
    markers = createSeriesMarkers(series, []);
    reload();
  }

  function handleKline(kline) {
    if (kline.symbol !== state.symbol || kline.interval !== state.interval) return;
    // Binance times are UTC milliseconds; the server sends open_time as an ISO string.
    const time = toChartTime(Date.parse(kline.open_time));
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

  chart.subscribeCrosshairMove(handleCrosshairMove);

  return { select, handleKline, handleNoteMessage, setStale };
})();
