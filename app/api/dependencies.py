from collections.abc import Collection

from fastapi import HTTPException, Request

from app.services.market_data_service import MarketDataService


def get_market_service(request: Request) -> MarketDataService:
    # A dependency (not a global) so tests can swap in a fake via app.dependency_overrides.
    return request.app.state.market_service


def normalize_symbol(raw: str, allowed: Collection[str]) -> str:
    """Turn "btcusdt", "BTC-USDT" or "BTC/USDT" into "BTCUSDT"; unknown pairs are a 404."""
    symbol = raw.strip().upper().replace("-", "").replace("/", "")
    if symbol not in allowed:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {raw}")
    return symbol
