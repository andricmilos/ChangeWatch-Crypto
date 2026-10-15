from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from app.api.dependencies import normalize_symbol
from app.config import get_settings


class WatchlistCreate(BaseModel):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def symbol_in_allowlist(cls, value: str) -> str:
        # normalize_symbol raises a 404 meant for paths; in a request body it must be a 422.
        try:
            return normalize_symbol(value, get_settings().supported_symbols)
        except HTTPException as exc:
            raise ValueError(f"Unsupported symbol: {value}") from exc


class WatchlistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    created_at: datetime
