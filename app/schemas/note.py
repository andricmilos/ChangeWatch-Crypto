import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import HTTPException
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.api.dependencies import normalize_symbol
from app.config import get_settings

MAX_TEXT_LENGTH = 280
# Clocks of browser and server are never exactly in sync; "now" in the browser may be
# slightly ahead of the server.
FUTURE_TOLERANCE = timedelta(seconds=60)
MAX_AGE = timedelta(days=365)
# "<b>", "</p>", "<!--": anything that starts like a tag. A lone "<" or ">" stays allowed,
# so notes like "BTC > 70k" still work.
HTML_TAG = re.compile(r"<\s*[a-zA-Z/!?]")


def check_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("Text must not be empty")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"Text must be at most {MAX_TEXT_LENGTH} characters")
    if HTML_TAG.search(text):
        raise ValueError("Text must be plain text, without HTML")
    return text


# The length is checked after stripping, so max_length is not set on the Field itself.
NoteText = Annotated[
    str,
    AfterValidator(check_text),
    Field(
        description=f"Plain text, 1-{MAX_TEXT_LENGTH} characters after trimming; no HTML.",
        examples=["Breakout above the 4h range"],
    ),
]


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class NoteCreate(BaseModel):
    symbol: str = Field(
        description="Supported pair; btceur, BTC-EUR and BTC/EUR are accepted too.",
        examples=["BTCEUR"],
    )
    time_ms: int = Field(
        description=(
            "Moment on the chart the note is about, UTC milliseconds. "
            "At most 60 s in the future and at most 1 year in the past."
        ),
        examples=[1760380800000],
    )
    text: NoteText

    @field_validator("symbol")
    @classmethod
    def symbol_in_allowlist(cls, value: str) -> str:
        # normalize_symbol raises a 404 meant for paths; in a request body it must be a 422.
        try:
            return normalize_symbol(value, get_settings().supported_symbols)
        except HTTPException as exc:
            raise ValueError(f"Unsupported symbol: {value}") from exc

    @field_validator("time_ms")
    @classmethod
    def time_in_range(cls, value: int) -> int:
        now = now_ms()
        if value > now + FUTURE_TOLERANCE // timedelta(milliseconds=1):
            raise ValueError("time_ms must not be in the future")
        if value < now - MAX_AGE // timedelta(milliseconds=1):
            raise ValueError("time_ms must not be older than 1 year")
        return value


class NoteUpdate(BaseModel):
    text: NoteText


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Note id.", examples=[3])
    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    time_ms: int = Field(
        description="Moment on the chart, UTC milliseconds.", examples=[1760380800000]
    )
    text: str = Field(description="Plain text of the note.", examples=["Breakout above range"])
    created_at: datetime = Field(
        description="When the note was created.", examples=["2026-10-21T14:05:12Z"]
    )
    updated_at: datetime = Field(
        description="When the note was last changed.", examples=["2026-10-21T14:05:12Z"]
    )


class NotesConfig(BaseModel):
    write_enabled: bool = Field(
        description="False when NOTES_WRITE_ENABLED=false: notes can be read, not changed.",
        examples=[True],
    )
