from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Candle


def make_candle() -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time=1760380800000,
        close_time=1760380859999,
        open_price=Decimal("113100.10000000"),
        high_price=Decimal("113300.00000000"),
        low_price=Decimal("113050.01000000"),
        close_price=Decimal("113250.55000000"),
        volume=Decimal("18.23455100"),
    )


async def test_candle_round_trip_keeps_exact_decimals(session):
    session.add(make_candle())
    await session.commit()
    session.expunge_all()  # force a real read from the database, not the identity map

    candle = (await session.scalars(select(Candle))).one()

    assert isinstance(candle.close_price, Decimal)
    assert candle.close_price == Decimal("113250.55000000")
    # str() also checks trailing zeros survive, i.e. nothing went through float.
    assert str(candle.close_price) == "113250.55000000"
    assert str(candle.open_price) == "113100.10000000"
    assert str(candle.high_price) == "113300.00000000"
    assert str(candle.low_price) == "113050.01000000"
    assert str(candle.volume) == "18.23455100"
    assert candle.open_time == 1760380800000
    assert candle.close_time == 1760380859999


async def test_same_candle_cannot_be_stored_twice(session):
    session.add(make_candle())
    await session.commit()

    session.add(make_candle())
    with pytest.raises(IntegrityError):
        await session.commit()

