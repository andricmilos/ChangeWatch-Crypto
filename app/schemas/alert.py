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
TargetPrice = Annotated[
    Decimal,
    Field(
        gt=0,
        decimal_places=8,
        description="Target price in EUR; greater than 0, at most 8 decimals.",
        examples=["75000.00"],
    ),
]


CONDITION_DESCRIPTION = "above: fires when price >= target; below: fires when price <= target."


class AlertCreate(BaseModel):
    symbol: str = Field(
        description="Supported pair; btceur, BTC-EUR and BTC/EUR are accepted too.",
        examples=["BTCEUR"],
    )
    target_price: TargetPrice
    condition: AlertCondition = Field(description=CONDITION_DESCRIPTION, examples=["above"])

    @field_validator("symbol")
    @classmethod
    def symbol_in_allowlist(cls, value: str) -> str:
        # normalize_symbol raises a 404 meant for paths; in a request body it must be a 422.
        try:
            return normalize_symbol(value, get_settings().supported_symbols)
        except HTTPException as exc:
            raise ValueError(f"Unsupported symbol: {value}") from exc


class AlertUpdate(BaseModel):
    is_active: bool | None = Field(
        default=None,
        description="Turn the alert on or off; turning it on again clears triggered_at.",
        examples=[True],
    )
    target_price: TargetPrice | None = None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Alert id.", examples=[7])
    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    target_price: Decimal = Field(description="Target price in EUR.", examples=["75000.00"])
    condition: AlertCondition = Field(description=CONDITION_DESCRIPTION, examples=["above"])
    is_active: bool = Field(
        description="False once the alert fired or was turned off.", examples=[True]
    )
    created_at: datetime = Field(
        description="When the alert was created.", examples=["2026-10-21T14:05:12Z"]
    )
    triggered_at: datetime | None = Field(
        description="When the alert fired; null if it has not.", examples=[None]
    )
    triggered_price: Decimal | None = Field(
        description="Price that fired the alert; null if it has not.", examples=[None]
    )

    # mypy rejects decorators above @property; Pydantic documents this ignore as the fix.
    @computed_field(description="Pair for display.", examples=["BTC/EUR"])  # type: ignore[prop-decorator]
    @property
    def display_symbol(self) -> str:
        return to_display_symbol(self.symbol)
