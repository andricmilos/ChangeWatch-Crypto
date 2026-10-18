"use strict";

// EUR <-> crypto calculator. All arithmetic happens on the server in Decimal; amounts come
// back as strings and are formatted here without going through a float.
// Rules:
// - typing waits for a 300 ms pause before calling /api/convert;
// - price updates ("market:prices" events from app.js) refresh at most once per 2 s;
// - every request gets a requestId; responses of superseded requests are dropped.

const converterPanel = (() => {
  const DEBOUNCE_MS = 300;
  const REFRESH_MIN_INTERVAL_MS = 2000;
  const EUR = "EUR";
  const MAX_DECIMALS = 8;
  const MAX_AMOUNT = "1000000000";
  const AMOUNT_PATTERN = /^\d+(?:[.,]\d+)?$/;
  const PRICE_LABELS = { ask: "ask", bid: "bid", last: "poslednja cena" };

  const elements = {
    amount: document.getElementById("converter-amount"),
    currency: document.getElementById("converter-currency"),
    swap: document.getElementById("converter-swap"),
    error: document.getElementById("converter-error"),
    results: document.getElementById("converter-results"),
  };

  const state = {
    requestId: 0,
    debounceTimer: null,
    refreshTimer: null,
    lastFetchMs: 0,
    // Results of the last successful request, used by the swap button.
    response: null,
    // Where the swap button goes from EUR; follows the last crypto the user picked.
    lastCrypto: [...elements.currency.options].find((option) => option.value !== EUR)?.value,
  };

  // Returns { value } with a dot-separated decimal string, { error }, or { value: null } if empty.
  function parseAmount(raw) {
    const text = raw.trim().replace(/\s/g, "");
    if (text === "") return { value: null };
    if (!AMOUNT_PATTERN.test(text)) return { error: "Unesi broj, npr. 100 ili 0,5." };
    const value = text.replace(",", ".");
    const [integer, fraction = ""] = value.split(".");
    if (fraction.length > MAX_DECIMALS) return { error: "Najviše 8 decimala." };
    if (/^0*$/.test(integer) && /^0*$/.test(fraction)) {
      return { error: "Iznos mora biti veći od 0." };
    }
    if (compareDecimalStrings(value, MAX_AMOUNT) > 0) {
      return { error: "Iznos može biti najviše 1.000.000.000." };
    }
    return { value };
  }

  // "36208.43" -> "36.208,43", "200.00000000" -> "200,00"; exact, unlike Number().
  function formatAmount(value) {
    if (isMissing(value)) return MISSING;
    const [integer, fraction = ""] = value.split(".");
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return `${grouped},${fraction.replace(/0+$/, "").padEnd(2, "0")}`;
  }

  function showError(message) {
    elements.error.textContent = message ?? "";
    elements.error.hidden = message === null;
  }

  function createResult(result) {
    const item = document.createElement("li");
    item.className = "converter-result";
    const stale = result.freshness === "stale";
    item.classList.toggle("is-stale", stale);

    const currency = document.createElement("span");
    currency.className = "converter-currency";
    currency.textContent = result.currency;

    const amount = document.createElement("span");
    amount.className = "num converter-amount";
    amount.textContent = formatAmount(result.amount);

    const meta = document.createElement("span");
    meta.className = "converter-meta";
    if (isMissing(result.price)) {
      meta.textContent = "Nema podataka";
    } else {
      const base = result.symbol.slice(0, -EUR.length);
      meta.textContent =
        `1 ${base} = ${formatPrice(result.price)} EUR · ${PRICE_LABELS[result.price_type]}`;
    }

    item.append(currency, amount, meta);
    if (stale) {
      const badge = document.createElement("span");
      badge.className = "status status-stale";
      badge.textContent = "Zastarelo";
      item.append(badge);
    }
    return item;
  }

  function render(response) {
    state.response = response;
    elements.results.replaceChildren(...(response?.results ?? []).map(createResult));
  }

  function markResultsStale() {
    for (const item of elements.results.children) item.classList.add("is-stale");
  }

  async function load() {
    clearTimeout(state.debounceTimer);
    state.debounceTimer = null;

    const parsed = parseAmount(elements.amount.value);
    if (parsed.error !== undefined || parsed.value === null) {
      // A new request id also drops any response still on its way for the old input.
      state.requestId += 1;
      showError(parsed.error ?? null);
      render(null);
      return;
    }

    const requestId = ++state.requestId;
    state.lastFetchMs = Date.now();
    const params = new URLSearchParams({ amount: parsed.value, currency: elements.currency.value });
    try {
      const response = await fetch(`/api/convert?${params}`);
      if (requestId !== state.requestId) return;
      if (response.status === 422) {
        showError("Neispravan iznos ili valuta.");
        render(null);
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = await response.json();
      if (requestId !== state.requestId) return;
      showError(null);
      render(body);
    } catch (error) {
      if (requestId !== state.requestId) return;
      console.error("Could not convert the amount", error);
      showError("Kalkulator trenutno nije dostupan.");
      // Old numbers stay visible, but no longer look current.
      markResultsStale();
    }
  }

  function scheduleLoad() {
    clearTimeout(state.debounceTimer);
    state.debounceTimer = setTimeout(load, DEBOUNCE_MS);
  }

  function pricesChanged() {
    if (state.refreshTimer !== null) return;
    const wait = Math.max(0, state.lastFetchMs + REFRESH_MIN_INTERVAL_MS - Date.now());
    state.refreshTimer = setTimeout(() => {
      state.refreshTimer = null;
      // The user is still typing; the debounced call will fetch fresh prices anyway.
      if (state.debounceTimer !== null) return;
      load();
    }, wait);
  }

  function swap() {
    const from = elements.currency.value;
    if (from !== EUR) state.lastCrypto = from;
    const to = from === EUR ? state.lastCrypto : EUR;
    if (to === undefined) return;
    // The amount carries over: 100 EUR -> 0.00138 BTC becomes 0.00138 BTC -> EUR.
    const result = state.response?.results.find((item) => item.currency === to);
    if (state.response?.currency === from && !isMissing(result?.amount)) {
      elements.amount.value = formatAmount(result.amount).replaceAll(".", "");
    }
    elements.currency.value = to;
    load();
  }

  elements.amount.addEventListener("input", scheduleLoad);
  elements.currency.addEventListener("change", () => {
    if (elements.currency.value !== EUR) state.lastCrypto = elements.currency.value;
    load();
  });
  elements.swap.addEventListener("click", swap);
  document.addEventListener("market:prices", pricesChanged);

  load();

  return { pricesChanged };
})();
