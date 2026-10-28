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
// Adding, editing and deleting only send the request: the list and the markers change when
// the note_* message comes back over the WebSocket, so there is one path for every change.
// Fired alerts (/api/alerts) load next to the notes with the same loadId, buffering and candle
// rules, and alert_triggered adds a marker live. Notes and alerts share one markers object.

const chartPanel = (() => {
  const elements = {
    container: document.getElementById("chart"),
    symbol: document.getElementById("chart-symbol"),
    message: document.getElementById("chart-message"),
    stale: document.getElementById("chart-stale"),
    intervalButtons: document.querySelectorAll("[data-interval]"),
    typeButtons: document.querySelectorAll("[data-chart-type]"),
    notes: document.getElementById("chart-notes"),
    markerButtons: document.querySelectorAll("[data-marker-kind]"),
    noteAdd: document.getElementById("note-add"),
    pickHint: document.getElementById("note-pick-hint"),
    form: document.getElementById("note-form"),
    formLocal: document.getElementById("note-form-local"),
    formUtc: document.getElementById("note-form-utc"),
    formText: document.getElementById("note-form-text"),
    formCount: document.getElementById("note-form-count"),
    formCancel: document.getElementById("note-form-cancel"),
    formSubmit: document.querySelector("#note-form [type=submit]"),
    formError: document.getElementById("note-form-error"),
    panelSymbol: document.getElementById("notes-panel-symbol"),
    panelError: document.getElementById("notes-panel-error"),
    panelEmpty: document.getElementById("notes-panel-empty"),
    panelList: document.getElementById("notes-panel-list"),
    exportCandles: document.getElementById("export-candles"),
    exportCandlesError: document.getElementById("export-candles-error"),
    exportNotes: document.getElementById("export-notes"),
    exportNotesError: document.getElementById("export-notes-error"),
  };

  const INTERVAL_MS = { "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000 };
  const MARKER_COLOR = "#d29922";
  const ALERT_COLOR = "#a371f7";
  const MARKERS_STORAGE_KEY = "changewatch.chartMarkers";
  const ALERT_SHAPES = {
    above: { arrow: "▲", word: "iznad", position: "belowBar", shape: "arrowUp" },
    below: { arrow: "▼", word: "ispod", position: "aboveBar", shape: "arrowDown" },
  };
  const noteTimeFormat = new Intl.DateTimeFormat("sr-Latn-RS", {
    timeZone: "UTC",
    dateStyle: "short",
    timeStyle: "short",
  });
  const localTimeFormat = new Intl.DateTimeFormat("sr-Latn-RS", {
    dateStyle: "short",
    timeStyle: "short",
  });
  const NOTE_MAX_LENGTH = 280;
  // Substrings of the server's English validation messages -> text for the user.
  const VALIDATION_MESSAGES = [
    ["HTML", "Tekst ne sme da sadrži HTML oznake."],
    ["must not be empty", "Tekst ne sme biti prazan."],
    ["at most", `Tekst može imati najviše ${NOTE_MAX_LENGTH} znakova.`],
    ["in the future", "Izabrano vreme je u budućnosti."],
    ["older than 1 year", "Izabrano vreme je starije od godinu dana."],
    ["Unsupported symbol", "Ovaj par nije podržan."],
  ];

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
    // id -> fired alert of the selected pair (with time_ms added), from alertsFromMs onwards.
    alerts: new Map(),
    alertsFromMs: null,
    alertsLoading: false,
    alertBuffer: [],
    // Which markers the toggles above the chart show; remembered in localStorage.
    show: { notes: true, alerts: true },
    // Candle time under the crosshair, so the hover list follows live note and alert changes.
    hoverTime: null,
    // Until /api/notes/config answers, the UI is read-only.
    writeEnabled: false,
    picking: false,
    pickedMs: null,
    saving: false,
    editingId: null,
    editDraft: "",
    confirmingId: null,
    // Note whose PATCH or DELETE is in flight; its buttons are disabled meanwhile.
    pendingId: null,
  };

  function showMessage(text) {
    elements.message.textContent = text;
    elements.message.hidden = text === "";
  }

  if (typeof LightweightCharts === "undefined") {
    showMessage("Biblioteka za grafikone nije učitana.");
    console.error("LightweightCharts is not available; the chart is disabled");
    return {
      select() {},
      handleKline() {},
      handleNoteMessage() {},
      handleAlertTriggered() {},
      setStale() {},
    };
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
    // A note or alert in a candle that had not started yet gets its marker once the candle exists.
    if (newCandle && (state.notes.size > 0 || state.alerts.size > 0)) renderMarkers();
  }

  // ---------- Markers (notes and fired alerts) ----------

  const EMPTY_GROUP = { notes: [], alerts: [] };

  // candle time -> shown notes and alerts in that candle, each oldest first.
  // Candles not on the chart are left out.
  function markersByCandle() {
    const groups = new Map();
    function add(kind, item) {
      const time = candleTime(item.time_ms);
      if (state.lastBarTime === null || time > state.lastBarTime) return;
      if (!groups.has(time)) groups.set(time, { notes: [], alerts: [] });
      groups.get(time)[kind].push(item);
    }
    const byTime = (a, b) => a.time_ms - b.time_ms || a.id - b.id;
    if (state.show.notes) {
      for (const note of [...state.notes.values()].sort(byTime)) add("notes", note);
    }
    if (state.show.alerts) {
      for (const alert of [...state.alerts.values()].sort(byTime)) add("alerts", alert);
    }
    return groups;
  }

  function noteMarker(time, notes) {
    return {
      time,
      position: "aboveBar",
      shape: "circle",
      color: MARKER_COLOR,
      text: notes.length > 1 ? String(notes.length) : "",
    };
  }

  function alertMarker(time, alert) {
    const { arrow, position, shape } = ALERT_SHAPES[alert.condition];
    const text = `${arrow} ${formatEuro(alert.triggered_price)}`;
    return { time, position, shape, color: ALERT_COLOR, text };
  }

  // formatPrice() comes from app.js, which loads after this file; it only runs once data arrives.
  function formatEuro(value) {
    return `${formatPrice(value)} €`;
  }

  function renderMarkers() {
    const groups = markersByCandle();
    const list = [];
    for (const [time, { notes, alerts }] of groups) {
      if (notes.length > 0) list.push(noteMarker(time, notes));
      for (const alert of alerts) list.push(alertMarker(time, alert));
    }
    // Notes and alerts come from two sorted lists; the library needs one ascending list.
    // The sort is stable, so within a candle the note marker stays before the alerts.
    list.sort((a, b) => a.time - b.time);
    markers.setMarkers(list);
    renderHoverList(groups.get(state.hoverTime) ?? EMPTY_GROUP);
  }

  function hoverTime(ms) {
    const time = document.createElement("time");
    time.dateTime = new Date(ms).toISOString();
    time.textContent = noteTimeFormat.format(ms);
    return time;
  }

  function renderHoverList({ notes, alerts }) {
    const noteItems = notes.map((note) => {
      const item = document.createElement("li");
      const text = document.createElement("span");
      // textContent, never innerHTML: note text is user input.
      text.textContent = note.text;
      item.append(hoverTime(note.time_ms), text);
      return item;
    });
    const alertItems = alerts.map((alert) => {
      const item = document.createElement("li");
      item.className = "is-alert";
      const { arrow, word } = ALERT_SHAPES[alert.condition];
      const text = document.createElement("span");
      text.textContent =
        `${arrow} Alert ${word} ${formatEuro(alert.target_price)} — ` +
        `okinut na ${formatEuro(alert.triggered_price)}`;
      item.append(hoverTime(alert.time_ms), text);
      return item;
    });
    const items = [...noteItems, ...alertItems];
    elements.notes.replaceChildren(...items);
    elements.notes.hidden = items.length === 0;
  }

  function resetMarkers() {
    // Every load starts a new markers object; the old one must not stay on the series.
    markers.detach();
    markers = createSeriesMarkers(series, []);
    state.notes = new Map();
    state.notesFromMs = null;
    state.notesLoading = true;
    state.noteBuffer = [];
    state.alerts = new Map();
    state.alertsFromMs = null;
    state.alertsLoading = true;
    state.alertBuffer = [];
    renderHoverList(EMPTY_GROUP);
    renderNotePanel();
  }

  function stopMarkerLoading() {
    state.notesLoading = false;
    state.noteBuffer = [];
    state.alertsLoading = false;
    state.alertBuffer = [];
  }

  function renderMarkerButtons() {
    for (const button of elements.markerButtons) {
      button.setAttribute("aria-pressed", String(state.show[button.dataset.markerKind]));
    }
  }

  function loadMarkerToggles() {
    try {
      const saved = JSON.parse(localStorage.getItem(MARKERS_STORAGE_KEY));
      for (const kind of Object.keys(state.show)) {
        if (typeof saved?.[kind] === "boolean") state.show[kind] = saved[kind];
      }
    } catch (error) {
      console.warn("Could not read the saved chart marker toggles", error);
    }
    renderMarkerButtons();
  }

  function toggleMarkers(kind) {
    state.show[kind] = !state.show[kind];
    try {
      localStorage.setItem(MARKERS_STORAGE_KEY, JSON.stringify(state.show));
    } catch (error) {
      console.warn("Could not save the chart marker toggles", error);
    }
    renderMarkerButtons();
    renderMarkers();
  }

  // ---------- Fired alerts ----------

  // SQLite drops the time zone, so triggered_at may come without an offset; it is always UTC.
  function parseUtc(iso) {
    return Date.parse(/(Z|[+-]\d\d:\d\d)$/i.test(iso) ? iso : `${iso}Z`);
  }

  function isAlertForChart(alert, timeMs) {
    return (
      alert.symbol === state.symbol &&
      state.alertsFromMs !== null &&
      Number.isFinite(timeMs) &&
      timeMs >= state.alertsFromMs
    );
  }

  function applyAlert(alert) {
    if (alert.triggered_at === null || !(alert.condition in ALERT_SHAPES)) return;
    const timeMs = parseUtc(alert.triggered_at);
    if (isAlertForChart(alert, timeMs)) state.alerts.set(alert.id, { ...alert, time_ms: timeMs });
  }

  async function loadAlerts(symbol, loadId, fromMs) {
    let alerts;
    try {
      // No triggered_to_ms: like notes, the range stays open while live candles extend it.
      const params = new URLSearchParams({ symbol, triggered_from_ms: String(fromMs) });
      const response = await fetch(`/api/alerts?${params}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      alerts = await response.json();
    } catch (error) {
      if (loadId !== state.loadId) return;
      // The chart and notes still work; only the alert markers are missing.
      console.warn(`Could not load fired alerts for ${symbol}`, error);
      state.alertsLoading = false;
      state.alertBuffer = [];
      return;
    }
    if (loadId !== state.loadId) return;

    state.alertsFromMs = fromMs;
    state.alerts = new Map();
    for (const alert of alerts) applyAlert(alert);
    state.alertsLoading = false;
    const buffered = state.alertBuffer;
    state.alertBuffer = [];
    for (const alert of buffered) applyAlert(alert);
    renderMarkers();
  }

  function handleAlertTriggered(alert) {
    if (state.alertsLoading) {
      state.alertBuffer.push(alert);
      return;
    }
    if (state.alertsFromMs === null) return;
    applyAlert(alert);
    renderMarkers();
  }

  // ---------- Notes ----------

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
      showPanelError("Beleške trenutno nisu dostupne.");
      return;
    }
    if (loadId !== state.loadId) return;

    state.notesFromMs = fromMs;
    state.notes = new Map(notes.map((note) => [note.id, note]));
    state.notesLoading = false;
    const buffered = state.noteBuffer;
    state.noteBuffer = [];
    for (const message of buffered) applyNoteMessage(message);
    showPanelError(null);
    renderMarkers();
    renderNotePanel();
  }

  function handleNoteMessage(message) {
    if (state.notesLoading) {
      state.noteBuffer.push(message);
      return;
    }
    // Without a loaded range (no candles or a failed load) there is nothing to draw on.
    if (state.notesFromMs === null) return;
    applyNoteMessage(message);
    renderMarkers();
    renderNotePanel();
  }

  function handleCrosshairMove(param) {
    state.hoverTime = param.time ?? null;
    renderHoverList(markersByCandle().get(state.hoverTime) ?? EMPTY_GROUP);
  }

  // ---------- Adding, editing and deleting notes ----------

  // The server counts code points (Python len), not UTF-16 units, so "😀" is one character.
  function noteLength(text) {
    return [...text.trim()].length;
  }

  // Message for the user if the text would be rejected anyway, otherwise null.
  function checkNoteText(text) {
    if (text.trim() === "") return "Tekst ne sme biti prazan.";
    if (noteLength(text) > NOTE_MAX_LENGTH) {
      return `Tekst može imati najviše ${NOTE_MAX_LENGTH} znakova.`;
    }
    return null;
  }

  async function errorMessage(response) {
    if (response.status === 429) {
      const seconds = response.headers.get("Retry-After");
      return seconds
        ? `Previše zahteva. Pokušaj ponovo za ${seconds} s.`
        : "Previše zahteva. Pokušaj ponovo malo kasnije.";
    }
    let detail = null;
    try {
      ({ detail } = await response.json());
    } catch (error) {
      console.warn(`Note request failed with HTTP ${response.status} and no JSON body`, error);
    }
    if (response.status === 422 && Array.isArray(detail)) {
      const messages = detail.map((item) => {
        const match = VALIDATION_MESSAGES.find(([key]) => String(item.msg).includes(key));
        return match ? match[1] : "Podaci beleške nisu ispravni.";
      });
      return [...new Set(messages)].join(" ");
    }
    // 400, 403, 404 and the route's own 422 carry a detail written for the user.
    if (typeof detail === "string") return detail;
    return `Zahtev nije uspeo (HTTP ${response.status}).`;
  }

  // Returns null on success, otherwise a message for the user.
  async function sendNoteRequest(url, options) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      console.warn(`Note request to ${url} failed`, error);
      return "Server nije dostupan. Pokušaj ponovo.";
    }
    return response.ok ? null : errorMessage(response);
  }

  function jsonRequest(method, body) {
    return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  function showFormError(message) {
    elements.formError.textContent = message ?? "";
    elements.formError.hidden = message === null;
  }

  function showPanelError(message) {
    elements.panelError.textContent = message ?? "";
    elements.panelError.hidden = message === null;
  }

  function setPicking(picking) {
    state.picking = picking;
    elements.noteAdd.setAttribute("aria-pressed", String(picking));
    elements.container.classList.toggle("is-picking", picking);
    elements.pickHint.hidden = !picking;
  }

  function handleChartClick(param) {
    // param.time is only set when the click hits a candle.
    if (!state.picking || !Number.isInteger(param.time)) return;
    setPicking(false);
    openNoteForm(param.time * 1000);
  }

  function updateNoteCount() {
    const length = noteLength(elements.formText.value);
    elements.formCount.textContent = `${length}/${NOTE_MAX_LENGTH}`;
    elements.formCount.classList.toggle("is-over", length > NOTE_MAX_LENGTH);
  }

  function openNoteForm(timeMs) {
    // Picking another moment while the form is open keeps the text typed so far.
    if (elements.form.hidden) elements.formText.value = "";
    state.pickedMs = timeMs;
    elements.formLocal.textContent = `${localTimeFormat.format(timeMs)} (lokalno)`;
    elements.formUtc.textContent = `${noteTimeFormat.format(timeMs)} UTC`;
    updateNoteCount();
    showFormError(null);
    elements.form.hidden = false;
    elements.formText.focus();
  }

  function closeNoteForm() {
    state.pickedMs = null;
    elements.form.hidden = true;
    showFormError(null);
  }

  async function saveNewNote() {
    if (state.saving || state.pickedMs === null) return;
    const text = elements.formText.value;
    const invalid = checkNoteText(text);
    if (invalid !== null) {
      showFormError(invalid);
      return;
    }
    state.saving = true;
    elements.formSubmit.disabled = true;
    const body = { symbol: state.symbol, time_ms: state.pickedMs, text: text.trim() };
    const error = await sendNoteRequest("/api/notes", jsonRequest("POST", body));
    state.saving = false;
    elements.formSubmit.disabled = false;
    if (error === null) {
      closeNoteForm();
    } else {
      showFormError(error);
    }
  }

  function startEdit(note) {
    state.editingId = note.id;
    state.editDraft = note.text;
    state.confirmingId = null;
    showPanelError(null);
    renderNotePanel({ focusEdit: true });
  }

  function stopEdit() {
    state.editingId = null;
    showPanelError(null);
    renderNotePanel();
  }

  async function saveEdit() {
    const id = state.editingId;
    if (id === null || state.pendingId !== null) return;
    const invalid = checkNoteText(state.editDraft);
    if (invalid !== null) {
      showPanelError(invalid);
      return;
    }
    const text = state.editDraft.trim();
    if (text === state.notes.get(id)?.text) {
      stopEdit();
      return;
    }
    state.pendingId = id;
    renderNotePanel();
    const error = await sendNoteRequest(`/api/notes/${id}`, jsonRequest("PATCH", { text }));
    state.pendingId = null;
    if (error === null && state.editingId === id) state.editingId = null;
    showPanelError(error);
    renderNotePanel({ focusEdit: error !== null });
  }

  async function deleteNote(id) {
    if (state.pendingId !== null) return;
    state.pendingId = id;
    renderNotePanel();
    const error = await sendNoteRequest(`/api/notes/${id}`, { method: "DELETE" });
    state.pendingId = null;
    if (error === null) state.confirmingId = null;
    showPanelError(error);
    renderNotePanel();
  }

  // Centers the chart on the note's candle and keeps the current zoom.
  function scrollToNote(note) {
    const timeScale = chart.timeScale();
    const range = timeScale.getVisibleRange();
    if (range === null) return;
    const time = candleTime(note.time_ms);
    const half = (range.to - range.from) / 2;
    timeScale.setVisibleRange({ from: time - half, to: time + half });
  }

  function smallButton(label, onClick, disabled) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "note-button";
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", onClick);
    return button;
  }

  function noteTimeElement(note) {
    const time = document.createElement("time");
    time.dateTime = new Date(note.time_ms).toISOString();
    time.textContent = noteTimeFormat.format(note.time_ms);
    return time;
  }

  function editInput(pending) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "note-edit-input";
    input.autocomplete = "off";
    input.setAttribute("aria-label", "Novi tekst beleške");
    input.value = state.editDraft;
    input.readOnly = pending;
    input.addEventListener("input", () => {
      state.editDraft = input.value;
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        saveEdit();
      } else if (event.key === "Escape") {
        // Must not also leave the add-note mode through the document handler.
        event.stopPropagation();
        stopEdit();
      }
    });
    return input;
  }

  function notePanelItem(note) {
    const item = document.createElement("li");
    const pending = state.pendingId !== null;

    if (state.editingId === note.id) {
      item.append(
        noteTimeElement(note),
        editInput(pending),
        smallButton("Sačuvaj", saveEdit, pending),
        smallButton("Otkaži", stopEdit, pending),
      );
      return item;
    }

    const main = document.createElement("button");
    main.type = "button";
    main.className = "note-item-main";
    main.title = "Prikaži na grafikonu";
    const text = document.createElement("span");
    // textContent, never innerHTML: note text is user input.
    text.textContent = note.text;
    main.append(noteTimeElement(note), text);
    main.addEventListener("click", () => scrollToNote(note));
    item.append(main);
    if (!state.writeEnabled) return item;

    if (state.confirmingId === note.id) {
      const question = document.createElement("span");
      question.className = "note-confirm";
      question.textContent = "Obrisati belešku?";
      item.append(
        question,
        smallButton("Obriši", () => deleteNote(note.id), pending),
        smallButton("Ne", () => {
          state.confirmingId = null;
          renderNotePanel();
        }, pending),
      );
    } else {
      item.append(
        smallButton("Izmeni", () => startEdit(note), pending),
        smallButton("Obriši", () => {
          state.confirmingId = note.id;
          state.editingId = null;
          showPanelError(null);
          renderNotePanel();
        }, pending),
      );
    }
    return item;
  }

  function renderNotePanel({ focusEdit = false } = {}) {
    // Someone else may have deleted the note that is being edited or confirmed.
    if (!state.notes.has(state.editingId)) state.editingId = null;
    if (!state.notes.has(state.confirmingId)) state.confirmingId = null;
    if (!state.writeEnabled) {
      state.editingId = null;
      state.confirmingId = null;
    }

    // A live note message rebuilds the list; the edit field must keep focus and caret.
    const active = document.activeElement;
    const hadFocus = active instanceof HTMLInputElement && active.classList.contains("note-edit-input");
    const selection = hadFocus ? [active.selectionStart, active.selectionEnd] : null;

    const notes = [...state.notes.values()].sort((a, b) => b.time_ms - a.time_ms || b.id - a.id);
    elements.panelList.replaceChildren(...notes.map(notePanelItem));
    elements.panelEmpty.hidden = notes.length > 0;

    const input = elements.panelList.querySelector(".note-edit-input");
    if (input === null || !(hadFocus || focusEdit)) return;
    input.focus();
    if (selection !== null) input.setSelectionRange(...selection);
  }

  async function loadNotesConfig() {
    try {
      const response = await fetch("/api/notes/config");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const config = await response.json();
      state.writeEnabled = config.write_enabled === true;
    } catch (error) {
      console.warn("Could not load the notes config; notes stay read-only", error);
      state.writeEnabled = false;
    }
    elements.noteAdd.hidden = !state.writeEnabled;
    renderNotePanel();
  }

  async function loadHistory(symbol, interval) {
    const loadId = ++state.loadId;
    state.loading = true;
    state.buffer = [];
    state.lastBarTime = null;
    series.setData([]);
    resetMarkers();
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
      stopMarkerLoading();
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
      const fromMs = bars[0].time * 1000;
      loadNotes(symbol, loadId, fromMs);
      loadAlerts(symbol, loadId, fromMs);
    } else {
      stopMarkerLoading();
    }
  }

  // Exports the stored 1m candles of the visible time range, whatever the chart interval is.
  function exportCandles() {
    const range = chart.timeScale().getVisibleRange();
    if (state.symbol === null || range === null) {
      elements.exportCandlesError.textContent = "Nema prikazanih sveća za izvoz.";
      elements.exportCandlesError.hidden = false;
      return;
    }
    const intervalMs = INTERVAL_MS[state.interval];
    const params = new URLSearchParams({
      symbol: state.symbol,
      interval: "1m",
      from_ms: String(Math.max(range.from * 1000, 0)),
      // The last visible candle counts as a whole.
      to_ms: String(range.to * 1000 + intervalMs - 1),
    });
    csvExport.download(
      `/api/export/candles.csv?${params}`,
      elements.exportCandles,
      elements.exportCandlesError,
    );
  }

  function exportNotes() {
    if (state.symbol === null) return;
    const params = new URLSearchParams({ symbol: state.symbol });
    csvExport.download(
      `/api/export/notes.csv?${params}`,
      elements.exportNotes,
      elements.exportNotesError,
    );
  }

  function reload() {
    if (state.symbol !== null) loadHistory(state.symbol, state.interval);
  }

  function select(symbol, displaySymbol) {
    if (symbol === state.symbol) return;
    state.symbol = symbol;
    elements.symbol.textContent = displaySymbol;
    elements.panelSymbol.textContent = displaySymbol;
    elements.exportCandles.disabled = false;
    elements.exportNotes.disabled = false;
    elements.exportCandlesError.hidden = true;
    elements.exportNotesError.hidden = true;
    // A picked moment or half-typed note belongs to the previous pair.
    setPicking(false);
    closeNoteForm();
    showPanelError(null);
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
  for (const button of elements.markerButtons) {
    button.addEventListener("click", () => toggleMarkers(button.dataset.markerKind));
  }

  elements.noteAdd.addEventListener("click", () => setPicking(!state.picking));
  elements.exportCandles.addEventListener("click", exportCandles);
  elements.exportNotes.addEventListener("click", exportNotes);
  elements.formText.addEventListener("input", updateNoteCount);
  elements.formText.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    closeNoteForm();
  });
  elements.formCancel.addEventListener("click", closeNoteForm);
  // Enter in the text field submits the form.
  elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveNewNote();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.picking) setPicking(false);
  });

  chart.subscribeCrosshairMove(handleCrosshairMove);
  chart.subscribeClick(handleChartClick);
  loadMarkerToggles();
  loadNotesConfig();

  return { select, handleKline, handleNoteMessage, handleAlertTriggered, setStale };
})();
