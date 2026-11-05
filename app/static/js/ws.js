"use strict";

// The /ws/market connection shared by the dashboard and the pair page.
// - one socket per page, reconnected with exponential backoff after every close;
// - owns the connection state and the parts of base.html that show it: the indicator, the
//   favicon, the two banners and "Poslednje ažuriranje";
// - every message goes to the page's onMessage after the connection state has taken it into
//   account, so the page sees a status change before the data that follows it;
// - the page decides what is fresh per market with freshness(market).

const marketSocket = (() => {
  const STALE_AFTER_MS = 15000;
  const RECONNECT_INITIAL_DELAY_MS = 1000;
  const RECONNECT_MAX_DELAY_MS = 30000;

  const elements = {
    indicator: document.getElementById("connection-indicator"),
    indicatorLabel: document.getElementById("connection-label"),
    lastUpdate: document.getElementById("last-update"),
    serverBanner: document.getElementById("banner-server"),
    exchangeBanner: document.getElementById("banner-exchange"),
    favicon: document.getElementById("favicon"),
  };

  const state = {
    // When the last message proving live exchange data arrived (any pair's ticker or kline);
    // null when the data on screen is not current. Shared by all pairs: quiet pairs such as
    // ADA/EUR can go minutes without a ticker while their price is still current.
    liveDataAtMs: null,
    socketState: "connecting", // "connecting" | "open" | "closed"
    exchangeStatus: null,
    // "rest" while the server polls Binance REST because its stream connection is down.
    priceSource: "ws",
    reconnectDelayMs: RECONNECT_INITIAL_DELAY_MS,
    lastUpdateMs: null,
    handlers: null,
  };

  function inRestFallback() {
    return state.exchangeStatus !== "connected" && state.priceSource === "rest";
  }

  function exchangeDataFlowing() {
    return state.exchangeStatus === "connected" || inRestFallback();
  }

  function markLiveData() {
    state.liveDataAtMs = Date.now();
  }

  // "live" | "stale" | "no-data" for one market as the page last received it.
  function freshness(market) {
    if (!market || isMissing(market.last_price)) return "no-data";
    if (
      state.socketState !== "open" ||
      !exchangeDataFlowing() ||
      state.liveDataAtMs === null ||
      Date.now() - state.liveDataAtMs > STALE_AFTER_MS
    ) {
      return "stale";
    }
    return "live";
  }

  // True while nothing on screen can be current: no socket, or the exchange is not sending.
  function connectionDown() {
    const exchangeDown = state.exchangeStatus !== null && !exchangeDataFlowing();
    return state.socketState === "closed" || exchangeDown;
  }

  function updateLastUpdate(updatedAt) {
    if (isMissing(updatedAt)) return;
    const ms = Date.parse(updatedAt);
    if (Number.isNaN(ms) || (state.lastUpdateMs !== null && ms <= state.lastUpdateMs)) return;
    state.lastUpdateMs = ms;
    renderLastUpdate();
  }

  function renderLastUpdate() {
    const ms = state.lastUpdateMs;
    const old = ms !== null && Date.now() - ms > STALE_AFTER_MS;
    elements.lastUpdate.textContent =
      ms === null ? MISSING : `${formatTime(ms)} (${formatRelative(Date.now() - ms)})`;
    elements.lastUpdate.classList.toggle("is-old", old);
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
    } else if (state.socketState === "open" && inRestFallback()) {
      kind = "fallback";
      label = "Rezervni režim – cene na 10 s";
    }
    elements.indicator.className = `indicator indicator-${kind}`;
    elements.indicatorLabel.textContent = label;
    renderFavicon(kind);
    state.handlers?.onStatusChange?.();
  }

  // Same colors as the indicator dot, read from the stylesheet so they stay in one place.
  const FAVICON_COLOR_VARS = {
    live: "--green",
    connecting: "--yellow",
    fallback: "--yellow",
    disconnected: "--red",
  };

  function renderFavicon(kind) {
    const link = elements.favicon;
    // renderIndicator runs on every status heartbeat; only touch the <link> on a real change.
    if (!link || link.dataset.kind === kind) return;
    link.dataset.kind = kind;
    const color = getComputedStyle(document.documentElement)
      .getPropertyValue(FAVICON_COLOR_VARS[kind])
      .trim();
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
      `<circle cx="16" cy="16" r="15" fill="${color}"/>` +
      '<text x="16" y="22" font-family="Arial,sans-serif" font-size="18" font-weight="700" ' +
      'fill="#0d1117" text-anchor="middle">C</text></svg>';
    link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  function setExchangeStatus(status, priceSource = "ws") {
    state.exchangeStatus = status;
    state.priceSource = priceSource;
    elements.exchangeBanner.hidden = status !== "reconnecting";
    if (inRestFallback()) {
      // The server sends this status after every successful REST round, as a heartbeat.
      markLiveData();
    } else if (status !== "connected") {
      // Without a live exchange connection no price on screen is current.
      state.liveDataAtMs = null;
    }
    renderIndicator();
  }

  function trackMessage(message) {
    switch (message.type) {
      case "snapshot": {
        const { markets } = message.data;
        setExchangeStatus(message.data.connection_status, message.data.price_source);
        // The server's freshness follows its exchange connection, so one live market means
        // all are.
        if (markets.some((market) => market.freshness === "live")) markLiveData();
        for (const market of markets) updateLastUpdate(market.updated_at);
        break;
      }
      case "ticker":
        markLiveData();
        updateLastUpdate(message.data.updated_at);
        break;
      case "status":
        setExchangeStatus(message.data.connection_status, message.data.price_source);
        break;
      case "kline":
        markLiveData();
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
      state.handlers.onOpen?.((payload) => {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
      });
    });

    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        console.warn("Ignoring malformed message from /ws/market", error);
        return;
      }
      trackMessage(message);
      state.handlers.onMessage(message);
    });

    socket.addEventListener("close", () => {
      state.socketState = "closed";
      elements.serverBanner.hidden = false;
      state.liveDataAtMs = null;
      renderIndicator();
      state.handlers.onClose?.();
      setTimeout(connect, state.reconnectDelayMs);
      state.reconnectDelayMs = Math.min(state.reconnectDelayMs * 2, RECONNECT_MAX_DELAY_MS);
    });
  }

  // handlers: { onMessage(message), onOpen(send), onClose(), onStatusChange() }; only
  // onMessage is required. Staleness also changes with time alone, so pages re-check
  // freshness() on their own timer as well.
  function start(handlers) {
    state.handlers = handlers;
    renderIndicator();
    setInterval(renderLastUpdate, 1000);
    connect();
  }

  return { start, freshness, connectionDown };
})();
