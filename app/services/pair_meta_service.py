"""User tags and short notes per pair (SPEC 16.6). Shared by every visitor, like chart notes."""

from collections.abc import Collection, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PairNote, PairTag, Tag
from app.schemas.tag import PairMeta, PairNoteOut, TagCreate, TagOut, TagUpdate, TagWithCount

MAX_TAGS = 20
MAX_TAGS_PER_PAIR = 5


class DuplicateTagNameError(Exception):
    pass


class TagLimitError(Exception):
    pass


class PairTagLimitError(Exception):
    pass


class UnknownTagError(Exception):
    def __init__(self, tag_ids: Collection[int]) -> None:
        super().__init__(f"Unknown tag ids: {sorted(tag_ids)}")
        self.tag_ids = sorted(tag_ids)


async def list_tags(session: AsyncSession) -> list[TagWithCount]:
    pair_count = func.count(PairTag.symbol)
    rows = await session.execute(
        select(Tag, pair_count)
        .outerjoin(PairTag, PairTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(Tag.name_key, Tag.id)
    )
    return [
        TagWithCount(**TagOut.model_validate(tag).model_dump(), pair_count=count)
        for tag, count in rows
    ]


async def _name_taken(session: AsyncSession, name: str, except_id: int | None = None) -> bool:
    query = select(Tag.id).where(Tag.name_key == name.casefold())
    if except_id is not None:
        query = query.where(Tag.id != except_id)
    return await session.scalar(query) is not None


async def _commit_tag(session: AsyncSession) -> None:
    # The pre-check covers the normal case; this catches two requests racing for one name.
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateTagNameError from exc


async def create_tag(session: AsyncSession, payload: TagCreate) -> Tag:
    count = await session.scalar(select(func.count()).select_from(Tag))
    if count is not None and count >= MAX_TAGS:
        raise TagLimitError
    if await _name_taken(session, payload.name):
        raise DuplicateTagNameError
    tag = Tag(name=payload.name, name_key=payload.name.casefold(), color=payload.color)
    session.add(tag)
    await _commit_tag(session)
    return tag


async def update_tag(session: AsyncSession, tag_id: int, payload: TagUpdate) -> Tag | None:
    tag = await session.get(Tag, tag_id)
    if tag is None:
        return None
    if payload.name is not None:
        if await _name_taken(session, payload.name, except_id=tag_id):
            raise DuplicateTagNameError
        tag.name = payload.name
        tag.name_key = payload.name.casefold()
    if payload.color is not None:
        tag.color = payload.color
    await _commit_tag(session)
    return tag


async def delete_tag(session: AsyncSession, tag_id: int) -> list[str] | None:
    """Deletes the tag and its pair links; returns the symbols that had it, None if missing."""
    tag = await session.get(Tag, tag_id)
    if tag is None:
        return None
    symbols = list(
        await session.scalars(
            select(PairTag.symbol).where(PairTag.tag_id == tag_id).order_by(PairTag.symbol)
        )
    )
    # Same transaction as the tag itself: SQLite foreign keys are off, so ON DELETE CASCADE
    # would not run, and a crash in between must not leave links to a missing tag.
    await session.execute(delete(PairTag).where(PairTag.tag_id == tag_id))
    await session.delete(tag)
    await session.commit()
    return symbols


async def set_pair_tags(session: AsyncSession, symbol: str, tag_ids: Sequence[int]) -> None:
    unique_ids = set(tag_ids)
    if len(unique_ids) > MAX_TAGS_PER_PAIR:
        raise PairTagLimitError
    found = set(await session.scalars(select(Tag.id).where(Tag.id.in_(unique_ids))))
    if missing := unique_ids - found:
        raise UnknownTagError(missing)
    await session.execute(delete(PairTag).where(PairTag.symbol == symbol))
    session.add_all(PairTag(symbol=symbol, tag_id=tag_id) for tag_id in unique_ids)
    await session.commit()


async def set_pair_note(session: AsyncSession, symbol: str, text: str) -> None:
    note = await session.get(PairNote, symbol)
    if not text:
        if note is not None:
            await session.delete(note)
    elif note is None:
        session.add(PairNote(symbol=symbol, text=text))
    else:
        note.text = text
    await session.commit()


async def pair_metas(session: AsyncSession, symbols: Sequence[str]) -> list[PairMeta]:
    """Tags and note of each symbol, in the order given."""
    tags: dict[str, list[TagOut]] = {symbol: [] for symbol in symbols}
    rows = await session.execute(
        select(PairTag.symbol, Tag)
        .join(Tag, Tag.id == PairTag.tag_id)
        .where(PairTag.symbol.in_(symbols))
        .order_by(Tag.name_key, Tag.id)
    )
    for symbol, tag in rows:
        tags[symbol].append(TagOut.model_validate(tag))

    notes = {
        note.symbol: PairNoteOut.model_validate(note)
        for note in await session.scalars(select(PairNote).where(PairNote.symbol.in_(symbols)))
    }
    return [
        PairMeta(symbol=symbol, tags=tags[symbol], note=notes.get(symbol)) for symbol in symbols
    ]


async def pair_meta(session: AsyncSession, symbol: str) -> PairMeta:
    return (await pair_metas(session, [symbol]))[0]
