# ChangeWatch Crypto

Real-time cryptocurrency market monitor built with FastAPI, using public Binance market data.

## Price alerts

An alert ("BTC/USDT above 100,000") is checked on every fresh ticker price and fires exactly
once: it is then switched off and stays off, also after a restart. `PATCH /api/alerts/{id}`
with `{"is_active": true}` re-enables it.

An alert created while its condition is already met fires on the next ticker. This is
expected: alerts are checked when new market data arrives, not when they are created.
Stale or missing prices never fire an alert.
