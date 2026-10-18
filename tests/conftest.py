from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.api.rate_limit import history_limiter, write_limiter
from app.database import Base


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # In-memory SQLite, so tests never touch data/changewatch.db. StaticPool keeps one
    # connection open; otherwise every new connection would see an empty database.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_sessionmaker(engine, expire_on_commit=False)() as db_session:
        yield db_session

    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> Iterator[None]:
    # Every TestClient request comes from the same "IP", so without a reset the shared
    # limiter would count requests across tests and start answering 429.
    for limiter in (write_limiter, history_limiter):
        limiter.reset()
    yield
    for limiter in (write_limiter, history_limiter):
        limiter.reset()
