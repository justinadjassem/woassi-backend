"""Refresh tokens: long-lived, single-use, rotated on every refresh."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RefreshToken:
    """Stored form of a refresh token: only its hash, never the token itself.

    A family is one login session. Every rotation adds a token to it.
    """

    id: UUID
    account_id: UUID
    family_id: UUID
    token_hash: str = field(repr=False)
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        """False for a revoked or expired token. A consumed one is handled as a reuse."""
        if self.revoked_at is not None:
            return False
        return self.consumed_at is not None or self.expires_at > now
