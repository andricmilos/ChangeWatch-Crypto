from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_NAME_LENGTH = 20
MAX_PAIR_NOTE_LENGTH = 500

TagColor = Literal["green", "red", "yellow", "blue", "purple", "orange", "teal", "gray"]


def check_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("Name must not be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"Name must be at most {MAX_NAME_LENGTH} characters")
    return name


def check_pair_note_text(value: str) -> str:
    text = value.strip()
    if len(text) > MAX_PAIR_NOTE_LENGTH:
        raise ValueError(f"Text must be at most {MAX_PAIR_NOTE_LENGTH} characters")
    return text


def as_utc(value: datetime) -> datetime:
    # Stored times are UTC, but SQLite gives them back without a zone. Without this the
    # same tag would be sent with "Z" right after a write and without it on a later read.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]


# The length is checked after stripping, so max_length is not set on the Field itself.
TagName = Annotated[
    str,
    AfterValidator(check_name),
    Field(
        description=(f"1-{MAX_NAME_LENGTH} characters after trimming; unique regardless of case."),
        examples=["Dugoročno"],
    ),
]
TagColorField = Annotated[
    TagColor, Field(description="One of the fixed tag colors.", examples=["green"])
]


class TagCreate(BaseModel):
    name: TagName
    color: TagColorField


class TagUpdate(BaseModel):
    name: TagName | None = None
    color: TagColorField | None = None

    @model_validator(mode="after")
    def something_to_change(self) -> Self:
        if self.name is None and self.color is None:
            raise ValueError("Give name, color or both")
        return self


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Tag id.", examples=[1])
    name: str = Field(description="Tag name.", examples=["Dugoročno"])
    color: TagColor = Field(description="Tag color.", examples=["green"])
    created_at: UtcDatetime = Field(
        description="When the tag was created.", examples=["2026-10-31T14:05:12Z"]
    )


class TagWithCount(TagOut):
    pair_count: int = Field(description="How many pairs have this tag.", examples=[2])


class PairTagsUpdate(BaseModel):
    tag_ids: list[int] = Field(
        description="The pair's new set of tags; duplicates are ignored, [] removes all.",
        examples=[[1, 3]],
    )


class PairNoteUpdate(BaseModel):
    text: Annotated[
        str,
        AfterValidator(check_pair_note_text),
        Field(
            description=(
                f"Plain text, at most {MAX_PAIR_NOTE_LENGTH} characters after trimming; "
                "empty text deletes the note."
            ),
            examples=["Holding until the next halving"],
        ),
    ]


class PairNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    text: str = Field(description="Text of the note.", examples=["Holding until the halving"])
    updated_at: UtcDatetime = Field(
        description="When the note was last changed.", examples=["2026-10-31T14:05:12Z"]
    )


class PairMeta(BaseModel):
    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    tags: list[TagOut] = Field(description="Tags of the pair, sorted by name.")
    note: PairNoteOut | None = Field(description="Note about the pair, or null.")
