from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, DecimalString


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    target_price: Mapped[Decimal] = mapped_column(DecimalString)
    # "above" (price >= target) or "below" (price <= target).
    condition: Mapped[str] = mapped_column(String(5))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    triggered_price: Mapped[Decimal | None] = mapped_column(DecimalString, default=None)
