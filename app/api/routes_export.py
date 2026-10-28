import csv
import io
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_market_service
from app.api.rate_limit import export_limiter
from app.database import get_session
from app.models import Candle, Note, PriceAlert
from app.schemas.error import RATE_LIMITED, validation_or_error
from app.services.candle_service import STORED_INTERVAL
from app.services.exchange_client import ms_to_datetime
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/export", tags=["export"], dependencies=[Depends(export_limiter)])

Session = Annotated[AsyncSession, Depends(get_session)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]

MAX_ROWS = 100_000
MAX_PERIOD_MS = 90 * 24 * 60 * 60 * 1000
# 9999-12-31T23:59:59.999Z; anything larger would overflow datetime and end in a 500.
MAX_TIME_MS = 253_402_300_799_999
# Rows are written to the response in chunks of this size, so a large export never sits
# in memory as a whole.
CHUNK_ROWS = 1000
# A spreadsheet runs a cell that starts with one of these as a formula (CSV injection).
FORMULA_PREFIXES = ("=", "+", "-", "@")

CANDLE_COLUMNS = ("open_time_utc", "open", "high", "low", "close", "volume")
NOTE_COLUMNS = ("time_utc", "symbol", "text", "created_at")
ALERT_COLUMNS = (
    "symbol",
    "condition",
    "target_price",
    "is_active",
    "created_at",
    "triggered_at",
    "triggered_price",
)

TimeMs = Annotated[int, Query(ge=0, le=MAX_TIME_MS, description="UTC milliseconds, inclusive.")]
OptionalTimeMs = Annotated[
    int | None, Query(ge=0, le=MAX_TIME_MS, description="UTC milliseconds, inclusive.")
]

CSV_FILE = {"content": {"text/csv": {}}, "description": "CSV file with a header row."}
RANGE_ERRORS = "from_ms > to_ms, a period longer than 90 days, or more than 100000 rows"
RATE_LIMIT_NOTE = "Limited to 10 requests per minute."


def today_utc() -> str:
    return datetime.now(UTC).date().isoformat()


def iso_utc(moment: datetime | None) -> str:
    if moment is None:
        return ""
    # SQLite returns DateTime columns without a time zone; every stored value is UTC.
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def decimal_text(value: Decimal | None) -> str:
    # "f" keeps every stored digit and never switches to exponent form ("0E-8", "1E+5").
    return "" if value is None else format(value, "f")


def safe_text(text: str) -> str:
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def check_symbol(raw: str, allowed: Iterable[str]) -> str:
    # Same normalization as normalize_symbol, but a bad query value here is a 422, not a 404.
    symbol = raw.strip().upper().replace("-", "").replace("/", "")
    if symbol not in allowed:
        raise HTTPException(status_code=422, detail=f"Nepodržan par: {raw}")
    return symbol


def check_range(from_ms: int | None, to_ms: int | None) -> None:
    if from_ms is None or to_ms is None:
        return
    if from_ms > to_ms:
        raise HTTPException(status_code=422, detail="from_ms mora biti manje ili jednako to_ms")
    if to_ms - from_ms > MAX_PERIOD_MS:
        raise HTTPException(status_code=422, detail="Period može biti najviše 90 dana")


async def check_row_count(session: AsyncSession, query: Select[Any]) -> None:
    # Counted before streaming: once the first chunk is sent, the status can no longer change.
    count = await session.scalar(select(func.count()).select_from(query.subquery()))
    if count is not None and count > MAX_ROWS:
        raise HTTPException(
            status_code=422,
            detail=f"Najviše {MAX_ROWS} redova po izvozu ({count} pronađeno); suzi period",
        )


def csv_response(
    session: AsyncSession,
    query: Select[Any],
    columns: tuple[str, ...],
    to_row: Callable[[Any], list[str]],
    filename: str,
) -> StreamingResponse:
    async def generate() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        rows = await session.stream_scalars(query.execution_options(yield_per=CHUNK_ROWS))
        async for partition in rows.partitions(CHUNK_ROWS):
            writer.writerows(to_row(item) for item in partition)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        # Only the header is left when there are no rows.
        if buffer.tell():
            yield buffer.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def candle_row(candle: Candle) -> list[str]:
    return [
        iso_utc(ms_to_datetime(candle.open_time)),
        *(
            decimal_text(price)
            for price in (
                candle.open_price,
                candle.high_price,
                candle.low_price,
                candle.close_price,
                candle.volume,
            )
        ),
    ]


def note_row(note: Note) -> list[str]:
    return [
        iso_utc(ms_to_datetime(note.time_ms)),
        note.symbol,
        safe_text(note.text),
        iso_utc(note.created_at),
    ]


def alert_row(alert: PriceAlert) -> list[str]:
    return [
        alert.symbol,
        alert.condition,
        decimal_text(alert.target_price),
        str(alert.is_active).lower(),
        iso_utc(alert.created_at),
        iso_utc(alert.triggered_at),
        decimal_text(alert.triggered_price),
    ]


@router.get(
    "/candles.csv",
    response_class=StreamingResponse,
    summary="Export stored candles as CSV",
    description=(
        "Stored 1m candles of one pair with from_ms <= open time <= to_ms, oldest first. "
        f"Columns: {', '.join(CANDLE_COLUMNS)}. Prices are exact decimal strings. "
        f"At most 90 days and {MAX_ROWS} rows per request. {RATE_LIMIT_NOTE}"
    ),
    responses={
        200: CSV_FILE,
        422: validation_or_error(
            f"Unsupported symbol, an interval other than 1m (not stored), {RANGE_ERRORS}."
        ),
        429: RATE_LIMITED,
    },
)
async def export_candles(
    session: Session,
    service: MarketService,
    symbol: str,
    from_ms: TimeMs,
    to_ms: TimeMs,
    interval: str = STORED_INTERVAL,
) -> StreamingResponse:
    normalized = check_symbol(symbol, service.settings.supported_symbols)
    if interval != STORED_INTERVAL:
        raise HTTPException(
            status_code=422,
            detail=f"Interval {interval} se ne čuva u bazi; izvoz podržava samo {STORED_INTERVAL}",
        )
    check_range(from_ms, to_ms)

    query = (
        select(Candle)
        .where(
            Candle.symbol == normalized,
            Candle.interval == interval,
            Candle.open_time >= from_ms,
            Candle.open_time <= to_ms,
        )
        .order_by(Candle.open_time)
    )
    await check_row_count(session, query)
    filename = f"changewatch_{normalized}_{interval}_{today_utc()}.csv"
    return csv_response(session, query, CANDLE_COLUMNS, candle_row, filename)


@router.get(
    "/notes.csv",
    response_class=StreamingResponse,
    summary="Export chart notes as CSV",
    description=(
        "Chart notes, sorted by time; symbol, from_ms and to_ms are optional filters "
        f"(inclusive). Columns: {', '.join(NOTE_COLUMNS)}. Text starting with =, +, - or @ "
        f"gets a leading apostrophe so spreadsheets do not run it. {RATE_LIMIT_NOTE}"
    ),
    responses={
        200: CSV_FILE,
        422: validation_or_error(f"Unsupported symbol, a time out of range, {RANGE_ERRORS}."),
        429: RATE_LIMITED,
    },
)
async def export_notes(
    session: Session,
    service: MarketService,
    symbol: str | None = None,
    from_ms: OptionalTimeMs = None,
    to_ms: OptionalTimeMs = None,
) -> StreamingResponse:
    normalized = (
        None if symbol is None else check_symbol(symbol, service.settings.supported_symbols)
    )
    check_range(from_ms, to_ms)

    query = select(Note)
    if normalized is not None:
        query = query.where(Note.symbol == normalized)
    if from_ms is not None:
        query = query.where(Note.time_ms >= from_ms)
    if to_ms is not None:
        query = query.where(Note.time_ms <= to_ms)
    query = query.order_by(Note.time_ms, Note.id)
    await check_row_count(session, query)
    name = "notes" if normalized is None else f"notes_{normalized}"
    filename = f"changewatch_{name}_{today_utc()}.csv"
    return csv_response(session, query, NOTE_COLUMNS, note_row, filename)


@router.get(
    "/alerts.csv",
    response_class=StreamingResponse,
    summary="Export price alerts as CSV",
    description=(
        f"Every price alert, oldest first. Columns: {', '.join(ALERT_COLUMNS)}. "
        f"triggered_at and triggered_price are empty for alerts that have not fired. "
        f"{RATE_LIMIT_NOTE}"
    ),
    responses={200: CSV_FILE, 429: RATE_LIMITED},
)
async def export_alerts(session: Session) -> StreamingResponse:
    query = select(PriceAlert).order_by(PriceAlert.id)
    filename = f"changewatch_alerts_{today_utc()}.csv"
    return csv_response(session, query, ALERT_COLUMNS, alert_row, filename)
