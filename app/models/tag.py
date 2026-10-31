from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))
    # name.casefold(): "Rizično" and "RIZIČNO" are the same tag. Kept as a column because
    # SQLite's lower() only folds ASCII letters; unique, so even racing requests cannot
    # create both.
    name_key: Mapped[str] = mapped_column(String(20), unique=True)
    color: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PairTag(Base):
    __tablename__ = "pair_tags"

    # The composite primary key makes (symbol, tag_id) unique.
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    # SQLite only enforces the cascade with PRAGMA foreign_keys=ON, which this app does not
    # set, so the service deletes a tag's links itself (SPEC 16.7).
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class PairNote(Base):
    __tablename__ = "pair_notes"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    text: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
