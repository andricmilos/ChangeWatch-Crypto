from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.api.dependencies import get_alert_service
from app.api.rate_limit import write_limiter
from app.models import PriceAlert
from app.schemas.alert import AlertCreate, AlertOut, AlertUpdate
from app.schemas.error import RATE_LIMITED, error_response, validation_error
from app.services.alert_service import AlertService
from app.services.exchange_client import quote_asset, to_display_symbol

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
partials_router = APIRouter(prefix="/partials", include_in_schema=False)

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["display_symbol"] = to_display_symbol
# Currency label next to prices, e.g. "75000 EUR"; empty for an unknown quote currency.
templates.env.filters["quote_asset"] = lambda symbol: quote_asset(symbol) or ""

Alerts = Annotated[AlertService, Depends(get_alert_service)]
RateLimited = [Depends(write_limiter)]

MAX_ACTIVE_ALERTS = 50
TOO_MANY_ALERTS = f"Najviše {MAX_ACTIVE_ALERTS} aktivnih alerta; obriši ili isključi neki"
ALERT_NOT_FOUND = error_response("No alert with this id.", "Alert ne postoji")


@router.get(
    "",
    response_model=list[AlertOut],
    summary="List price alerts",
    description="All alerts, or only active (active=true) or inactive (active=false) ones.",
    responses={422: validation_error("active is not a boolean.")},
)
async def list_alerts(alert_service: Alerts, active: bool | None = None) -> list[PriceAlert]:
    return await alert_service.list_alerts(active)


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
