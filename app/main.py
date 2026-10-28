import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api.dependencies import get_market_service
from app.api.routes_alerts import partials_router as alerts_partials_router
from app.api.routes_alerts import router as alerts_router
from app.api.routes_convert import router as convert_router
from app.api.routes_export import router as export_router
from app.api.routes_history import router as history_router
from app.api.routes_market import router as market_router
from app.api.routes_notes import router as notes_router
from app.api.routes_status import router as status_router
from app.api.routes_watchlist import router as watchlist_router
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.middleware import RequestTimingMiddleware
from app.services.alert_service import AlertService
from app.services.candle_service import CandleService
from app.services.converter import EUR, base_assets
from app.services.exchange_client import (
    BinanceRestClient,
    ExchangeUnavailableError,
    to_display_symbol,
)
from app.services.history_service import HistoryService
from app.services.market_data_service import MarketDataService, MarketEvent
from app.services.rest_fallback import RestFallbackPoller
from app.services.runtime_stats import RuntimeStats
from app.websocket.manager import ConnectionManager
from app.websocket.routes import market_event_message
from app.websocket.routes import router as websocket_router

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    # uvicorn sets up its own handlers before importing the app; handing its records to
    # the root logger gives server and application lines the same format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    stats = RuntimeStats()
    service = MarketDataService(get_settings(), stats)
    manager = ConnectionManager(stats)
    rest_client = BinanceRestClient(get_settings())
    candle_service = CandleService(rest_client, SessionLocal, stats=stats)
    alert_service = AlertService(SessionLocal, service, manager, stats)
    await alert_service.load_active()
    app.state.runtime_stats = stats
    app.state.market_service = service
    app.state.connection_manager = manager
    app.state.candle_service = candle_service
    app.state.history_service = HistoryService(rest_client)
    app.state.alert_service = alert_service

    async def broadcast_market_event(event: MarketEvent) -> None:
        await manager.broadcast(market_event_message(event, service))

    service.subscribe(broadcast_market_event)
    service.subscribe(candle_service.handle_event)
    service.subscribe(alert_service.handle_event)
    await service.start()
    cleanup_task = asyncio.create_task(
        candle_service.run_cleanup(get_settings().candle_retention_days)
    )
    app.state.candle_cleanup_task = cleanup_task
    rest_fallback = RestFallbackPoller(
        service,
        lambda: rest_client.get_tickers(get_settings().supported_symbols),
        after_seconds=get_settings().rest_fallback_after_seconds,
        interval_seconds=get_settings().rest_fallback_interval_seconds,
    )
    rest_fallback_task = asyncio.create_task(rest_fallback.run())
    app.state.rest_fallback_task = rest_fallback_task
    yield
    # Cancelled before the REST client closes, so no fallback request uses a closed client.
    for task in (cleanup_task, rest_fallback_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await service.stop()
    await rest_client.close()


API_DESCRIPTION = """
Live prices for EUR crypto pairs from Binance, with a watchlist, price alerts, a currency
converter and a historical value calculator.

**Informational only.** ChangeWatch Crypto shows market data; it does not place orders,
hold funds or execute trades.

Prices and amounts are decimal strings (e.g. `"72309.76"`) so no precision is lost.
Live updates are also pushed over the WebSocket at `/ws/market`.
"""

OPENAPI_TAGS = [
    {"name": "markets", "description": "Live prices, 24h statistics and chart candles."},
    {"name": "convert", "description": "Convert amounts between EUR and crypto at live prices."},
    {
        "name": "history",
        "description": "What an amount invested in the past would be worth today.",
    },
    {"name": "watchlist", "description": "Pairs the user follows, stored in the database."},
    {"name": "alerts", "description": "Price alerts that fire once when a target is reached."},
    {"name": "notes", "description": "Text notes tied to a moment on a pair's chart."},
    {"name": "export", "description": "Download candles, notes and alerts as CSV files."},
    {"name": "status", "description": "Runtime diagnostics of the server."},
]

app = FastAPI(
    title="ChangeWatch Crypto",
    version=__version__,
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.add_middleware(RequestTimingMiddleware)
app.include_router(market_router)
app.include_router(convert_router)
app.include_router(history_router)
app.include_router(status_router)
app.include_router(watchlist_router)
app.include_router(alerts_router)
app.include_router(alerts_partials_router)
app.include_router(notes_router)
app.include_router(export_router)
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


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, service: MarketService) -> HTMLResponse:
    # Only the pair list is rendered on the server; prices arrive over /ws/market.
    pairs = [
        {"symbol": symbol, "display_symbol": to_display_symbol(symbol)}
        for symbol in service.settings.supported_symbols
    ]
    currencies = [EUR, *base_assets(service.settings.supported_symbols)]
    return templates.TemplateResponse(
        request, "index.html", {"pairs": pairs, "currencies": currencies}
    )


# Hidden from /docs: it is for the deploy platform's health checks, not for API clients.
@app.get("/health", include_in_schema=False)
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
