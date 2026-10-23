"""Error bodies and reusable `responses={...}` entries for the OpenAPI docs.

They only describe what the routes already return; nothing here changes a response.
"""

from typing import Any

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Body of every error the app raises itself (HTTPException and the global handlers)."""

    detail: str = Field(
        description="Human-readable reason for the error.",
        examples=["Unknown symbol: XRPEUR"],
    )


class ValidationErrorItem(BaseModel):
    loc: list[str | int] = Field(
        description="Where the invalid value is, e.g. the query parameter or body field.",
        examples=[["query", "amount"]],
    )
    msg: str = Field(
        description="What is wrong with the value.",
        examples=["Input should be greater than 0"],
    )
    type: str = Field(description="Pydantic error type.", examples=["greater_than"])


class ValidationErrorResponse(BaseModel):
    """FastAPI's body for request validation errors: a list instead of a string."""

    detail: list[ValidationErrorItem]


def error_response(description: str, example: str) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"example": {"detail": example}}},
    }


def validation_error(description: str) -> dict[str, Any]:
    return {"model": ValidationErrorResponse, "description": description}


def validation_or_error(description: str) -> dict[str, Any]:
    """422 from both request validation (list) and a check in the route (string)."""
    return {"model": ErrorResponse | ValidationErrorResponse, "description": description}


UNKNOWN_SYMBOL = error_response(
    "The pair is not on the list of supported pairs.", "Unknown symbol: XRPEUR"
)
RATE_LIMITED: dict[str, Any] = {
    **error_response(
        "Too many requests from this IP; wait for the number of seconds in Retry-After.",
        "Previše zahteva, pokušaj ponovo kasnije",
    ),
    "headers": {
        "Retry-After": {
            "description": "Seconds until the next request is accepted.",
            "schema": {"type": "integer"},
        }
    },
}
EXCHANGE_UNAVAILABLE = error_response(
    "Binance REST is not reachable right now; try again later.",
    "Berza trenutno nije dostupna",
)
