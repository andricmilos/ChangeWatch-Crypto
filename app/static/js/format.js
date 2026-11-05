"use strict";

// Formatting and small rendering helpers shared by the dashboard and the pair page.
// Prices arrive as strings ("72416.85"). Number() is used only to format them for display,
// never to compare them: equality checks and signs are decided on the strings themselves.

const LOCALE = "sr-Latn-RS";
const MISSING = "—";
const HIGH_SPREAD_TOOLTIP =
  "Velika razlika između kupovne i prodajne cene — tržište je manje likvidno.";

// Labels of marketSocket.freshness() values.
const STATUS_LABELS = { live: "Uživo", stale: "Zastarelo", "no-data": "Nema podataka" };

// symbol -> { price_decimals, quantity_decimals } from /api/markets/meta; empty until loaded.
const precision = new Map();

// ---------- Formatting ----------

const numberFormats = new Map();

function numberFormat(fractionDigits, signed, minimumDigits) {
  const key = `${fractionDigits}:${signed}:${minimumDigits}`;
  if (!numberFormats.has(key)) {
    numberFormats.set(
      key,
      new Intl.NumberFormat(LOCALE, {
        minimumFractionDigits: minimumDigits,
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

function formatNumber(
  value,
  fractionDigits,
  signed = false,
  minimumDigits = Math.min(2, fractionDigits),
) {
  if (isMissing(value)) return MISSING;
  const number = Number(value);
  if (!Number.isFinite(number)) return MISSING;
  return numberFormat(fractionDigits, signed, minimumDigits).format(number);
}

// Decimals of the pair's Binance tick size (prices) or step size (quantities); null while
// /api/markets/meta has not loaded or Binance did not say, so callers keep their own rule.
function priceDecimals(symbol) {
  const decimals = precision.get(symbol)?.price_decimals;
  return Number.isInteger(decimals) ? decimals : null;
}

function quantityDecimals(symbol) {
  const decimals = precision.get(symbol)?.quantity_decimals;
  return Number.isInteger(decimals) ? decimals : null;
}

function formatPrice(value, signed = false, symbol = null) {
  if (isMissing(value)) return MISSING;
  const decimals = priceDecimals(symbol);
  if (decimals === null) return formatNumber(value, priceDigits(value), signed);
  return formatNumber(value, decimals, signed, decimals);
}

function formatQuantity(value, symbol) {
  const decimals = quantityDecimals(symbol);
  if (decimals === null) return formatNumber(value, 2);
  return formatNumber(value, decimals, false, decimals);
}

function formatPercent(value) {
  const formatted = formatNumber(value, 2, true);
  return formatted === MISSING ? MISSING : `${formatted}%`;
}

function formatSpreadPercent(value) {
  return isMissing(value) ? MISSING : `${formatNumber(value, 3)}%`;
}

function direction(value) {
  if (isMissing(value) || /^-?0*\.?0*$/.test(value)) return null;
  return value.startsWith("-") ? "negative" : "positive";
}

function formatTime(ms) {
  return new Date(ms).toLocaleTimeString(LOCALE, { hour12: false });
}

const relativeTimeFormat = createRelativeTimeFormat();

function createRelativeTimeFormat() {
  try {
    return new Intl.RelativeTimeFormat("sr-Latn", { style: "narrow", numeric: "always" });
  } catch {
    return null;
  }
}

const RELATIVE_FALLBACK_UNITS = { second: "s", minute: "min", hour: "h", day: "d" };

function formatRelative(elapsedMs) {
  // A browser clock slightly behind the server would otherwise show "in 2 s".
  const seconds = Math.max(0, Math.floor(elapsedMs / 1000));
  let value = seconds;
  let unit = "second";
  if (seconds >= 86400) {
    value = Math.floor(seconds / 86400);
    unit = "day";
  } else if (seconds >= 3600) {
    value = Math.floor(seconds / 3600);
    unit = "hour";
  } else if (seconds >= 60) {
    value = Math.floor(seconds / 60);
    unit = "minute";
  }
  // -value keeps 0 in the past ("pre 0 s"), not the future ("za 0 s").
  if (relativeTimeFormat) return relativeTimeFormat.format(-value, unit);
  return `pre ${value} ${RELATIVE_FALLBACK_UNITS[unit]}`;
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

// ---------- Precision ----------

// Loads every pair's decimals; true when they changed, so the caller can redraw.
async function loadPrecision() {
  let metas;
  try {
    const response = await fetch("/api/markets/meta");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    metas = await response.json();
  } catch (error) {
    // Display only: the formatting guessed from the values keeps working without it.
    console.warn("Could not load pair precision; using the default formatting", error);
    return false;
  }
  for (const meta of metas) precision.set(meta.symbol, meta);
  return true;
}
