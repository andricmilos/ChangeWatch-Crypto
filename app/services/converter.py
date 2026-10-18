from collections.abc import Iterable
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from app.schemas.convert import ConversionResponse, ConversionResult, PriceType
from app.schemas.market import MarketTicker
from app.services.exchange_client import quote_asset

EUR = "EUR"
CRYPTO_STEP = Decimal("0.00000001")
EUR_STEP = Decimal("0.01")
# Enough digits that 1e9 times a price with 8 decimals is never rounded mid-calculation.
PRECISION = 50
# A result built from two pairs is only as fresh as the worse of the two.
FRESHNESS_RANK = {"live": 0, "stale": 1, "no_data": 2}


def base_assets(symbols: Iterable[str]) -> list[str]:
    """Base currencies of the EUR pairs in allowlist order: "BTCEUR" -> "BTC"."""
    return [symbol[: -len(EUR)] for symbol in symbols if quote_asset(symbol) == EUR]


def pick_price(ticker: MarketTicker, buying: bool) -> tuple[PriceType, Decimal]:
    # Buying crypto for EUR pays the ask, selling it gets the bid; without both sides of the
    # book the last trade price is the best estimate.
    if ticker.bid_price is not None and ticker.ask_price is not None:
        return ("ask", ticker.ask_price) if buying else ("bid", ticker.bid_price)
    return "last", ticker.last_price


def _worse(*freshness: str) -> str:
    return max(freshness, key=FRESHNESS_RANK.__getitem__)


def _buy_result(
    currency: str, market: dict[str, Any], eur_amount: Decimal | None, source_freshness: str
) -> ConversionResult:
    ticker: MarketTicker | None = market["ticker"]
    freshness = _worse(market["freshness"], source_freshness)
    if ticker is None:
        return ConversionResult(currency=currency, symbol=market["symbol"], freshness=freshness)

    price_type, price = pick_price(ticker, buying=True)
    amount = None
    if eur_amount is not None:
        amount = (eur_amount / price).quantize(CRYPTO_STEP, rounding=ROUND_DOWN)
    return ConversionResult(
        currency=currency,
        symbol=market["symbol"],
        amount=amount,
        price_type=price_type,
        price=price,
        updated_at=market["updated_at"],
        freshness=freshness,
    )


def convert(amount: Decimal, currency: str, markets: list[dict[str, Any]]) -> ConversionResponse:
    """Convert `amount` of EUR or of a base currency, using MarketDataService.snapshot() markets.

    EUR -> crypto uses each pair's ask; crypto -> EUR uses the source pair's bid, and the
    other cryptos are then bought with that EUR value at their ask.
    """
    by_base = {
        market["symbol"][: -len(EUR)]: market
        for market in markets
        if quote_asset(market["symbol"]) == EUR
    }

    with localcontext() as context:
        context.prec = PRECISION
        if currency == EUR:
            results = [
                _buy_result(base, market, amount, "live") for base, market in by_base.items()
            ]
            return ConversionResponse(amount=amount, currency=currency, results=results)

        source = by_base[currency]
        source_ticker: MarketTicker | None = source["ticker"]
        eur_result = ConversionResult(
            currency=EUR, symbol=source["symbol"], freshness=source["freshness"]
        )
        # Kept unrounded, so the other cryptos are not computed from a value cut to cents.
        eur_amount = None
        if source_ticker is not None:
            price_type, price = pick_price(source_ticker, buying=False)
            eur_amount = amount * price
            eur_result = eur_result.model_copy(
                update={
                    "amount": eur_amount.quantize(EUR_STEP, rounding=ROUND_HALF_UP),
                    "price_type": price_type,
                    "price": price,
                    "updated_at": source["updated_at"],
                }
            )

        others = [
            _buy_result(base, market, eur_amount, source["freshness"])
            for base, market in by_base.items()
            if base != currency
        ]
        return ConversionResponse(amount=amount, currency=currency, results=[eur_result, *others])
