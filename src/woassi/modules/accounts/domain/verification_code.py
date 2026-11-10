"""Six-digit codes sent by phone, to verify a number or reset a password."""

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5


def generate_code() -> str:
    """Random six-digit code, from a cryptographically secure source."""
    return f"{secrets.randbelow(1_000_000):06d}"


class CodePurpose(StrEnum):
    """What a code is for: a code of one purpose cannot be used for the other."""

    PHONE_VERIFICATION = "PHONE_VERIFICATION"
    PASSWORD_RESET = "PASSWORD_RESET"


@dataclass(slots=True)
class VerificationCode:
    """A code sent to an account's phone, stored hashed. Valid 10 minutes, 5 attempts."""

    id: UUID
    account_id: UUID
    purpose: CodePurpose
    code_hash: str = field(repr=False)
    expires_at: datetime
    created_at: datetime
    attempts: int = 0
    consumed_at: datetime | None = None

    @classmethod
    def issue(
        cls, *, account_id: UUID, purpose: CodePurpose, code_hash: str, now: datetime
    ) -> "VerificationCode":
        """A new code, valid for `CODE_TTL` from `now`."""
        return cls(
            id=uuid4(),
            account_id=account_id,
            purpose=purpose,
            code_hash=code_hash,
            expires_at=now + CODE_TTL,
            created_at=now,
        )

    def is_usable(self, now: datetime) -> bool:
        """True if not consumed, not expired, and with attempts left."""
        return self.consumed_at is None and self.attempts < MAX_ATTEMPTS and self.expires_at > now

    def record_failure(self) -> None:
        """Counts one wrong attempt."""
        self.attempts += 1

    def consume(self, now: datetime) -> None:
        """Marks the code as used: it cannot be used again."""
        self.consumed_at = now
