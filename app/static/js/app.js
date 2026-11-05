"use strict";

// The dashboard. Formatting comes from format.js and the /ws/market connection from ws.js.
// Prices arrive as strings; Number() is used only to format them for display.

// Live prices would otherwise reshuffle the sorted table every second.
const RESORT_MIN_INTERVAL_MS = 5000;
const COPY_LABEL_MS = 1500;
const COPY_HINT = "Klikni da kopiraš";
const SORT_STORAGE_KEY = "changewatch.sort";
const SORT_KEYS = [
  "pair",
  "last_price",
  "change_1h_percent",
  "change_4h_percent",
  "price_change_percent",
  "volume_24h",
];

const rows = new Map();
const cards = new Map();
for (const row of document.querySelectorAll("#markets-body tr[data-symbol]")) {
  rows.set(row.dataset.symbol, row);
}
for (const card of document.querySelectorAll(".card[data-symbol]")) {
  cards.set(card.dataset.symbol, card);
}

const elements = {
  loading: document.getElementById("loading"),
  search: document.getElementById("search"),
  onlyWatchlist: document.getElementById("only-watchlist"),
  watchlist: document.getElementById("watchlist"),
  watchlistEmpty: document.getElementById("watchlist-empty"),
  watchlistError: document.getElementById("watchlist-error"),
  marketsBody: document.getElementById("markets-body"),
  marketsEmpty: document.getElementById("markets-empty"),
  marketsEmptyMessage: document.getElementById("markets-empty-message"),
  marketsEmptyClear: document.getElementById("markets-empty-clear"),
  tableWrap: document.querySelector(".table-wrap"),
  sortButtons: document.querySelectorAll(".markets .sort"),
  shortcuts: document.getElementById("shortcuts"),
};

const state = {
  // symbol -> { market }
  markets: new Map(),
  selectedSymbol: null,
  // Symbols in the order they were added; a Set keeps insertion order.
  watchlist: new Set(),
  // Symbols with a POST/DELETE in flight, so a double click sends only one request.
  pendingStars: new Set(),
  // { key, direction: "desc" | "asc" } or null when the table keeps its original order.
  sort: null,
  sortedAtMs: 0,
  // New data arrived since the last reorder.
  sortDirty: false,
  pointerOverTable: false,
};

function renderRow(market) {
  const row = rows.get(market.symbol);
  if (!row) return;

  const previous = state.markets.get(market.symbol);
  const priceChanged =
    previous !== undefined &&
    !isMissing(market.last_price) &&
    previous.market.last_price !== market.last_price;
  state.markets.set(market.symbol, { market });

  const symbol = market.symbol;
  const priceCell = setCell(row, "last_price", formatPrice(market.last_price, false, symbol));
  setCell(
    row,
    "price_change",
    formatPrice(market.price_change, true, symbol),
    market.price_change,
  );
  setCell(
    row,
    "price_change_percent",
    formatPercent(market.price_change_percent),
    market.price_change_percent,
  );
  for (const field of ["change_1h_percent", "change_4h_percent"]) {
    setCell(row, field, formatPercent(market[field]), market[field]);
  }
  setCell(row, "high_24h", formatPrice(market.high_24h, false, symbol));
  setCell(row, "low_24h", formatPrice(market.low_24h, false, symbol));
  setCell(row, "volume_24h", formatQuantity(market.volume_24h, symbol));
  setCell(row, "bid_price", formatPrice(market.bid_price, false, symbol));
  setCell(row, "ask_price", formatPrice(market.ask_price, false, symbol));
  renderSpread(row, market);
  if (priceChanged && priceCell) flash(priceCell);

  const card = cards.get(market.symbol);
  if (card) {
    const cardPrice = setCell(card, "last_price", formatPrice(market.last_price, false, symbol));
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
  renderPriceAge(market.symbol);
  state.sortDirty = true;
}

function renderSpread(row, market) {
  setCell(row, "spread_percent", formatSpreadPercent(market.spread_percent));
  const cell = row.querySelector('[data-field="spread_percent"]');
  const high = market.high_spread === true;
  cell.classList.toggle("spread-high", high);
  if (high) {
    cell.title = HIGH_SPREAD_TOOLTIP;
  } else {
    cell.removeAttribute("title");
  }
}

function rowStatus(symbol) {
  return marketSocket.freshness(state.markets.get(symbol)?.market);
}

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

function renderPriceAge(symbol) {
  const cell = rows.get(symbol).querySelector('[data-field="last_price"]');
  const updatedAt = state.markets.get(symbol)?.market.updated_at;
  const ms = isMissing(updatedAt) ? NaN : Date.parse(updatedAt);
  const age = Number.isNaN(ms)
    ? `Ažurirano ${MISSING}`
    : `Ažurirano ${formatRelative(Date.now() - ms)} (${formatTime(ms)})`;
  cell.title = `${age}\n${COPY_HINT}`;
}

function renderAllStatuses() {
  for (const symbol of rows.keys()) renderStatus(symbol);
}

function renderChartStale() {
  const stale =
    marketSocket.connectionDown() ||
    (state.selectedSymbol !== null && rowStatus(state.selectedSymbol) === "stale");
  chartPanel.setStale(stale);
}

// ---------- Chart selection ----------

function selectPair(symbol) {
  const row = rows.get(symbol);
  if (!row) return;
  state.selectedSymbol = symbol;
  for (const other of rows.values()) other.classList.toggle("is-selected", other === row);
  chartPanel.select(symbol, displaySymbol(symbol));
  orderBookPanel.select(symbol, displaySymbol(symbol));
  renderChartStale();
  writeChartUrl();
}

// ---------- Chart link ----------

// ?pair=ETHEUR&interval=15m mirrors the chart, so the address can be shared or bookmarked.
const URL_PAIR_PARAM = "pair";
const URL_INTERVAL_PARAM = "interval";

// Accepts "ethEUR" and "ETH-EUR"; null unless the server lists the pair.
function pairFromUrl(params) {
  const value = params.get(URL_PAIR_PARAM);
  if (value === null) return null;
  const symbol = value.trim().toUpperCase().replace("-", "");
  return rows.has(symbol) ? symbol : null;
}

// replaceState, not pushState: picking pairs must not fill the browser history. Other query
// parameters are kept; invalid pair or interval values are overwritten with the real choice.
function writeChartUrl() {
  const url = new URL(window.location.href);
  if (state.selectedSymbol === null) {
    url.searchParams.delete(URL_PAIR_PARAM);
  } else {
    url.searchParams.set(URL_PAIR_PARAM, state.selectedSymbol);
  }
  url.searchParams.set(URL_INTERVAL_PARAM, chartPanel.getInterval());
  history.replaceState(history.state, "", url);
}

// The URL wins over the default pair and interval; there is no saved choice to fall back on.
function applyChartUrl() {
  const params = new URLSearchParams(window.location.search);
  const interval = chartPanel.intervalFromUrl(params);
  // Before selectPair, so the chart loads its history only once.
  if (interval !== null) chartPanel.selectInterval(interval);
  const symbol = pairFromUrl(params) ?? rows.keys().next().value;
  if (symbol !== undefined) selectPair(symbol);
  writeChartUrl();
}

// ---------- Watchlist ----------

function displaySymbol(symbol) {
  // .pair-name, not .pair: the pair cell also holds the tag chips.
  return rows.get(symbol).querySelector(".pair-name").textContent;
}

function watchlistItem(symbol) {
  return elements.watchlist.querySelector(`[data-symbol="${symbol}"]`);
}

function renderWatchlistPrice(symbol) {
  const item = watchlistItem(symbol);
  const market = state.markets.get(symbol)?.market;
  if (!item || !market) return;
  const priceCell = setCell(item, "last_price", formatPrice(market.last_price, false, symbol));
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

// ---------- Precision ----------

async function loadMarketMeta() {
  if (!(await loadPrecision())) return;
  // Redraws what is already on screen without flashing it as a price change; everything
  // drawn after this uses the new decimals anyway.
  for (const { market } of state.markets.values()) renderRow(market);
  renderWatchlist();
}

// ---------- Alerts ----------

const TOAST_DURATION_MS = 10000;

function showAlertToast(alert) {
  const verb = alert.condition === "above" ? "je prešao" : "je pao ispod";
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  // The quote currency comes from the pair itself: "BTC/EUR" -> "EUR".
  const currency = alert.display_symbol.split("/")[1] ?? "";
  const price = formatPrice(alert.target_price, false, alert.symbol);
  toast.textContent = `${alert.display_symbol} ${verb} ${price} ${currency}`;
  document.getElementById("toasts").append(toast);
  setTimeout(() => toast.remove(), TOAST_DURATION_MS);
}

function handleAlertTriggered(alert) {
  showAlertToast(alert);
  chartPanel.handleAlertTriggered(alert);
  // The server renders the list, so reloading it moves the alert to "Okinuti".
  htmx.ajax("GET", "/partials/alerts", { target: "#alerts-list" });
}

// ---------- WebSocket ----------

function handleSnapshot(data) {
  elements.loading.hidden = true;
  for (const market of data.markets) renderRow(market);
}

function notifyPricesChanged() {
  // An event instead of a direct call, so converter.js stays optional and loads after this file.
  document.dispatchEvent(new CustomEvent("market:prices"));
}

function handleMessage(message) {
  switch (message.type) {
    case "snapshot":
      handleSnapshot(message.data);
      notifyPricesChanged();
      break;
    case "ticker":
      renderRow(message.data);
      notifyPricesChanged();
      break;
    case "kline":
      chartPanel.handleKline(message.data);
      break;
    case "note_created":
    case "note_updated":
    case "note_deleted":
      chartPanel.handleNoteMessage(message);
      break;
    case "alert_triggered":
      handleAlertTriggered(message.data);
      break;
    case "tags_updated":
    case "pair_meta_updated":
      pairMetaPanel.handleMessage(message);
      break;
    case "orderbook":
      orderBookPanel.handleOrderBook(message.data);
      break;
    case "error":
      orderBookPanel.handleError(message.data.detail);
      break;
    default:
      break;
  }
}

// ---------- Sorting ----------

function compareDecimalStrings(a, b) {
  // Exact comparison of "-0.21" / "72416.85" without going through a float.
  const negativeA = a.startsWith("-");
  const negativeB = b.startsWith("-");
  if (negativeA !== negativeB) return negativeA ? -1 : 1;
  const [intA, fracA = ""] = a.replace(/^[-+]/, "").split(".");
  const [intB, fracB = ""] = b.replace(/^[-+]/, "").split(".");
  const cleanA = intA.replace(/^0+/, "");
  const cleanB = intB.replace(/^0+/, "");
  const width = Math.max(fracA.length, fracB.length);
  const digitsA = fracA.padEnd(width, "0");
  const digitsB = fracB.padEnd(width, "0");
  let result;
  if (cleanA.length !== cleanB.length) {
    result = cleanA.length < cleanB.length ? -1 : 1;
  } else {
    // Same number of integer digits, so plain string order equals numeric order.
    const textA = cleanA + digitsA;
    const textB = cleanB + digitsB;
    result = textA === textB ? 0 : textA < textB ? -1 : 1;
  }
  return negativeA ? -result : result;
}

function sortValue(symbol, key) {
  if (key === "pair") return displaySymbol(symbol);
  const value = state.markets.get(symbol)?.market[key];
  return isMissing(value) ? null : value;
}

function compareRows(symbolA, symbolB) {
  const { key, direction } = state.sort;
  const a = sortValue(symbolA, key);
  const b = sortValue(symbolB, key);
  // Rows without a value go last in both directions.
  if (a === null || b === null) return (a === null) - (b === null);
  const result = key === "pair" ? a.localeCompare(b, LOCALE) : compareDecimalStrings(a, b);
  return direction === "desc" ? -result : result;
}

function applySort() {
  state.sortedAtMs = Date.now();
  state.sortDirty = false;
  if (state.sort === null) return;
  const sorted = [...rows.keys()].sort(compareRows);
  // append() moves existing rows, so their listeners and state stay intact.
  elements.marketsBody.append(...sorted.map((symbol) => rows.get(symbol)));
}

function maybeResort() {
  if (state.sort === null || !state.sortDirty || state.pointerOverTable) return;
  if (Date.now() - state.sortedAtMs < RESORT_MIN_INTERVAL_MS) return;
  applySort();
}

function renderSortHeaders() {
  for (const button of elements.sortButtons) {
    const active = state.sort?.key === button.dataset.sort;
    const ariaSort = !active
      ? "none"
      : state.sort.direction === "desc"
        ? "descending"
        : "ascending";
    button.closest("th").setAttribute("aria-sort", ariaSort);
  }
}

function loadSort() {
  try {
    const saved = JSON.parse(localStorage.getItem(SORT_STORAGE_KEY));
    if (SORT_KEYS.includes(saved?.key) && ["asc", "desc"].includes(saved?.direction)) {
      state.sort = { key: saved.key, direction: saved.direction };
    }
  } catch (error) {
    console.warn("Could not read the saved table sort", error);
  }
}

function saveSort() {
  try {
    localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(state.sort));
  } catch (error) {
    console.warn("Could not save the table sort", error);
  }
}

function changeSort(key) {
  const direction = state.sort?.key === key && state.sort.direction === "desc" ? "asc" : "desc";
  state.sort = { key, direction };
  saveSort();
  renderSortHeaders();
  // A click is a deliberate choice, so it reorders right away even under the cursor.
  applySort();
}

// ---------- Copy price ----------

// The label lives in a data attribute shown by CSS, because live updates overwrite the
// cell's text and would wipe any child element.
function showCopyLabel(cell, text) {
  clearTimeout(Number(cell.dataset.copyTimer));
  cell.dataset.copyLabel = text;
  cell.dataset.copyTimer = String(
    setTimeout(() => {
      delete cell.dataset.copyLabel;
      delete cell.dataset.copyTimer;
    }, COPY_LABEL_MS),
  );
}

async function copyPrice(symbol, cell) {
  const price = state.markets.get(symbol)?.market.last_price;
  if (isMissing(price)) return;
  try {
    // The raw server string, so it can be pasted straight into an alert or the calculator.
    await navigator.clipboard.writeText(price);
    showCopyLabel(cell, "Kopirano");
  } catch (error) {
    console.warn("Could not copy the price", error);
    showCopyLabel(cell, "Kopiranje nije uspelo");
  }
}

// ---------- Search ----------

function applyFilter() {
  const query = elements.search.value.trim().toLowerCase();
  const onlyWatchlist = elements.onlyWatchlist.checked;
  for (const [symbol, row] of rows) {
    const matchesQuery = query === "" || row.dataset.search.toLowerCase().includes(query);
    // Search, "Samo watchlist" and the tag filter must all hold.
    row.hidden =
      !matchesQuery ||
      (onlyWatchlist && !state.watchlist.has(symbol)) ||
      !pairMetaPanel.matches(symbol);
  }
  renderEmptyState();
}

function renderEmptyState() {
  const query = elements.search.value.trim();
  const filtered = query !== "" || elements.onlyWatchlist.checked || pairMetaPanel.hasFilter();
  const anyVisible = [...rows.values()].some((row) => !row.hidden);
  elements.marketsEmpty.hidden = !filtered || anyVisible;
  if (elements.marketsEmpty.hidden) return;
  // textContent, never innerHTML: the query is user input.
  elements.marketsEmptyMessage.textContent =
    query === "" ? "Nema parova za izabrane filtere." : `Nema parova za „${query}”.`;
}

function clearFilters() {
  elements.search.value = "";
  elements.onlyWatchlist.checked = false;
  // Calls back into applyFilter() when a tag filter was active.
  pairMetaPanel.clearFilter();
  applyFilter();
}

function isTypingTarget(target) {
  return target instanceof Element && target.matches("input, textarea, select");
}

// ---------- Keyboard shortcuts ----------

const INTERVAL_KEYS = { 1: "1m", 2: "5m", 3: "15m", 4: "1h" };
// Panels and forms that own the keyboard while they are open.
const BLOCKING_PANEL_SELECTOR =
  "#pair-panel:not([hidden]), #tags-manager:not([hidden]), #note-form:not([hidden]), " +
  "#note-pick-hint:not([hidden]), #pair-tooltip.is-pinned:not([hidden])";

function shortcutsAllowed(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return false;
  if (isTypingTarget(event.target) || elements.shortcuts.open) return false;
  return document.querySelector(BLOCKING_PANEL_SELECTOR) === null;
}

function visibleSymbols() {
  // DOM order follows the active sort; hidden rows are filtered out.
  return [...elements.marketsBody.querySelectorAll("tr[data-symbol]")]
    .filter((row) => !row.hidden)
    .map((row) => row.dataset.symbol);
}

function selectAdjacentPair(step) {
  const symbols = visibleSymbols();
  if (symbols.length === 0) return;
  const index = symbols.indexOf(state.selectedSymbol);
  // A selected pair hidden by the filters starts the walk from the matching end.
  const next = index === -1 ? (step > 0 ? 0 : symbols.length - 1) : index + step;
  if (next < 0 || next >= symbols.length) return;
  selectPair(symbols[next]);
}

function handleShortcut(event) {
  if (!shortcutsAllowed(event)) return;
  if (event.key === "/") {
    // Without preventDefault the "/" would be typed into the field it just focused.
    event.preventDefault();
    elements.search.focus();
  } else if (event.key === "?") {
    event.preventDefault();
    elements.shortcuts.showModal();
  } else if (Object.hasOwn(INTERVAL_KEYS, event.key)) {
    chartPanel.selectInterval(INTERVAL_KEYS[event.key]);
  } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    // The page would otherwise scroll sideways as well.
    event.preventDefault();
    selectAdjacentPair(event.key === "ArrowRight" ? 1 : -1);
  }
}

function closeShortcutsOnBackdrop(event) {
  // The dialog's content sits in an inner element, so only the backdrop targets the dialog.
  if (event.target === elements.shortcuts) elements.shortcuts.close();
}

function handleSearchEscape(event) {
  if (event.key !== "Escape") return;
  event.preventDefault();
  elements.search.value = "";
  applyFilter();
  elements.search.blur();
}

elements.search.addEventListener("input", applyFilter);
elements.search.addEventListener("keydown", handleSearchEscape);
document.addEventListener("keydown", handleShortcut);
elements.shortcuts.addEventListener("click", closeShortcutsOnBackdrop);
for (const button of elements.sortButtons) {
  button.addEventListener("click", () => changeSort(button.dataset.sort));
}
elements.tableWrap.addEventListener("pointerenter", () => {
  state.pointerOverTable = true;
});
elements.tableWrap.addEventListener("pointerleave", () => {
  state.pointerOverTable = false;
});
elements.onlyWatchlist.addEventListener("change", applyFilter);
elements.marketsEmptyClear.addEventListener("click", clearFilters);
for (const [symbol, row] of rows) {
  row.addEventListener("click", () => selectPair(symbol));
  row.querySelector(".star").addEventListener("click", (event) => {
    // The star must not also open the chart for this row.
    event.stopPropagation();
    toggleWatch(symbol);
  });
  const priceCell = row.querySelector('[data-field="last_price"]');
  priceCell.addEventListener("click", (event) => {
    // Copying must not also open the chart for this row.
    event.stopPropagation();
    copyPrice(symbol, priceCell);
  });
}
setInterval(() => {
  for (const symbol of rows.keys()) {
    renderStatus(symbol);
    renderPriceAge(symbol);
  }
  renderChartStale();
  maybeResort();
}, 1000);
chartPanel.onIntervalChange(writeChartUrl);
applyChartUrl();
loadSort();
renderSortHeaders();
pairMetaPanel.init({ rows, displaySymbol, onFilterChange: applyFilter });
loadWatchlist();
loadMarketMeta();
marketSocket.start({
  onMessage: handleMessage,
  onOpen(send) {
    orderBookPanel.connected(send);
    pairMetaPanel.connected();
  },
  onClose: () => orderBookPanel.disconnected(),
  onStatusChange() {
    renderAllStatuses();
    renderChartStale();
  },
});
