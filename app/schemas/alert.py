from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.api.dependencies import normalize_symbol
from app.config import get_settings
from app.services.exchange_client import to_display_symbol

AlertCondition = Literal["above", "below"]
# allow_inf_nan is off by default for Decimal, so "NaN" and "Infinity" are rejected too.
TargetPrice = Annotated[Decimal, Field(gt=0, decimal_places=8)]


class AlertCreate(BaseModel):
    symbol: str
    target_price: TargetPrice
    condition: AlertCondition

    @field_validator("symbol")
    @classmethod
    def symbol_in_allowlist(cls, value: str) -> str:
        # normalize_symbol raises a 404 meant for paths; in a request body it must be a 422.
        try:
            return normalize_symbol(value, get_settings().supported_symbols)
        except HTTPException as exc:
            raise ValueError(f"Unsupported symbol: {value}") from exc


class AlertUpdate(BaseModel):
    is_active: bool | None = None
    target_price: TargetPrice | None = None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    target_price: Decimal
    condition: AlertCondition
    is_active: bool
    created_at: datetime
    triggered_at: datetime | None
    triggered_price: Decimal | None

    @computed_field
    @property
    def display_symbol(self) -> str:
        return to_display_symbol(self.symbol)
