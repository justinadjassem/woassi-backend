"""Base business errors.

The API maps each family to an HTTP status (see `shared/api/errors.py`), so modules subclass
these and only pick a more specific `code`.
"""


class DomainError(Exception):
    """Business error. `code` is stable: API clients can rely on it."""

    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFound(DomainError):
    """The requested resource does not exist, or is not visible to the caller (404)."""

    code = "not_found"


class InvalidData(DomainError):
    """The input breaks a business rule (422)."""

    code = "invalid_data"


class Conflict(DomainError):
    """The request clashes with the current state of the resource (409)."""

    code = "conflict"


class Unauthorized(DomainError):
    """The caller is not authenticated, or their token is no longer valid (401)."""

    code = "auth.unauthorized"


class Forbidden(DomainError):
    """The caller is authenticated but may not do this (403)."""

    code = "auth.forbidden"


class RateLimited(DomainError):
    """Too many attempts: `retry_after_s` becomes the `Retry-After` header (429)."""

    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class UpstreamError(DomainError):
    """A third party failed: the request was fine, so this is neither 4xx nor a bug."""

    code = "upstream_error"


class ObjectNotFound(NotFound):
    """Object missing from storage."""

    code = "storage.object_not_found"


class IdempotencyInProgress(Conflict):
    """A request with the same `Idempotency-Key` is still running."""

    code = "idempotency.in_progress"


class IdempotencyKeyReused(InvalidData):
    """An `Idempotency-Key` was reused for a different request body."""

    code = "idempotency.key_reused"


class ShopNotFound(NotFound):
    """The shop does not exist, is archived, or belongs to another account."""

    code = "shops.not_found"
