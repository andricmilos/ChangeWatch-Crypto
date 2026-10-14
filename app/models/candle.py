from decimal import Decimal

from sqlalchemy import BigInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, DecimalString


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol", "interval", "open_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    interval: Mapped[str] = mapped_column(String(5))
    # Milliseconds since epoch, exactly as Binance sends them (no timezone issues in SQLite).
    open_time: Mapped[int] = mapped_column(BigInteger)
    close_time: Mapped[int] = mapped_column(BigInteger)
    open_price: Mapped[Decimal] = mapped_column(DecimalString)
    high_price: Mapped[Decimal] = mapped_column(DecimalString)
    low_price: Mapped[Decimal] = mapped_column(DecimalString)
    close_price: Mapped[Decimal] = mapped_column(DecimalString)
    volume: Mapped[Decimal] = mapped_column(DecimalString)
