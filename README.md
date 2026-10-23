# ChangeWatch Crypto

Real-time cryptocurrency market monitor built with FastAPI, using public Binance market data.

## Price alerts

An alert ("BTC/EUR above 100,000") is checked on every fresh ticker price and fires exactly
once: it is then switched off and stays off, also after a restart. `PATCH /api/alerts/{id}`
with `{"is_active": true}` re-enables it.

An alert created while its condition is already met fires on the next ticker. This is
expected: alerts are checked when new market data arrives, not when they are created.
Stale or missing prices never fire an alert.

## Chart notes and fired alerts

![BTC/EUR chart with note and alert markers](docs/images/chart-markers.png)

The chart shows two kinds of markers on the candle they belong to:

- **Notes** (yellow dot): short comments pinned to a moment, added with "Dodaj belešku".
  Several notes in one candle show their count.
- **Fired alerts** (purple arrow): ▲ for "above", ▼ for "below", labelled with the price
  that fired the alert, e.g. "▲ 72.818,76 €". An alert that fires while the chart is open
  appears at once, without a reload.

Hovering a candle lists its notes and alerts below the chart. The "Beleške" and "Alerti"
toggles above the chart hide or show each kind; the choice is remembered in the browser.
Fired alerts of a pair in a period come from
`GET /api/alerts?symbol=BTCEUR&triggered_from_ms=...&triggered_to_ms=...` (UTC milliseconds,
both inclusive and optional, oldest first).

## Limitations

- Chart notes (`/api/notes`) are shared by every visitor: there are no accounts, so anyone
  who can open the app can add, edit or delete any note. At most 500 notes are stored.
- For a public demo, set `NOTES_WRITE_ENABLED=false`: notes stay visible, but creating,
  editing and deleting them returns 403. `GET /api/notes/config` tells the UI which mode
  the server is in.
- Notes are not removed by the candle cleanup (`CANDLE_RETENTION_DAYS`).

## Running locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Alternatively, `python -m app` starts the same server with `HOST` and `PORT` from `.env`
(default `127.0.0.1:8000`); auto-reload is on only when `APP_ENV=development`.
