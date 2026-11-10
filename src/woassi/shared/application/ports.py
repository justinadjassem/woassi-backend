"""Ports of the shared foundation: storage, time, events, secrets, audit, idempotency,
authentication and rate limiting. Adapters live in `shared/infrastructure`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from woassi.shared.domain.audit import AuditEntry
from woassi.shared.domain.events import DomainEvent
from woassi.shared.domain.values import PhoneNumber


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Metadata of a stored object."""

    key: str
    size: int
    content_type: str | None


class StoragePort(Protocol):
    """Object storage. One instance maps to one bucket."""

    async def presigned_put(self, key: str, content_type: str, expire_s: int = 600) -> str:
        """URL a client can upload `key` to directly, valid for `expire_s` seconds."""
        ...

    async def presigned_get(self, key: str, expire_s: int = 3600) -> str:
        """Temporary download URL of `key`, valid for `expire_s` seconds."""
        ...

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Stores `data` under `key`, replacing any existing object."""
        ...

    async def get(self, key: str) -> bytes:
        """Raise ObjectNotFound if the object does not exist."""
        ...

    async def stat(self, key: str) -> ObjectInfo | None:
        """Metadata of `key`, or None if the object does not exist."""
        ...

    async def delete(self, key: str) -> None:
        """Deletes `key`. Deleting a missing object is not an error."""
        ...

    def public_url(self, key: str) -> str:
        """Raise an error if the bucket is not public."""
        ...


class Clock(Protocol):
    """Source of the current time. Inject it instead of calling `datetime.now()`."""

    def now(self) -> datetime:
        """Current time in UTC. Always store this one."""
        ...

    def local_now(self) -> datetime:
        """Current time in Africa/Lome. For display only."""
        ...


class EventBus(Protocol):
    """Delivers domain events to their subscribers."""

    async def publish(self, event: DomainEvent) -> None:
        """Runs the immediate subscribers of `event` and queues the deferred ones."""
        ...


class SecretDecryptionError(Exception):
    """A secret cannot be decrypted: wrong key or corrupted value. Not a client error."""


class SecretCipher(Protocol):
    """Encrypts secrets (Meta tokens, aggregator credentials) before they are stored."""

    def encrypt(self, plaintext: str) -> str:
        """Returns an encrypted, storable form of `plaintext`."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Raise SecretDecryptionError if `ciphertext` cannot be decrypted."""
        ...


class PhoneNormalizer(Protocol):
    """Parses phone numbers typed by users, with Togo as the default country."""

    def normalize(self, raw: str) -> PhoneNumber:
        """Raise InvalidData if `raw` is not a valid phone number."""
        ...


class AuditLog(Protocol):
    """Append-only. Writes join the transaction of the action they describe."""

    async def record(
        self,
        *,
        account_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        shop_id: UUID | None = None,
        details: Mapping[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        """Adds an entry. Call it before `uow.commit()` so it shares the action's transaction."""
        ...

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[AuditEntry]:
        """Entries of `shop_id`, newest first."""
        ...

    async def count(self, shop_id: UUID) -> int:
        """Number of entries of `shop_id`."""
        ...


class UnitOfWork(Protocol):
    """One transaction. Events recorded by saved entities are published after the commit."""

    @property
    def audit(self) -> AuditLog:
        """The audit log, writing in this transaction."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None:
        """Commits the transaction, then publishes the events of the saved entities."""
        ...

    async def rollback(self) -> None:
        """Rolls the transaction back and drops the pending events."""
        ...


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """HTTP response kept to be replayed for a repeated `Idempotency-Key`."""

    status: int
    content_type: str | None
    body: bytes


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """State of an `Idempotency-Key`: the request it was used for, and its response."""

    fingerprint: str
    response: StoredResponse | None  # None while the first request is still running


class IdempotencyStore(Protocol):
    """Remembers responses by `Idempotency-Key`, so a retried request is not run twice."""

    async def begin(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        """Reserve `key`. Return None if it was free, otherwise the existing record."""
        ...

    async def complete(self, key: str, fingerprint: str, response: StoredResponse) -> None:
        """Stores the response of the request that reserved `key`."""
        ...

    async def release(self, key: str) -> None:
        """Frees `key` after a failure, so the client can retry."""
        ...


@dataclass(frozen=True, slots=True)
class AuthenticatedAccount:
    """The account behind an access token."""

    id: UUID


class AccountAuthenticator(Protocol):
    """Checks access tokens. Declared here because `shared` cannot import `accounts`;
    implemented in `woassi/integration`.
    """

    async def authenticate(self, access_token: str) -> AuthenticatedAccount:
        """Raise Unauthorized if the token is invalid or the account cannot be used."""
        ...


class ShopOwnershipPort(Protocol):
    """Tells whether an account owns a shop. Declared here because `shared` cannot import
    `shops`; implemented in `woassi/integration`.
    """

    async def owns(self, account_id: UUID, shop_id: UUID) -> bool:
        """True if `shop_id` exists, is not archived, and belongs to `account_id`."""
        ...


class RateLimiter(Protocol):
    """Counts attempts per key over a fixed window."""

    async def hit(self, key: str, limit: int, window_s: int) -> int | None:
        """Count one attempt. Return None if allowed, else the seconds left in the window."""
        ...

    async def forgive(self, key: str) -> None:
        """Take back one attempt, for instance when it turned out to be legitimate."""
        ...
