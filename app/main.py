import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.services.market_data_service import MarketDataService

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = MarketDataService(get_settings())
    app.state.market_service = service
    await service.start()
    yield
    await service.stop()


app = FastAPI(title="ChangeWatch Crypto", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
