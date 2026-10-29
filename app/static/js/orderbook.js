"use strict";

// Live order book of the selected pair, pushed over /ws/market after subscribe_orderbook.
// - app.js owns the socket: it passes a send function on every (re)connect, so the
//   subscription is sent again after a reconnect, and tells the panel when the socket closes;
// - books of any other pair than the selected one are dropped (a switch can race a message);
// - prices and quantities arrive as strings; Number() is used only to format them and to size
//   the depth bars;
// - decimals come from the data: the most significant decimals seen for the pair so far, so a
//   book whose prices happen to end in 0 does not make the columns jump;
// - only cells whose text changed are written, and only those get the short highlight.

const orderBookPanel = (() => {
  const elements = {
    symbol: document.getElementById("orderbook-symbol"),
    toggle: document.getElementById("orderbook-toggle"),
    message: document.getElementById("orderbook-message"),
    stale: document.getElementById("orderbook-stale"),
    table: document.getElementById("orderbook-table"),
    askRows: [...document.querySelectorAll("#orderbook-asks tr")],
    bidRows: [...document.querySelectorAll("#orderbook-bids tr")],
    midPrice: document.getElementById("orderbook-mid-price"),
    spread: document.getElementById("orderbook-spread"),
    imbalance: document.getElementById("orderbook-imbalance"),
    imbalanceBuyers: document.getElementById("imbalance-buyers"),
    imbalanceSellers: document.getElementById("imbalance-sellers"),
    imbalanceLabel: document.getElementById("imbalance-label"),
    alertForm: document.querySelector(".alert-form"),
  };

  const STALE_AFTER_MS = 5000;
  const MAX_DIGITS = 8;
  const STORAGE_KEY = "changewatch.orderbook";
  const HIGHLIGHT_MS = 500;
  const HIGHLIGHT_COLOR = "rgba(210, 153, 34, 0.14)";
  const MESSAGES = {
    loading: "Učitavanje…",
    "no-data": "Nema podataka",
    off: "Knjiga naloga je isključena.",
  };

  const state = {
    enabled: loadEnabled(),
    symbol: null,
    // Sends a message over the open socket; null while it is closed.
    send: null,
    // "loading" | "no-data" | "ready" | "off"
    status: "loading",
    // Date.now() when the shown book arrived; the browser's own clock, so clock skew between
    // server and browser cannot make a book look fresh or stale.
    receivedAtMs: null,
    priceDigits: 0,
    quantityDigits: 0,
  };

  const formats = new Map();

  function fixedFormat(digits) {
    if (!formats.has(digits)) {
      // LOCALE comes from app.js, which loads after this file; it only runs once data arrives.
      formats.set(
        digits,
        new Intl.NumberFormat(LOCALE, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        }),
      );
    }
    return formats.get(digits);
  }

  function formatFixed(value, digits) {
    const number = Number(value);
    return Number.isFinite(number) ? fixedFormat(digits).format(number) : "—";
  }

  function significantDecimals(value) {
    // Binance pads to 8 decimals ("72309.75000000"), so trailing zeros do not count.
    const fraction = value.split(".")[1] ?? "";
    return Math.min(fraction.replace(/0+$/, "").length, MAX_DIGITS);
  }

  function trimZeros(value) {
    return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  }

  // ---------- Rendering ----------

  function highlight(element) {
    // The Web Animations API restarts cleanly every second without forcing a reflow per cell.
    if (typeof element.animate !== "function") return;
    element.animate([{ backgroundColor: HIGHLIGHT_COLOR }, { backgroundColor: "transparent" }], {
      duration: HIGHLIGHT_MS,
      easing: "ease-out",
    });
  }

  function setText(element, text, changed) {
    if (element.textContent === text) return;
    element.textContent = text;
    changed.push(element);
  }

  function renderLevel(row, level, maxCumulative, changed) {
    const [priceCell, quantityCell, cumulativeCell] = row.cells;
    const button = priceCell.querySelector("button");
    row.classList.toggle("is-empty", level === undefined);
    button.disabled = level === undefined;
    button.dataset.price = level?.price ?? "";
    if (level === undefined) {
      setText(button, "", changed);
      setText(quantityCell, "", changed);
      setText(cumulativeCell, "", changed);
    } else {
      setText(button, formatFixed(level.price, state.priceDigits), changed);
      setText(quantityCell, formatFixed(level.quantity, state.quantityDigits), changed);
      setText(cumulativeCell, formatFixed(level.cumulative_quantity, state.quantityDigits), changed);
    }
    const ratio = level === undefined || maxCumulative <= 0
      ? 0
      : Number(level.cumulative_quantity) / maxCumulative;
    const depth = `${Math.min(100, ratio * 100).toFixed(1)}%`;
    if (row.dataset.depth !== depth) {
      row.dataset.depth = depth;
      row.style.setProperty("--depth", depth);
    }
  }

  function lastCumulative(levels) {
    return levels.length === 0 ? 0 : Number(levels[levels.length - 1].cumulative_quantity);
  }

  function updateDigits(book) {
    for (const level of [...book.bids, ...book.asks]) {
      state.priceDigits = Math.max(state.priceDigits, significantDecimals(level.price));
      state.quantityDigits = Math.max(state.quantityDigits, significantDecimals(level.quantity));
    }
  }

  function renderBook(book) {
    const firstRender = state.status !== "ready";
    updateDigits(book);
    // The largest total on either side is 100%, so both sides share one scale.
    const maxCumulative = Math.max(lastCumulative(book.bids), lastCumulative(book.asks));
    const changed = [];
    const rowCount = elements.askRows.length;
    // Asks are drawn upside down, so the best ask sits right above the mid row.
    elements.askRows.forEach((row, index) => {
      renderLevel(row, book.asks[rowCount - 1 - index], maxCumulative, changed);
    });
    elements.bidRows.forEach((row, index) => {
      renderLevel(row, book.bids[index], maxCumulative, changed);
    });

    const midDigits = Math.max(state.priceDigits, significantDecimals(book.mid_price));
    setText(elements.midPrice, formatFixed(book.mid_price, midDigits), changed);
    setText(elements.spread, `Spread ${formatFixed(book.spread_percent, 3)}%`, changed);
    renderImbalance(book);

    // A new pair or the first book after loading is not a change worth pointing at.
    if (!firstRender) changed.forEach(highlight);
  }

  function renderImbalance(book) {
    const bids = Number(book.bid_volume);
    const asks = Number(book.ask_volume);
    const total = bids + asks;
    if (!Number.isFinite(total) || total <= 0) {
      elements.imbalance.hidden = true;
      return;
    }
    const buyers = Math.round((bids / total) * 100);
    const sellers = 100 - buyers;
    elements.imbalance.hidden = false;
    elements.imbalanceBuyers.style.width = `${buyers}%`;
    elements.imbalanceSellers.style.width = `${sellers}%`;
    elements.imbalanceLabel.textContent = `Kupci ${buyers}% · Prodavci ${sellers}%`;
  }

  function setStatus(status) {
    state.status = status;
    const ready = status === "ready";
    elements.table.hidden = !ready;
    if (!ready) elements.imbalance.hidden = true;
    elements.message.hidden = ready;
    if (!ready) elements.message.textContent = MESSAGES[status];
    renderStale();
  }

  function isStale() {
    if (state.status !== "ready") return false;
    return state.send === null || Date.now() - state.receivedAtMs > STALE_AFTER_MS;
  }

  function renderStale() {
    const stale = isStale();
    elements.stale.hidden = !stale;
    elements.table.classList.toggle("is-stale", stale);
  }

  function resetBook() {
    state.receivedAtMs = null;
    state.priceDigits = 0;
    state.quantityDigits = 0;
  }

  // ---------- Subscription ----------

  function subscribe() {
    if (!state.enabled || state.symbol === null || state.send === null) return;
    state.send({ action: "subscribe_orderbook", symbol: state.symbol });
  }

  async function checkForData(symbol) {
    // The socket says nothing while the server has no book for the pair, so the REST view
    // tells "still loading" apart from "nothing to show".
    try {
      const response = await fetch(`/api/markets/${encodeURIComponent(symbol)}/orderbook`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const view = await response.json();
      if (view.freshness === "no_data" && symbol === state.symbol && state.status === "loading") {
        setStatus("no-data");
      }
    } catch (error) {
      console.warn("Could not check the order book", error);
    }
  }

  function start() {
    resetBook();
    setStatus("loading");
    subscribe();
    checkForData(state.symbol);
  }

  // ---------- Toggle ----------

  function loadEnabled() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY)) !== false;
    } catch (error) {
      console.warn("Could not read the saved order book setting", error);
      return true;
    }
  }

  function saveEnabled() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state.enabled));
    } catch (error) {
      console.warn("Could not save the order book setting", error);
    }
  }

  function setEnabled(enabled) {
    state.enabled = enabled;
    elements.toggle.setAttribute("aria-pressed", String(enabled));
    saveEnabled();
    if (!enabled) {
      state.send?.({ action: "unsubscribe_orderbook" });
      resetBook();
      setStatus("off");
    } else if (state.symbol !== null) {
      start();
    }
  }

  // ---------- Alert form ----------

  function useAsAlertTarget(price) {
    const form = elements.alertForm;
    if (!form || price === "" || state.symbol === null) return;
    // Only fills the form: nothing is saved until the user submits it.
    form.elements.symbol.value = state.symbol;
    const input = form.elements.target_price;
    input.value = trimZeros(price);
    input.focus();
    highlight(input);
  }

  // ---------- Public API ----------

  function select(symbol, displaySymbol) {
    if (symbol === state.symbol) return;
    state.symbol = symbol;
    elements.symbol.textContent = displaySymbol;
    if (state.enabled) start();
  }

  function connected(send) {
    state.send = send;
    // The server forgets subscriptions with the connection, so every new socket needs one.
    // A book already on screen stays (marked stale) until the fresh one arrives.
    subscribe();
    if (state.enabled && state.symbol !== null && state.status !== "ready") {
      checkForData(state.symbol);
    }
    renderStale();
  }

  function disconnected() {
    state.send = null;
    renderStale();
  }

  function handleOrderBook(book) {
    if (!state.enabled || book.symbol !== state.symbol) return;
    if (book.bids.length === 0 && book.asks.length === 0) {
      setStatus("no-data");
      return;
    }
    state.receivedAtMs = Date.now();
    renderBook(book);
    setStatus("ready");
  }

  function handleError(detail) {
    console.warn("Order book subscription refused:", detail);
  }

  for (const row of [...elements.askRows, ...elements.bidRows]) {
    const button = row.querySelector("button");
    button.addEventListener("click", () => useAsAlertTarget(button.dataset.price));
  }
  elements.toggle.addEventListener("click", () => setEnabled(!state.enabled));
  elements.toggle.setAttribute("aria-pressed", String(state.enabled));
  if (!state.enabled) setStatus("off");
  setInterval(renderStale, 1000);

  return { select, connected, disconnected, handleOrderBook, handleError };
})();
