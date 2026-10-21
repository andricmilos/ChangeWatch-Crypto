from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sqlalchemy import String, TypeDecorator, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


class DecimalString(TypeDecorator):
    """Stores Decimal as text, because SQLite has no exact decimal type."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


def enable_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Applies the SQLite settings on every new connection (PRAGMAs are per connection)."""
    if engine.dialect.name != "sqlite":
        return
    # An in-memory database has no file for a write-ahead log, so WAL is skipped there.
    in_memory = engine.url.database in (None, "", ":memory:")

    def on_connect(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            if not in_memory:
                # Readers no longer wait for the writer (live candles, alerts) and vice versa.
                cursor.execute("PRAGMA journal_mode=WAL")
            # Safe with WAL: a power loss can drop the last commits, never corrupt the file.
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    event.listen(engine.sync_engine, "connect", on_connect)


engine = create_async_engine(settings.database_url)
enable_sqlite_pragmas(engine)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    # Imported here so every model is registered on Base.metadata; a top-level
    # import would be circular, since the models import Base from this module.
    import app.models  # noqa: F401

    database = make_url(settings.database_url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
