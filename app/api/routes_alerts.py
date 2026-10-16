from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.api.dependencies import get_alert_service
from app.models import PriceAlert
from app.schemas.alert import AlertCreate, AlertOut, AlertUpdate
from app.services.alert_service import AlertService
from app.services.exchange_client import to_display_symbol

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
partials_router = APIRouter(prefix="/partials", include_in_schema=False)

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["display_symbol"] = to_display_symbol

Alerts = Annotated[AlertService, Depends(get_alert_service)]


@router.get("", response_model=list[AlertOut])
async def list_alerts(alert_service: Alerts, active: bool | None = None) -> list[PriceAlert]:
    return await alert_service.list_alerts(active)


@router.post("", response_model=AlertOut, status_code=201)
async def create_alert(payload: AlertCreate, alert_service: Alerts) -> PriceAlert:
    return await alert_service.create(payload)


@router.patch("/{alert_id}", response_model=AlertOut)
async def update_alert(alert_id: int, payload: AlertUpdate, alert_service: Alerts) -> PriceAlert:
    alert = await alert_service.update(alert_id, payload)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert ne postoji")
    return alert


@router.delete("/{alert_id}", status_code=204)
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


@partials_router.post("/alerts", response_class=HTMLResponse)
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
    await alert_service.create(payload)
    return await render_alerts_list(request, alert_service)
