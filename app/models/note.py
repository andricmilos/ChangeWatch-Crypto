from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Note(Base):
    __tablename__ = "notes"
    # Every read is "notes of one pair in a time range", sorted by time.
    __table_args__ = (Index("ix_notes_symbol_time_ms", "symbol", "time_ms"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    # Moment on the chart the note is about, in UTC milliseconds like Candle.open_time.
    time_ms: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(String(280))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
