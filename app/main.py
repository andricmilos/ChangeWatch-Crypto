import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes_market import router as market_router
from app.config import get_settings
from app.services.market_data_service import MarketDataService, MarketEvent
from app.websocket.manager import ConnectionManager
from app.websocket.routes import market_event_message
from app.websocket.routes import router as websocket_router

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = MarketDataService(get_settings())
    manager = ConnectionManager()
    app.state.market_service = service
    app.state.connection_manager = manager

    async def broadcast_market_event(event: MarketEvent) -> None:
        await manager.broadcast(market_event_message(event, service))

    service.subscribe(broadcast_market_event)
    await service.start()
    yield
    await service.stop()


app = FastAPI(title="ChangeWatch Crypto", lifespan=lifespan)
app.include_router(market_router)
app.include_router(websocket_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
