import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.config import Settings
from app.database import Base, enable_sqlite_pragmas
from app.models import Candle, PriceAlert, WatchlistItem
from app.services.alert_service import AlertService
from app.services.candle_service import CandleService
from app.services.exchange_client import datetime_to_ms
from app.services.market_data_service import MarketDataService
from app.services.runtime_stats import RuntimeStats

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


async def make_engine(url: str, **kwargs) -> AsyncEngine:
    engine = create_async_engine(url, **kwargs)
    enable_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


@pytest.fixture
async def file_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = await make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(file_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(file_engine, expire_on_commit=False)


def candle_row(open_time: datetime) -> Candle:
    open_ms = datetime_to_ms(open_time)
    price = Decimal("72416.85")
    return Candle(
        symbol="BTCEUR",
        interval="1m",
        open_time=open_ms,
        close_time=open_ms + 59_999,
        open_price=price,
        high_price=price,
        low_price=price,
        close_price=price,
        volume=Decimal("1"),
    )


# ---------- SQLite settings ----------


async def test_file_database_uses_wal(file_engine, tmp_path):
    async with file_engine.connect() as conn:
        assert (await conn.scalar(text("PRAGMA journal_mode"))) == "wal"
        assert (await conn.scalar(text("PRAGMA busy_timeout"))) == 5000
        # NORMAL is 1 (OFF=0, FULL=2).
        assert (await conn.scalar(text("PRAGMA synchronous"))) == 1
    assert (tmp_path / "test.db-wal").exists()


async def test_in_memory_database_works_without_wal():
    engine = await make_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.connect() as conn:
            assert (await conn.scalar(text("PRAGMA journal_mode"))) == "memory"
            assert (await conn.scalar(text("PRAGMA busy_timeout"))) == 5000
    finally:
        await engine.dispose()


# ---------- Candle cleanup ----------


async def test_cleanup_deletes_only_candles_older_than_retention(session_factory):
    async with session_factory() as session:
        session.add_all(
            [
                candle_row(NOW - timedelta(days=31)),
                candle_row(NOW - timedelta(days=30, minutes=1)),
                candle_row(NOW - timedelta(days=29, hours=23)),
                candle_row(NOW - timedelta(minutes=1)),
            ]
        )
        await session.commit()
    candle_service = CandleService(rest_client=None, session_factory=session_factory)

    deleted = await candle_service.delete_old_candles(30, now=NOW)

    assert deleted == 2
    async with session_factory() as session:
        remaining = (await session.scalars(select(Candle.open_time))).all()
    assert sorted(remaining) == [
        datetime_to_ms(NOW - timedelta(days=29, hours=23)),
        datetime_to_ms(NOW - timedelta(minutes=1)),
    ]


async def test_cleanup_leaves_watchlist_and_alerts_untouched(session_factory):
    long_ago = NOW - timedelta(days=400)
    async with session_factory() as session:
        session.add_all(
            [
                candle_row(long_ago),
                WatchlistItem(symbol="BTCEUR", created_at=long_ago),
                PriceAlert(
                    symbol="BTCEUR",
                    target_price=Decimal("75000"),
                    condition="above",
                    created_at=long_ago,
                ),
            ]
        )
        await session.commit()
    candle_service = CandleService(rest_client=None, session_factory=session_factory)

    assert await candle_service.delete_old_candles(30, now=NOW) == 1

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WatchlistItem)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceAlert)) == 1


async def test_cleanup_loop_records_result_and_survives_errors(session_factory):
    stats = RuntimeStats()
    candle_service = CandleService(rest_client=None, session_factory=session_factory, stats=stats)
    async with session_factory() as session:
        session.add(candle_row(datetime.now(UTC) - timedelta(days=40)))
        await session.commit()

    task = asyncio.create_task(candle_service.run_cleanup(30, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    assert stats.last_cleanup_result == "ok"
    assert stats.last_cleanup_at is not None

    def broken_session():
        raise OperationalError("DELETE", {}, Exception("database is locked"))

    candle_service.session_factory = broken_session
    await asyncio.sleep(0.05)
    assert stats.last_cleanup_result == "error"
    assert stats.last_cleanup_deleted is None
    # The error did not end the loop.
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_retention_must_be_greater_than_zero():
    with pytest.raises(ValidationError, match="candle_retention_days must be greater than 0"):
        Settings(_env_file=None, candle_retention_days=0)


# ---------- Lifespan ----------


async def test_lifespan_starts_cleanup_and_cancels_it_on_shutdown(monkeypatch, session_factory):
    async def noop(*_args) -> None:
        return None

    # No Binance connection and no real database: only the cleanup task runs for real.
    monkeypatch.setattr(main_module, "init_db", noop)
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(MarketDataService, "start", noop)
    monkeypatch.setattr(MarketDataService, "stop", noop)
    monkeypatch.setattr(AlertService, "load_active", noop)
    test_app = FastAPI()

    async with main_module.lifespan(test_app):
        task = test_app.state.candle_cleanup_task
        await asyncio.sleep(0.05)
        assert not task.done()
        assert test_app.state.runtime_stats.last_cleanup_result == "ok"

    assert task.cancelled()
