from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.api.dependencies import get_alert_service, get_market_service, normalize_symbol
from app.api.rate_limit import write_limiter
from app.models import PriceAlert
from app.schemas.alert import AlertCreate, AlertOut, AlertUpdate
from app.schemas.error import (
    RATE_LIMITED,
    UNKNOWN_SYMBOL,
    error_response,
    validation_error,
    validation_or_error,
)
from app.services.alert_service import AlertService
from app.services.exchange_client import quote_asset, to_display_symbol
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
partials_router = APIRouter(prefix="/partials", include_in_schema=False)

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["display_symbol"] = to_display_symbol
# Currency label next to prices, e.g. "75000 EUR"; empty for an unknown quote currency.
templates.env.filters["quote_asset"] = lambda symbol: quote_asset(symbol) or ""

Alerts = Annotated[AlertService, Depends(get_alert_service)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
RateLimited = [Depends(write_limiter)]

MAX_ACTIVE_ALERTS = 50
TOO_MANY_ALERTS = f"Najviše {MAX_ACTIVE_ALERTS} aktivnih alerta; obriši ili isključi neki"
ALERT_NOT_FOUND = error_response("No alert with this id.", "Alert ne postoji")

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# 9999-12-31T23:59:59.999Z; anything larger would overflow datetime and end in a 500.
MAX_TIME_MS = 253_402_300_799_999
LAST_MICROSECONDS = timedelta(microseconds=999)
TriggeredMs = Annotated[
    int | None,
    Query(ge=0, le=MAX_TIME_MS, description="UTC milliseconds, inclusive."),
]


def from_ms(ms: int) -> datetime:
    # timedelta keeps whole milliseconds exact, unlike datetime.fromtimestamp(ms / 1000).
    return EPOCH + timedelta(milliseconds=ms)


@router.get(
    "",
    response_model=list[AlertOut],
    summary="List price alerts",
    description=(
        "All alerts (newest first), or only active (active=true) or inactive (active=false) "
        "ones. symbol limits the list to one pair. With triggered_from_ms or triggered_to_ms "
        "only fired alerts in that period are returned, sorted by triggered_at (oldest first)."
    ),
    responses={
        404: UNKNOWN_SYMBOL,
        422: validation_or_error(
            "active is not a boolean, a time is not an integer in range, or "
            "triggered_from_ms > triggered_to_ms."
        ),
    },
)
async def list_alerts(
    alert_service: Alerts,
    service: MarketService,
    active: bool | None = None,
    symbol: str | None = None,
    triggered_from_ms: TriggeredMs = None,
    triggered_to_ms: TriggeredMs = None,
) -> list[PriceAlert]:
    normalized = (
        None if symbol is None else normalize_symbol(symbol, service.settings.supported_symbols)
    )
    if (
        triggered_from_ms is not None
        and triggered_to_ms is not None
        and triggered_from_ms > triggered_to_ms
    ):
        raise HTTPException(
            status_code=422, detail="triggered_from_ms mora biti manje ili jednako triggered_to_ms"
        )
    if triggered_from_ms is None and triggered_to_ms is None:
        return await alert_service.list_alerts(active, normalized)
    return await alert_service.list_triggered(
        normalized,
        None if triggered_from_ms is None else from_ms(triggered_from_ms),
        # triggered_at has microseconds; the whole triggered_to_ms millisecond counts.
        None if triggered_to_ms is None else from_ms(triggered_to_ms) + LAST_MICROSECONDS,
        active,
    )


@router.post(
    "",
    response_model=AlertOut,
    status_code=201,
    dependencies=RateLimited,
    summary="Create a price alert",
    description=(
        "Fires once when the live price reaches the target. "
        f"At most {MAX_ACTIVE_ALERTS} alerts can be active at a time."
    ),
    responses={
        400: error_response(f"Already {MAX_ACTIVE_ALERTS} active alerts.", TOO_MANY_ALERTS),
        422: validation_error(
            "Unsupported symbol, unknown condition, or a target price not greater than 0 "
            "or with more than 8 decimals."
        ),
        429: RATE_LIMITED,
    },
)
async def create_alert(payload: AlertCreate, alert_service: Alerts) -> PriceAlert:
    if alert_service.active_count() >= MAX_ACTIVE_ALERTS:
        raise HTTPException(status_code=400, detail=TOO_MANY_ALERTS)
    return await alert_service.create(payload)


@router.patch(
    "/{alert_id}",
    response_model=AlertOut,
    dependencies=RateLimited,
    summary="Update a price alert",
    description=(
        "Turns an alert on or off or changes its target price. "
        "Turning a fired alert back on clears triggered_at."
    ),
    responses={
        404: ALERT_NOT_FOUND,
        422: validation_error("Non-integer id or invalid target price."),
        429: RATE_LIMITED,
    },
)
async def update_alert(alert_id: int, payload: AlertUpdate, alert_service: Alerts) -> PriceAlert:
    alert = await alert_service.update(alert_id, payload)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert ne postoji")
    return alert


@router.delete(
    "/{alert_id}",
    status_code=204,
    dependencies=RateLimited,
    summary="Delete a price alert",
    description="Deletes the alert; the response has no body.",
    responses={
        404: ALERT_NOT_FOUND,
        422: validation_error("Non-integer id."),
        429: RATE_LIMITED,
    },
)
async def delete_alert(alert_id: int, alert_service: Alerts) -> Response:
    if not await alert_service.delete(alert_id):
        raise HTTPException(status_code=404, detail="Alert ne postoji")
    return Response(status_code=204)


async def render_alerts_list(
    request: Request, alert_service: AlertService, error: str | None = None
) -> HTMLResponse:
    alerts = await alert_service.list_alerts()
    context = {
        "active": [alert for alert in alerts if alert.is_active],
        "triggered": [alert for alert in alerts if alert.triggered_at is not None],
        "disabled": [
            alert for alert in alerts if not alert.is_active and alert.triggered_at is None
        ],
        "error": error,
    }
    return templates.TemplateResponse(request, "partials/alerts_list.html", context)


@partials_router.get("/alerts", response_class=HTMLResponse)
async def alerts_partial(request: Request, alert_service: Alerts) -> HTMLResponse:
    return await render_alerts_list(request, alert_service)


@partials_router.post("/alerts", response_class=HTMLResponse, dependencies=RateLimited)
async def create_alert_partial(
    request: Request,
    alert_service: Alerts,
    symbol: Annotated[str, Form()],
    condition: Annotated[str, Form()],
    target_price: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        payload = AlertCreate(symbol=symbol, condition=condition, target_price=target_price)
    except ValidationError:
        # Status 200 on purpose: htmx does not swap 4xx responses into the page by default,
        # and the message has to appear next to the form.
        return await render_alerts_list(
            request,
            alert_service,
            error="Neispravan alert: izaberi par i uslov i unesi cenu veću od 0 "
            "(najviše 8 decimala).",
        )
    if alert_service.active_count() >= MAX_ACTIVE_ALERTS:
        return await render_alerts_list(request, alert_service, error=TOO_MANY_ALERTS)
    await alert_service.create(payload)
    return await render_alerts_list(request, alert_service)
