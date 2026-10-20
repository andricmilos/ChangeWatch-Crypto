"use strict";

// "What if" calculator: what an EUR amount bought at a past moment is worth today, per pair.
// All arithmetic happens on the server in Decimal; values arrive as strings.
// Rules:
// - typing waits for a 300 ms pause before calling /api/history/value;
// - "Drugi datum" takes local time from datetime-local and sends it as UTC;
// - every request gets a requestId; responses of superseded requests are dropped.

const historyPanel = (() => {
  const DEBOUNCE_MS = 300;
  const CUSTOM = "custom";
  const PRICE_LABELS = { bid: "bid", last: "poslednja cena" };
  const ERROR_MESSAGES = {
    429: "Previše zahteva, pokušaj ponovo za minut.",
    503: "Berza trenutno nije dostupna, pokušaj ponovo kasnije.",
  };
  const localTime = new Intl.DateTimeFormat(LOCALE, { dateStyle: "medium", timeStyle: "short" });
  const utcTime = new Intl.DateTimeFormat(LOCALE, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  });

  const elements = {
    amount: document.getElementById("history-amount"),
    periods: [...document.querySelectorAll("[data-period]")],
    at: document.getElementById("history-at"),
    error: document.getElementById("history-error"),
    loading: document.getElementById("history-loading"),
    body: document.getElementById("history-body"),
    candle: document.getElementById("history-candle"),
  };

  const state = {
    requestId: 0,
    debounceTimer: null,
    period: elements.periods.find((button) => button.getAttribute("aria-pressed") === "true")
      .dataset.period,
  };

  // datetime-local wants "YYYY-MM-DDTHH:MM" in local time.
  function toLocalInputValue(date) {
    const pad = (number) => String(number).padStart(2, "0");
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
      `T${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
  }

  function updateDateLimits() {
    const now = new Date();
    const yearAgo = new Date(now);
    yearAgo.setFullYear(now.getFullYear() - 1);
    // One minute of slack, so the edge of the range is not rejected by the time it is sent.
    yearAgo.setMinutes(yearAgo.getMinutes() + 1);
    elements.at.min = toLocalInputValue(yearAgo);
    elements.at.max = toLocalInputValue(now);
    if (elements.at.value === "") {
      const weekAgo = new Date(now);
      weekAgo.setDate(now.getDate() - 7);
      elements.at.value = toLocalInputValue(weekAgo);
    }
  }

  function showError(message) {
    elements.error.textContent = message ?? "";
    elements.error.hidden = message === null;
  }

  function cell(text, className = "num") {
    const td = document.createElement("td");
    td.className = className;
    td.textContent = text;
    return td;
  }

  function formatEur(value, signed = false) {
    const sign = signed && direction(value) === "positive" ? "+" : "";
    return `${sign}${converterPanel.formatAmount(value)} €`;
  }

  function createRow(item) {
    const row = document.createElement("tr");
    row.append(cell(item.display_symbol, "pair"));
    const result = item.result;
    if (result === null) {
      const missing = cell(`${MISSING} `, "reason-cell");
      missing.colSpan = 5;
      const reason = document.createElement("span");
      reason.className = "reason";
      reason.textContent = `(${item.reason})`;
      missing.append(reason);
      row.append(missing);
      return row;
    }

    const now = cell(`${formatPrice(result.current_price)} `);
    const priceType = document.createElement("span");
    priceType.className = "price-type";
    priceType.textContent = PRICE_LABELS[result.current_price_type];
    now.append(priceType);
    row.classList.toggle("is-stale", result.freshness === "stale");

    const profitClass = direction(result.profit_eur);
    row.append(
      cell(formatPrice(result.historical_price)),
      now,
      cell(formatEur(result.value_eur)),
      cell(formatEur(result.profit_eur, true), `num ${profitClass ?? ""}`),
      cell(formatPercent(result.profit_percent), `num ${profitClass ?? ""}`),
    );
    return row;
  }

  // Best result first; pairs without a result go last, in server order.
  function compareItems(a, b) {
    if (a.result === null || b.result === null) {
      return (a.result === null) - (b.result === null);
    }
    return compareDecimalStrings(b.result.profit_percent, a.result.profit_percent);
  }

  function render(body) {
    if (body === null) {
      elements.body.replaceChildren();
      elements.candle.textContent = "";
      return;
    }
    const items = [...body.results].sort(compareItems);
    elements.body.replaceChildren(...items.map(createRow));
    const candleTime = new Date(body.candle_open_time);
    elements.candle.textContent =
      `Cena tada je cena otvaranja 1h sveće od ${localTime.format(candleTime)} ` +
      `po lokalnom vremenu (${utcTime.format(candleTime)} UTC).`;
  }

  function errorMessage(status, detail) {
    if (status === 422) return typeof detail === "string" ? detail : "Neispravan iznos ili datum.";
    return ERROR_MESSAGES[status] ?? "Kalkulator trenutno nije dostupan.";
  }

  function buildParams(amount) {
    const params = new URLSearchParams({ amount });
    if (state.period !== CUSTOM) {
      params.set("period", state.period);
      return params;
    }
    if (elements.at.value === "") return null;
    // new Date("YYYY-MM-DDTHH:MM") is local time; toISOString() turns it into UTC.
    params.set("at", new Date(elements.at.value).toISOString());
    return params;
  }

  async function load() {
    clearTimeout(state.debounceTimer);
    state.debounceTimer = null;
    // A new request id also drops any response still on its way for the old input.
    const requestId = ++state.requestId;

    const parsed = converterPanel.parseAmount(elements.amount.value);
    const params = parsed.value ? buildParams(parsed.value) : null;
    if (params === null) {
      showError(parsed.error ?? (parsed.value ? "Izaberi datum." : null));
      elements.loading.hidden = true;
      render(null);
      return;
    }

    elements.loading.hidden = false;
    try {
      const response = await fetch(`/api/history/value?${params}`);
      const body = await response.json().catch(() => null);
      if (requestId !== state.requestId) return;
      elements.loading.hidden = true;
      if (!response.ok) {
        showError(errorMessage(response.status, body?.detail));
        render(null);
        return;
      }
      showError(null);
      render(body);
    } catch (error) {
      if (requestId !== state.requestId) return;
      console.error("Could not load the historical value", error);
      elements.loading.hidden = true;
      showError("Kalkulator trenutno nije dostupan.");
      render(null);
    }
  }

  function selectPeriod(period) {
    state.period = period;
    for (const button of elements.periods) {
      button.setAttribute("aria-pressed", String(button.dataset.period === period));
    }
    elements.at.hidden = period !== CUSTOM;
    if (period === CUSTOM) updateDateLimits();
    load();
  }

  elements.amount.addEventListener("input", () => {
    clearTimeout(state.debounceTimer);
    state.debounceTimer = setTimeout(load, DEBOUNCE_MS);
  });
  for (const button of elements.periods) {
    button.addEventListener("click", () => selectPeriod(button.dataset.period));
  }
  elements.at.addEventListener("change", load);

  load();
})();
