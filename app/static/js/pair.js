"use strict";

// The pair page (/pair/BTCEUR). Formatting comes from format.js, the connection from ws.js,
// and the chart and order book are the dashboard's panels showing this one pair.
// - GET /api/pairs/{symbol}/summary gives the 7-day statistics and the pair's decimals; the
//   market itself also arrives over /ws/market, so whichever copy is newer (updated_at) wins;
// - ?interval= picks the chart interval and follows every change, as on the dashboard;
// - prices arrive as strings; Number() is used only to format them for display.

(() => {
  const header = document.querySelector(".pair-header");
  const nameElement = document.getElementById("pair-name");
  const symbol = nameElement.dataset.symbol;
  const display = nameElement.textContent;
  // "BTC/EUR" -> volume in BTC.
  const baseAsset = display.split("/")[0];

  const elements = {
    status: document.getElementById("pair-status"),
    loading: document.getElementById("pair-loading"),
    price: header.querySelector('[data-field="last_price"]'),
    book: document.getElementById("pair-book"),
    stats: document.querySelector(".pair-stats"),
    stats7d: document.getElementById("stats-7d"),
    stats7dLoading: document.getElementById("stats-7d-loading"),
    stats7dUnavailable: document.getElementById("stats-7d-unavailable"),
    stats7dList: document.getElementById("stats-7d-list"),
  };

  let market = null;

  function formatVolume(value) {
    const formatted = formatQuantity(value, symbol);
    return formatted === MISSING ? MISSING : `${formatted} ${baseAsset}`;
  }

  // ---------- Market ----------

  function isOlder(next, current) {
    const nextMs = Date.parse(next.updated_at);
    const currentMs = Date.parse(current.updated_at);
    return !Number.isNaN(nextMs) && !Number.isNaN(currentMs) && nextMs < currentMs;
  }

  function applyMarket(next) {
    if (next.symbol !== symbol) return;
    // The summary can answer after a newer ticker has already arrived over the socket.
    if (market !== null && isOlder(next, market)) return;
    const priceChanged =
      market !== null && !isMissing(next.last_price) && market.last_price !== next.last_price;
    market = next;
    renderMarket(priceChanged);
  }

  function renderMarket(priceChanged = false) {
    if (market === null) return;
    elements.loading.hidden = true;
    const price = setCell(header, "last_price", formatPrice(market.last_price, false, symbol));
    setCell(
      header,
      "price_change",
      formatPrice(market.price_change, true, symbol),
      market.price_change,
    );
    setCell(
      header,
      "price_change_percent",
      formatPercent(market.price_change_percent),
      market.price_change_percent,
    );
    if (priceChanged && price) flash(price);

    const hasBook = !isMissing(market.bid_price) || !isMissing(market.ask_price);
    elements.book.hidden = !hasBook;
    setCell(elements.book, "bid_price", formatPrice(market.bid_price, false, symbol));
    setCell(elements.book, "ask_price", formatPrice(market.ask_price, false, symbol));
    const spread = elements.book.querySelector('[data-field="spread_percent"]');
    setCell(elements.book, "spread_percent", formatSpreadPercent(market.spread_percent));
    const highSpread = market.high_spread === true;
    spread.classList.toggle("spread-high", highSpread);
    if (highSpread) {
      spread.title = HIGH_SPREAD_TOOLTIP;
    } else {
      spread.removeAttribute("title");
    }

    setCell(elements.stats, "high_24h", formatPrice(market.high_24h, false, symbol));
    setCell(elements.stats, "low_24h", formatPrice(market.low_24h, false, symbol));
    setCell(elements.stats, "volume_24h", formatVolume(market.volume_24h));
    renderFreshness();
  }

  function renderFreshness() {
    const status = marketSocket.freshness(market);
    const ms = market === null || isMissing(market.updated_at) ? NaN : Date.parse(market.updated_at);
    elements.price.title = Number.isNaN(ms)
      ? `Ažurirano ${MISSING}`
      : `Ažurirano ${formatRelative(Date.now() - ms)} (${formatTime(ms)})`;
    header.classList.toggle("is-stale", status === "stale");
    chartPanel.setStale(marketSocket.connectionDown() || status === "stale");
    if (elements.status.dataset.status === status) return;
    elements.status.dataset.status = status;
    const badge = document.createElement("span");
    badge.className = `status status-${status}`;
    badge.textContent = STATUS_LABELS[status];
    elements.status.replaceChildren(badge);
  }

  // ---------- Summary ----------

  function render7d(stats) {
    elements.stats7dLoading.hidden = true;
    // Without the statistics there is nothing to show: no zeros, only the message.
    elements.stats7dUnavailable.hidden = stats !== null;
    elements.stats7dList.hidden = stats === null;
    if (stats === null) return;
    const list = elements.stats7dList;
    setCell(list, "high", formatPrice(stats.high, false, symbol));
    setCell(list, "low", formatPrice(stats.low, false, symbol));
    setCell(
      list,
      "price_change_percent",
      formatPercent(stats.price_change_percent),
      stats.price_change_percent,
    );
    setCell(list, "volume", formatVolume(stats.volume));
  }

  async function loadSummary() {
    let summary;
    try {
      const response = await fetch(`/api/pairs/${encodeURIComponent(symbol)}/summary`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      summary = await response.json();
    } catch (error) {
      console.warn(`Could not load the summary of ${symbol}`, error);
      render7d(null);
      return;
    }
    precision.set(symbol, summary.meta);
    render7d(summary.stats_7d);
    applyMarket(summary.market);
    // Redraws with the pair's decimals, even when the socket's copy was newer.
    renderMarket();
  }

  // ---------- Chart interval in the URL ----------

  // replaceState, as on the dashboard: changing the interval must not fill the history.
  function writeIntervalUrl() {
    const url = new URL(window.location.href);
    url.searchParams.set("interval", chartPanel.getInterval());
    history.replaceState(history.state, "", url);
  }

  // ---------- WebSocket ----------

  function handleMessage(message) {
    switch (message.type) {
      case "snapshot":
        for (const item of message.data.markets) applyMarket(item);
        break;
      case "ticker":
        applyMarket(message.data);
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
        chartPanel.handleAlertTriggered(message.data);
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

  const interval = chartPanel.intervalFromUrl(new URLSearchParams(window.location.search));
  // Before select, so the chart loads its history only once.
  if (interval !== null) chartPanel.selectInterval(interval);
  chartPanel.select(symbol, display);
  chartPanel.onIntervalChange(writeIntervalUrl);
  writeIntervalUrl();
  orderBookPanel.select(symbol, display);

  loadSummary();
  marketSocket.start({
    onMessage: handleMessage,
    onOpen: (send) => orderBookPanel.connected(send),
    onClose: () => orderBookPanel.disconnected(),
    onStatusChange: renderFreshness,
  });
  setInterval(renderFreshness, 1000);
})();
