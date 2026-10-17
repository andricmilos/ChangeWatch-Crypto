import logging
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PriceAlert
from app.schemas.alert import AlertCreate, AlertOut, AlertUpdate
from app.schemas.market import MarketTicker
from app.services.market_data_service import MarketDataService, MarketEvent
from app.services.runtime_stats import RuntimeStats
from app.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)


def is_condition_met(condition: str, price: Decimal, target: Decimal) -> bool:
    if condition == "above":
        return price >= target
    if condition == "below":
        return price <= target
    raise ValueError(f"Unknown alert condition: {condition!r}")


class AlertService:
    """Price alerts: stored in SQLite, checked in memory on every fresh ticker."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        market_service: MarketDataService,
        manager: ConnectionManager,
        stats: RuntimeStats | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.stats = stats or RuntimeStats()
        self.market_service = market_service
        self.manager = manager
        # symbol -> active alerts; kept in memory so a tick never has to read the database.
        self._active: dict[str, list[PriceAlert]] = {}

    async def load_active(self) -> None:
        async with self.session_factory() as session:
            alerts = (await session.scalars(select(PriceAlert).where(PriceAlert.is_active))).all()
        grouped: dict[str, list[PriceAlert]] = defaultdict(list)
        for alert in alerts:
            grouped[alert.symbol].append(alert)
        self._active = dict(grouped)

    def active_alerts(self, symbol: str) -> list[PriceAlert]:
        return list(self._active.get(symbol, []))

    def active_count(self) -> int:
        return sum(len(alerts) for alerts in self._active.values())

    async def list_alerts(self, active: bool | None = None) -> list[PriceAlert]:
        query = select(PriceAlert).order_by(PriceAlert.id.desc())
        if active is not None:
            query = query.where(PriceAlert.is_active == active)
        async with self.session_factory() as session:
            return list(await session.scalars(query))

    async def create(self, payload: AlertCreate) -> PriceAlert:
        alert = PriceAlert(
            symbol=payload.symbol, target_price=payload.target_price, condition=payload.condition
        )
        async with self.session_factory() as session:
            session.add(alert)
            await session.commit()
        await self.load_active()
        return alert

    async def update(self, alert_id: int, payload: AlertUpdate) -> PriceAlert | None:
        async with self.session_factory() as session:
            alert = await session.get(PriceAlert, alert_id)
            if alert is None:
                return None
            if payload.target_price is not None:
                alert.target_price = payload.target_price
            if payload.is_active is not None:
                if payload.is_active and not alert.is_active:
                    # Re-enabling starts over: the alert can fire again exactly once.
                    alert.triggered_at = None
                    alert.triggered_price = None
                alert.is_active = payload.is_active
            await session.commit()
        await self.load_active()
        return alert

    async def delete(self, alert_id: int) -> bool:
        async with self.session_factory() as session:
            alert = await session.get(PriceAlert, alert_id)
            if alert is None:
                return False
            await session.delete(alert)
            await session.commit()
        await self.load_active()
        return True

    async def handle_event(self, event: MarketEvent) -> None:
        """MarketDataService subscriber: check alerts against every fresh ticker price."""
        if event["type"] != "ticker":
            return
        ticker: MarketTicker = event["data"]
        # A stale price (or none at all) must never fire an alert.
        if self.market_service.freshness(ticker.symbol) != "live":
            return
        for alert in self.active_alerts(ticker.symbol):
            if is_condition_met(alert.condition, ticker.last_price, alert.target_price):
                await self._trigger(alert, ticker.last_price)

    async def _trigger(self, alert: PriceAlert, price: Decimal) -> None:
        # Out of memory first, so the next tick does not try this alert again.
        alerts = self._active.get(alert.symbol, [])
        if alert not in alerts:
            return
        alerts.remove(alert)

        # The database decides who fires it: only one UPDATE can see is_active = true.
        statement = (
            update(PriceAlert)
            .where(PriceAlert.id == alert.id, PriceAlert.is_active.is_(True))
            .values(is_active=False, triggered_at=datetime.now(UTC), triggered_price=price)
        )
        try:
            async with self.session_factory() as session:
                result = await session.execute(statement)
                await session.commit()
                if result.rowcount == 0:
                    return
                triggered = await session.get(PriceAlert, alert.id, populate_existing=True)
        except SQLAlchemyError as exc:
            self.stats.record_db_write_error()
            logger.error("Failed to store fired alert %s (%s): %r", alert.id, alert.symbol, exc)
            raise

        self.stats.record_alert_triggered()
        logger.info(
            "Alert %s fired: %s %s %s at %s (%d still active)",
            alert.id,
            alert.symbol,
            alert.condition,
            alert.target_price,
            price,
            self.active_count(),
        )
        data = AlertOut.model_validate(triggered).model_dump(mode="json")
        await self.manager.broadcast({"type": "alert_triggered", "data": data})
