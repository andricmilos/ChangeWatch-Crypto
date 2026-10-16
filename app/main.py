import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.dependencies import get_market_service
from app.api.routes_alerts import partials_router as alerts_partials_router
from app.api.routes_alerts import router as alerts_router
from app.api.routes_market import router as market_router
from app.api.routes_watchlist import router as watchlist_router
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.services.alert_service import AlertService
from app.services.candle_service import CandleService
from app.services.exchange_client import (
    BinanceRestClient,
    ExchangeUnavailableError,
    to_display_symbol,
)
from app.services.market_data_service import MarketDataService, MarketEvent
from app.websocket.manager import ConnectionManager
from app.websocket.routes import market_event_message
from app.websocket.routes import router as websocket_router

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    service = MarketDataService(get_settings())
    manager = ConnectionManager()
    rest_client = BinanceRestClient(get_settings())
    candle_service = CandleService(rest_client, SessionLocal)
    alert_service = AlertService(SessionLocal, service, manager)
    await alert_service.load_active()
    app.state.market_service = service
    app.state.connection_manager = manager
    app.state.candle_service = candle_service
    app.state.alert_service = alert_service

    async def broadcast_market_event(event: MarketEvent) -> None:
        await manager.broadcast(market_event_message(event, service))

    service.subscribe(broadcast_market_event)
    service.subscribe(candle_service.handle_event)
    service.subscribe(alert_service.handle_event)
    await service.start()
    yield
    await service.stop()
    await rest_client.close()


app = FastAPI(title="ChangeWatch Crypto", lifespan=lifespan)
app.include_router(market_router)
app.include_router(watchlist_router)
app.include_router(alerts_router)
app.include_router(alerts_partials_router)
app.include_router(websocket_router)


@app.exception_handler(ExchangeUnavailableError)
async def exchange_unavailable_handler(
    request: Request, exc: ExchangeUnavailableError
) -> JSONResponse:
    logger.warning("Exchange unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "Berza trenutno nije dostupna"})


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # The full trace goes to the log only; the response must not reveal code or paths.
    logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Interna greška"})


app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

MarketService = Annotated[MarketDataService, Depends(get_market_service)]


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, service: MarketService) -> HTMLResponse:
    # Only the pair list is rendered on the server; prices arrive over /ws/market.
    pairs = [
        {"symbol": symbol, "display_symbol": to_display_symbol(symbol)}
        for symbol in service.settings.supported_symbols
    ]
    return templates.TemplateResponse(request, "index.html", {"pairs": pairs})


@app.get("/health")
async def health(service: MarketService) -> dict[str, Any]:
    client = service.client
    seconds_since_last_message = None
    if client.last_message_at is not None:
        age = datetime.now(UTC) - client.last_message_at
        seconds_since_last_message = round(age.total_seconds(), 1)
    # Always HTTP 200: the deploy platform restarts unhealthy containers, and a restart
    # does not fix a Binance outage. "degraded" says the server runs without live data.
    return {
        "status": "ok" if client.status == "connected" else "degraded",
        "exchange_connection": client.status,
        "seconds_since_last_message": seconds_since_last_message,
    }
