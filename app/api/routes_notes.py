from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_connection_manager, get_market_service, normalize_symbol
from app.api.rate_limit import write_limiter
from app.database import get_session
from app.models import Note
from app.schemas.error import (
    RATE_LIMITED,
    UNKNOWN_SYMBOL,
    error_response,
    validation_error,
    validation_or_error,
)
from app.schemas.note import NoteCreate, NoteOut, NotesConfig, NoteUpdate
from app.services.market_data_service import MarketDataService
from app.websocket.manager import ConnectionManager

router = APIRouter(prefix="/api/notes", tags=["notes"])

Session = Annotated[AsyncSession, Depends(get_session)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]

MAX_NOTES = 500
TOO_MANY_NOTES = f"Najviše {MAX_NOTES} beleški; obriši neku pre dodavanja nove"
WRITE_DISABLED = "Izmena beleški je isključena na ovom serveru"
NOTE_NOT_FOUND = error_response("No note with this id.", "Beleška ne postoji")
WRITE_FORBIDDEN = error_response("NOTES_WRITE_ENABLED=false on this server.", WRITE_DISABLED)


def require_write_enabled(service: MarketService) -> None:
    if not service.settings.notes_write_enabled:
        raise HTTPException(status_code=403, detail=WRITE_DISABLED)


# The 403 check runs first, so requests to a read-only server do not use up the rate limit.
WriteGuards = [Depends(require_write_enabled), Depends(write_limiter)]


async def broadcast_note(manager: ConnectionManager, message_type: str, note: Note) -> None:
    data = NoteOut.model_validate(note).model_dump(mode="json")
    await manager.broadcast({"type": message_type, "data": data})


async def find_note(session: AsyncSession, note_id: int) -> Note:
    note = await session.get(Note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Beleška ne postoji")
    return note


@router.get(
    "",
    response_model=list[NoteOut],
    summary="List notes of a pair",
    description=(
        "Notes of one pair, sorted by time_ms (oldest first). from_ms and to_ms are "
        "optional and inclusive."
    ),
    responses={
        404: UNKNOWN_SYMBOL,
        422: validation_or_error("Missing symbol, non-integer time, or from_ms > to_ms."),
    },
)
async def list_notes(
    session: Session,
    service: MarketService,
    symbol: str,
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> list[Note]:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise HTTPException(status_code=422, detail="from_ms mora biti manje ili jednako to_ms")

    query = select(Note).where(Note.symbol == normalized)
    if from_ms is not None:
        query = query.where(Note.time_ms >= from_ms)
    if to_ms is not None:
        query = query.where(Note.time_ms <= to_ms)
    # id breaks ties, so notes at the same moment keep the order they were added in.
    result = await session.scalars(query.order_by(Note.time_ms, Note.id))
    return list(result)


@router.get(
    "/config",
    response_model=NotesConfig,
    summary="Whether notes can be changed",
    description="Lets the UI hide the add, edit and delete buttons on a read-only server.",
)
async def notes_config(service: MarketService) -> NotesConfig:
    return NotesConfig(write_enabled=service.settings.notes_write_enabled)


@router.post(
    "",
    response_model=NoteOut,
    status_code=201,
    dependencies=WriteGuards,
    summary="Create a note",
    description=(
        f"Adds a note to a moment on a pair's chart. At most {MAX_NOTES} notes in total. "
        "Every browser receives a note_created message."
    ),
    responses={
        400: error_response(f"Already {MAX_NOTES} notes.", TOO_MANY_NOTES),
        403: WRITE_FORBIDDEN,
        422: validation_error(
            "Unsupported symbol, empty or too long text, HTML in the text, or time_ms in the "
            "future or older than 1 year."
        ),
        429: RATE_LIMITED,
    },
)
async def create_note(payload: NoteCreate, session: Session, manager: Manager) -> Note:
    count = await session.scalar(select(func.count()).select_from(Note))
    if count is not None and count >= MAX_NOTES:
        raise HTTPException(status_code=400, detail=TOO_MANY_NOTES)
    note = Note(symbol=payload.symbol, time_ms=payload.time_ms, text=payload.text)
    session.add(note)
    await session.commit()
    await broadcast_note(manager, "note_created", note)
    return note


@router.patch(
    "/{note_id}",
    response_model=NoteOut,
    dependencies=WriteGuards,
    summary="Change the text of a note",
    description="Only the text can change. Every browser receives a note_updated message.",
    responses={
        403: WRITE_FORBIDDEN,
        404: NOTE_NOT_FOUND,
        422: validation_error("Non-integer id, or empty, too long or HTML text."),
        429: RATE_LIMITED,
    },
)
async def update_note(
    note_id: int, payload: NoteUpdate, session: Session, manager: Manager
) -> Note:
    note = await find_note(session, note_id)
    note.text = payload.text
    await session.commit()
    await broadcast_note(manager, "note_updated", note)
    return note


@router.delete(
    "/{note_id}",
    status_code=204,
    dependencies=WriteGuards,
    summary="Delete a note",
    description=(
        "Deletes the note; the response has no body. Every browser receives a note_deleted message."
    ),
    responses={
        403: WRITE_FORBIDDEN,
        404: NOTE_NOT_FOUND,
        422: validation_error("Non-integer id."),
        429: RATE_LIMITED,
    },
)
async def delete_note(note_id: int, session: Session, manager: Manager) -> Response:
    note = await find_note(session, note_id)
    await session.delete(note)
    await session.commit()
    # id and symbol are enough for the browser to remove the marker from the right chart.
    await manager.broadcast(
        {"type": "note_deleted", "data": {"id": note_id, "symbol": note.symbol}}
    )
    return Response(status_code=204)
