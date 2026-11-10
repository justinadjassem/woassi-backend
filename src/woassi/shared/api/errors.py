"""Turns domain errors into JSON responses: `{"code": ..., "message": ...}`."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from woassi.shared.api.idempotency import IdempotentReplay, replay_response
from woassi.shared.domain.errors import (
    Conflict,
    DomainError,
    Forbidden,
    InvalidData,
    NotFound,
    RateLimited,
    Unauthorized,
    UpstreamError,
)

HTTP_STATUSES: tuple[tuple[type[DomainError], int], ...] = (
    (NotFound, status.HTTP_404_NOT_FOUND),
    (InvalidData, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (Conflict, status.HTTP_409_CONFLICT),
    (Unauthorized, status.HTTP_401_UNAUTHORIZED),
    (Forbidden, status.HTTP_403_FORBIDDEN),
    (RateLimited, status.HTTP_429_TOO_MANY_REQUESTS),
    (UpstreamError, status.HTTP_502_BAD_GATEWAY),
)


def http_status_for(error: DomainError) -> int:
    """HTTP status of `error`, from the first matching family in `HTTP_STATUSES`.

    Defaults to 400 for a business error outside every family.
    """
    for error_type, http_status in HTTP_STATUSES:
        if isinstance(error, error_type):
            return http_status
    return status.HTTP_400_BAD_REQUEST


def error_response(error: DomainError, http_status: int) -> JSONResponse:
    """JSON body of an error: its stable `code` and its developer message."""
    return JSONResponse(
        status_code=http_status, content={"code": error.code, "message": error.message}
    )


async def _handle_domain_error(_: Request, error: Exception) -> JSONResponse:
    """Exception handler for every `DomainError`, with the headers some statuses need."""
    assert isinstance(error, DomainError)
    response = error_response(error, http_status_for(error))
    if isinstance(error, Unauthorized):
        response.headers["WWW-Authenticate"] = "Bearer"
    if isinstance(error, RateLimited):
        response.headers["Retry-After"] = str(error.retry_after_s)
    return response


def register_error_handlers(app: FastAPI) -> None:
    """Installs the error handlers on `app`."""
    app.add_exception_handler(DomainError, _handle_domain_error)
    app.add_exception_handler(IdempotentReplay, replay_response)
