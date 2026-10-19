# ChangeWatch Crypto

Real-time cryptocurrency market monitor built with FastAPI, using public Binance market data.

## Price alerts

An alert ("BTC/EUR above 100,000") is checked on every fresh ticker price and fires exactly
once: it is then switched off and stays off, also after a restart. `PATCH /api/alerts/{id}`
with `{"is_active": true}` re-enables it.

An alert created while its condition is already met fires on the next ticker. This is
expected: alerts are checked when new market data arrives, not when they are created.
Stale or missing prices never fire an alert.

## Running locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Alternatively, `python -m app` starts the same server with `HOST` and `PORT` from `.env`
(default `127.0.0.1:8000`); auto-reload is on only when `APP_ENV=development`.
