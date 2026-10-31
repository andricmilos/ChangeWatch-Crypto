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

## Order book

![BTC/EUR order book panel](docs/images/orderbook.png)

Next to the chart (below it on phones), the "Knjiga naloga" panel shows the live order book of
the selected pair from the Binance depth stream:

- the 10 best sell orders (red) above and the 10 best buy orders (green) below, the best
  prices closest to the middle row with the mid price and the spread ("Spread 0,012%");
- price, quantity and cumulative total per level, with a depth bar behind each row sized to
  the cumulative quantity (the largest total on either side is 100%);
- the buyer/seller ratio of the shown volume below the book ("Kupci 58% · Prodavci 42%").

Decimals follow the pair's own data, so ADA/EUR shows more of them than BTC/EUR. Changed cells
are highlighted briefly. "Zastarelo" covers the book when no new book arrived for 5 seconds or
the connection to the server is down. Clicking a price puts it into the new-alert form as the
target price for that pair; nothing is saved until the form is submitted. The "Knjiga naloga"
toggle switches the panel and its subscription off; the choice is remembered in the browser.

## Pair tags and notes

![Markets table with tag chips and a pair note](docs/images/pair-tags.png)

Pairs in the markets table can carry colored tags ("Dugoročno", "Rizično") and a short note:

- Up to 3 tag chips are shown next to the pair name, the rest as "+2"; hovering it lists all
  of them. A 🗒 icon marks a pair with a note; hovering it (tapping on a phone) shows the text.
- Clicking a chip shows only pairs with that tag ("Oznaka: Dugoročno ✕" above the table). The
  tag filter combines with the search and "Samo watchlist", and is remembered in the browser;
  it is dropped when the tag is deleted.
- "⋯" at the end of a row edits that pair: check up to 5 tags, create a new tag with one of
  8 colors, and write a note of up to 500 characters. "Upravljaj oznakama" renames, recolors
  and deletes tags; deleting asks first and says how many pairs use the tag.
- Changes made in one browser appear in every open dashboard right away. On a read-only
  server (`NOTES_WRITE_ENABLED=false`) the edit controls are hidden.

## Event processing architecture

```text
Binance WebSocket
      │  ticker, rolling, kline, status
      ▼
MarketDataService ── keeps the latest state, drops duplicates
      │  publish(): only enqueues, never waits for a subscriber (*)
      ▼
EventBus ── one bounded queue + one worker task per subscriber
      ├─► [latest   queue] ─► worker ─► browser_broadcast  (WebSocket to browsers)
      ├─► [keep_all queue] ─► worker ─► candle_storage     (database)
      └─► [keep_all queue] ─► worker ─► alerts             (price alerts)
```

(*) Except when a `keep_all` queue is full, see below. Order books take their own path
(Binance → OrderBookService → browsers) and do not go through the EventBus.

Each subscriber works at its own pace, so a slow database write no longer delays the prices
in the browser. What happens when a subscriber falls behind depends on its queue policy:

- **`latest`** (browsers): only the newest event per pair and kind waits in the queue. A new
  BTC/EUR price replaces the waiting one in place (counted as `replaced`), so a slow browser
  connection gets the current price instead of a backlog of old ones. Closed candles have
  their own key and are never replaced by the next candle. If the queue is full of different
  pairs, the oldest waiting event is dropped.
- **`keep_all`** (database, alerts): every event is kept, in order. When the queue is full,
  `publish` waits up to 1 s for room (backpressure); if there is still none, the event is
  dropped, counted as `dropped` and logged as an error.

Queue sizes come from `EVENT_QUEUE_SIZE_LATEST` (default 100) and
`EVENT_QUEUE_SIZE_KEEP_ALL` (default 1000). `GET /api/status` lists every queue under
`event_queues`: current and largest size seen, processed, replaced and dropped events, and the
delay from publish until the subscriber has handled the event (average and p95 over the last
1000 events). A queue more than 80% full or with a p95 delay above 500 ms logs a WARNING, at
most once a minute per queue.

## Limitations

- Chart notes (`/api/notes`) are shared by every visitor: there are no accounts, so anyone
  who can open the app can add, edit or delete any note. At most 500 notes are stored.
- For a public demo, set `NOTES_WRITE_ENABLED=false`: notes stay visible, but creating,
  editing and deleting them returns 403. `GET /api/notes/config` tells the UI which mode
  the server is in.
- Notes are not removed by the candle cleanup (`CANDLE_RETENTION_DAYS`).
- Pair tags and pair notes (`/api/tags`, `/api/pairs`) are shared by every visitor too:
  anyone can create, rename or delete a tag, change which tags a pair has, or edit its note.
  At most 20 tags in total and 5 per pair; a pair note is at most 500 characters.
  `NOTES_WRITE_ENABLED=false` makes them read-only as well.

## Running locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Alternatively, `python -m app` starts the same server with `HOST` and `PORT` from `.env`
(default `127.0.0.1:8000`); auto-reload is on only when `APP_ENV=development`.

Checks before committing:

```bash
pytest
ruff check .
mypy
```
