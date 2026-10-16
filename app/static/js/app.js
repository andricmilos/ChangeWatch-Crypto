"use strict";

// Prices arrive as strings ("113250.55"). Number() is used only to format them for display,
// never to compare them: equality checks and signs are decided on the strings themselves.

const STALE_AFTER_MS = 15000;
const RECONNECT_INITIAL_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const LOCALE = "sr-Latn-RS";
const MISSING = "—";

const rows = new Map();
const cards = new Map();
for (const row of document.querySelectorAll("#markets-body tr[data-symbol]")) {
  rows.set(row.dataset.symbol, row);
}
for (const card of document.querySelectorAll(".card[data-symbol]")) {
  cards.set(card.dataset.symbol, card);
}

const elements = {
  indicator: document.getElementById("connection-indicator"),
  indicatorLabel: document.getElementById("connection-label"),
  lastUpdate: document.getElementById("last-update"),
  serverBanner: document.getElementById("banner-server"),
  exchangeBanner: document.getElementById("banner-exchange"),
  loading: document.getElementById("loading"),
  search: document.getElementById("search"),
  onlyWatchlist: document.getElementById("only-watchlist"),
  watchlist: document.getElementById("watchlist"),
  watchlistEmpty: document.getElementById("watchlist-empty"),
  watchlistError: document.getElementById("watchlist-error"),
};

const state = {
  // symbol -> { market, receivedAt }; receivedAt is null when the data is not current.
  markets: new Map(),
  socketState: "connecting", // "connecting" | "open" | "closed"
  exchangeStatus: null,
  reconnectDelayMs: RECONNECT_INITIAL_DELAY_MS,
  lastUpdateMs: null,
  selectedSymbol: null,
  // Symbols in the order they were added; a Set keeps insertion order.
  watchlist: new Set(),
  // Symbols with a POST/DELETE in flight, so a double click sends only one request.
  pendingStars: new Set(),
};

// ---------- Formatting ----------

const numberFormats = new Map();

function numberFormat(fractionDigits, signed) {
  const key = `${fractionDigits}:${signed}`;
  if (!numberFormats.has(key)) {
    numberFormats.set(
      key,
      new Intl.NumberFormat(LOCALE, {
        minimumFractionDigits: Math.min(2, fractionDigits),
        maximumFractionDigits: fractionDigits,
        signDisplay: signed ? "exceptZero" : "auto",
      }),
    );
  }
  return numberFormats.get(key);
}

function isMissing(value) {
  return value === null || value === undefined || value === "";
}

function priceDigits(value) {
  // Cheap coins need more decimals; decided from the string so no float comparison is needed.
  const integerPart = value.replace("-", "").split(".")[0].replace(/^0+/, "");
  if (integerPart.length >= 4) return 2;
  if (integerPart.length >= 1) return 4;
  return 6;
}

function formatNumber(value, fractionDigits, signed = false) {
  if (isMissing(value)) return MISSING;
  const number = Number(value);
  if (!Number.isFinite(number)) return MISSING;
  return numberFormat(fractionDigits, signed).format(number);
}

function formatPrice(value, signed = false) {
  if (isMissing(value)) return MISSING;
  return formatNumber(value, priceDigits(value), signed);
}

function formatPercent(value) {
  const formatted = formatNumber(value, 2, true);
  return formatted === MISSING ? MISSING : `${formatted}%`;
}

function direction(value) {
  if (isMissing(value) || /^-?0*\.?0*$/.test(value)) return null;
  return value.startsWith("-") ? "negative" : "positive";
}

function formatTime(ms) {
  return new Date(ms).toLocaleTimeString(LOCALE, { hour12: false });
}

// ---------- Rendering ----------

function setCell(container, field, text, signValue) {
  const cell = container.querySelector(`[data-field="${field}"]`);
  if (!cell) return null;
  const changed = cell.textContent !== text;
  cell.textContent = text;
  if (signValue !== undefined) {
    const sign = direction(signValue);
    cell.classList.toggle("positive", sign === "positive");
    cell.classList.toggle("negative", sign === "negative");
  }
  return changed ? cell : null;
}

function flash(element) {
  element.classList.remove("flash");
  // Reading offsetWidth restarts the animation when prices change faster than 600 ms.
  void element.offsetWidth;
  element.classList.add("flash");
}

function renderRow(market) {
  const row = rows.get(market.symbol);
  if (!row) return;

  const previous = state.markets.get(market.symbol);
  const priceChanged =
    previous !== undefined &&
    !isMissing(market.last_price) &&
    previous.market.last_price !== market.last_price;
  state.markets.set(market.symbol, {
    market,
    receivedAt: market.freshness === "live" ? Date.now() : null,
  });

  const priceCell = setCell(row, "last_price", formatPrice(market.last_price));
  setCell(row, "price_change", formatPrice(market.price_change, true), market.price_change);
  setCell(
    row,
    "price_change_percent",
    formatPercent(market.price_change_percent),
    market.price_change_percent,
  );
  setCell(row, "high_24h", formatPrice(market.high_24h));
  setCell(row, "low_24h", formatPrice(market.low_24h));
  setCell(row, "volume_24h", formatNumber(market.volume_24h, 2));
  if (priceChanged && priceCell) flash(priceCell);

  const card = cards.get(market.symbol);
  if (card) {
    const cardPrice = setCell(card, "last_price", formatPrice(market.last_price));
    setCell(
      card,
      "price_change_percent",
      formatPercent(market.price_change_percent),
      market.price_change_percent,
    );
    if (priceChanged && cardPrice) flash(cardPrice);
  }

  renderWatchlistPrice(market.symbol);
  renderStatus(market.symbol);
  updateLastUpdate(market.updated_at);
}

function rowStatus(symbol) {
  const entry = state.markets.get(symbol);
  if (!entry || isMissing(entry.market.last_price)) return "no-data";
  if (entry.receivedAt === null || Date.now() - entry.receivedAt > STALE_AFTER_MS) {
    return "stale";
  }
  return "live";
}

const STATUS_LABELS = { live: "Uživo", stale: "Zastarelo", "no-data": "Nema podataka" };

function renderStatus(symbol) {
  const row = rows.get(symbol);
  const status = rowStatus(symbol);
  if (row.dataset.status === status) return;
  row.dataset.status = status;

  const badge = document.createElement("span");
  badge.className = `status status-${status}`;
  badge.textContent = STATUS_LABELS[status];
  row.querySelector('[data-field="status"]').replaceChildren(badge);

  const stale = status === "stale";
  row.classList.toggle("is-stale", stale);
  cards.get(symbol)?.classList.toggle("is-stale", stale);
  watchlistItem(symbol)?.classList.toggle("is-stale", stale);
}

function markAllStale() {
  for (const entry of state.markets.values()) entry.receivedAt = null;
  for (const symbol of rows.keys()) renderStatus(symbol);
}

function updateLastUpdate(updatedAt) {
  if (isMissing(updatedAt)) return;
  const ms = Date.parse(updatedAt);
  if (Number.isNaN(ms) || (state.lastUpdateMs !== null && ms <= state.lastUpdateMs)) return;
  state.lastUpdateMs = ms;
  elements.lastUpdate.textContent = formatTime(ms);
}

function renderIndicator() {
  let kind = "connecting";
  let label = "Povezivanje…";
  if (state.socketState === "closed" || state.exchangeStatus === "stopped") {
    kind = "disconnected";
    label = "Prekinuto";
  } else if (state.socketState === "open" && state.exchangeStatus === "connected") {
    kind = "live";
    label = "Uživo";
  }
  elements.indicator.className = `indicator indicator-${kind}`;
  elements.indicatorLabel.textContent = label;
  renderChartStale();
}

function renderChartStale() {
  const exchangeDown = state.exchangeStatus !== null && state.exchangeStatus !== "connected";
  const stale =
    state.socketState === "closed" ||
    exchangeDown ||
    (state.selectedSymbol !== null && rowStatus(state.selectedSymbol) === "stale");
  chartPanel.setStale(stale);
}

// ---------- Chart selection ----------

function selectPair(symbol) {
  const row = rows.get(symbol);
  if (!row) return;
  state.selectedSymbol = symbol;
  for (const other of rows.values()) other.classList.toggle("is-selected", other === row);
  chartPanel.select(symbol, row.querySelector(".pair").textContent);
  renderChartStale();
}

function setExchangeStatus(status) {
  state.exchangeStatus = status;
  elements.exchangeBanner.hidden = status !== "reconnecting";
  // Without a live exchange connection no price on screen is current.
  if (status !== "connected") markAllStale();
  renderIndicator();
}

// ---------- Watchlist ----------

function displaySymbol(symbol) {
  return rows.get(symbol).querySelector(".pair").textContent;
}

function watchlistItem(symbol) {
  return elements.watchlist.querySelector(`[data-symbol="${symbol}"]`);
}

function renderWatchlistPrice(symbol) {
  const item = watchlistItem(symbol);
  const market = state.markets.get(symbol)?.market;
  if (!item || !market) return;
  const priceCell = setCell(item, "last_price", formatPrice(market.last_price));
  setCell(
    item,
    "price_change_percent",
    formatPercent(market.price_change_percent),
    market.price_change_percent,
  );
  if (priceCell && item.dataset.rendered === "true") flash(priceCell);
  item.dataset.rendered = "true";
}

function createWatchlistItem(symbol) {
  const item = document.createElement("li");
  item.className = "watchlist-item";
  item.dataset.symbol = symbol;
  for (const [className, field] of [
    ["pair", null],
    ["num", "last_price"],
    ["num", "price_change_percent"],
  ]) {
    const cell = document.createElement("span");
    cell.className = className;
    if (field === null) {
      cell.textContent = displaySymbol(symbol);
    } else {
      cell.dataset.field = field;
      cell.textContent = MISSING;
    }
    item.append(cell);
  }
  return item;
}

function renderWatchlist() {
  for (const [symbol, row] of rows) {
    const watched = state.watchlist.has(symbol);
    const star = row.querySelector(".star");
    star.textContent = watched ? "★" : "☆";
    star.setAttribute("aria-pressed", String(watched));
    star.setAttribute(
      "aria-label",
      `${watched ? "Ukloni" : "Dodaj"} ${displaySymbol(symbol)} ${watched ? "iz" : "u"} watchlist`,
    );
    star.disabled = state.pendingStars.has(symbol);
    row.classList.toggle("is-watched", watched);
  }

  elements.watchlist.replaceChildren(
    ...[...state.watchlist].filter((symbol) => rows.has(symbol)).map(createWatchlistItem),
  );
  for (const symbol of state.watchlist) {
    renderWatchlistPrice(symbol);
    watchlistItem(symbol)?.classList.toggle("is-stale", rowStatus(symbol) === "stale");
  }
  elements.watchlistEmpty.hidden = state.watchlist.size > 0;
  applyFilter();
}

function showWatchlistError(message) {
  elements.watchlistError.textContent = message;
  elements.watchlistError.hidden = message === null;
}

async function loadWatchlist() {
  try {
    const response = await fetch("/api/watchlist");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const items = await response.json();
    state.watchlist = new Set(items.map((item) => item.symbol));
    showWatchlistError(null);
  } catch (error) {
    console.error("Could not load the watchlist", error);
    showWatchlistError("Watchlist trenutno nije dostupna.");
  }
  renderWatchlist();
}

async function toggleWatch(symbol) {
  if (state.pendingStars.has(symbol)) return;
  state.pendingStars.add(symbol);
  renderWatchlist();

  const watched = state.watchlist.has(symbol);
  try {
    const response = watched
      ? await fetch(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" })
      : await fetch("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol }),
        });
    // 409 / 404 mean the server is already in the state we wanted (e.g. another tab).
    if (watched && (response.ok || response.status === 404)) {
      state.watchlist.delete(symbol);
    } else if (!watched && (response.ok || response.status === 409)) {
      state.watchlist.add(symbol);
    } else {
      throw new Error(`HTTP ${response.status}`);
    }
    showWatchlistError(null);
  } catch (error) {
    console.error("Could not update the watchlist", error);
    showWatchlistError("Izmena watchlist-e nije uspela. Pokušaj ponovo.");
  } finally {
    state.pendingStars.delete(symbol);
    renderWatchlist();
  }
}

// ---------- Alerts ----------

const TOAST_DURATION_MS = 10000;

function showAlertToast(alert) {
  const verb = alert.condition === "above" ? "je prešao" : "je pao ispod";
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.textContent = `${alert.display_symbol} ${verb} ${formatPrice(alert.target_price)} USDT`;
  document.getElementById("toasts").append(toast);
  setTimeout(() => toast.remove(), TOAST_DURATION_MS);
}

function handleAlertTriggered(alert) {
  showAlertToast(alert);
  // The server renders the list, so reloading it moves the alert to "Okinuti".
  htmx.ajax("GET", "/partials/alerts", { target: "#alerts-list" });
}

// ---------- WebSocket ----------

function handleSnapshot(data) {
  elements.loading.hidden = true;
  setExchangeStatus(data.connection_status);
  for (const market of data.markets) renderRow(market);
}

function handleMessage(message) {
  switch (message.type) {
    case "snapshot":
      handleSnapshot(message.data);
      break;
    case "ticker":
      renderRow(message.data);
      break;
    case "status":
      setExchangeStatus(message.data.connection_status);
      break;
    case "kline":
      chartPanel.handleKline(message.data);
      break;
    case "alert_triggered":
      handleAlertTriggered(message.data);
      break;
    default:
      break;
  }
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/market`);

  socket.addEventListener("open", () => {
    state.socketState = "open";
    state.reconnectDelayMs = RECONNECT_INITIAL_DELAY_MS;
    elements.serverBanner.hidden = true;
    renderIndicator();
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      console.warn("Ignoring malformed message from /ws/market", error);
      return;
    }
    handleMessage(message);
  });

  socket.addEventListener("close", () => {
    state.socketState = "closed";
    elements.serverBanner.hidden = false;
    markAllStale();
    renderIndicator();
    setTimeout(connect, state.reconnectDelayMs);
    state.reconnectDelayMs = Math.min(state.reconnectDelayMs * 2, RECONNECT_MAX_DELAY_MS);
  });
}

// ---------- Search ----------

function applyFilter() {
  const query = elements.search.value.trim().toLowerCase();
  const onlyWatchlist = elements.onlyWatchlist.checked;
  for (const [symbol, row] of rows) {
    const matchesQuery = query === "" || row.dataset.search.toLowerCase().includes(query);
    row.hidden = !matchesQuery || (onlyWatchlist && !state.watchlist.has(symbol));
  }
}

elements.search.addEventListener("input", applyFilter);
elements.onlyWatchlist.addEventListener("change", applyFilter);
for (const [symbol, row] of rows) {
  row.addEventListener("click", () => selectPair(symbol));
  row.querySelector(".star").addEventListener("click", (event) => {
    // The star must not also open the chart for this row.
    event.stopPropagation();
    toggleWatch(symbol);
  });
}
setInterval(() => {
  for (const symbol of rows.keys()) renderStatus(symbol);
  renderChartStale();
}, 1000);
const firstSymbol = rows.keys().next().value;
if (firstSymbol !== undefined) selectPair(firstSymbol);
loadWatchlist();
connect();
