"""Ports of the accounts module: repositories, password hashing, tokens, code sending."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from woassi.modules.accounts.domain.account import Account
from woassi.modules.accounts.domain.refresh_token import RefreshToken
from woassi.modules.accounts.domain.verification_code import CodePurpose, VerificationCode
from woassi.shared.application.ports import UnitOfWork
from woassi.shared.domain.values import PhoneNumber


class AccountRepository(Protocol):
    """Stores accounts."""

    async def add(self, account: Account) -> None:
        """Raise PhoneAlreadyUsed if the phone number is taken."""
        ...

    async def get(self, account_id: UUID) -> Account | None: ...

    async def get_by_phone(self, phone: PhoneNumber) -> Account | None:
        """The account registered with `phone`, if any."""
        ...

    async def save(self, account: Account) -> None:
        """Writes back an account loaded from this repository."""
        ...


class RefreshTokenStore(Protocol):
    """Stores refresh tokens by hash. Revoked tokens stay, for reuse detection."""

    async def add(self, token: RefreshToken) -> None: ...

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        """The token with this SHA-256, whatever its state."""
        ...

    async def consume(self, token_id: UUID, now: datetime) -> bool:
        """Atomically mark the token as consumed. False if something got there first."""
        ...

    async def revoke(self, token_id: UUID, now: datetime) -> None:
        """Revokes one token, i.e. ends one session's next refresh."""
        ...

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        """Revokes every token of a session."""
        ...

    async def revoke_all(self, account_id: UUID, now: datetime) -> None:
        """Revokes every token of an account: every session ends."""
        ...


class VerificationCodeStore(Protocol):
    """Stores verification codes (phone verification, password reset), hashed."""

    async def add(self, code: VerificationCode) -> None: ...

    async def get_latest(self, account_id: UUID, purpose: CodePurpose) -> VerificationCode | None:
        """Lock and return the latest code, so that concurrent attempts are all counted."""
        ...

    async def save(self, code: VerificationCode) -> None:
        """Writes back the attempts count and consumption date of `code`."""
        ...

    async def invalidate(self, account_id: UUID, purpose: CodePurpose, now: datetime) -> None:
        """Marks every pending code of this purpose as consumed, so only a new one works."""
        ...


class AccountsUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the accounts module."""

    @property
    def accounts(self) -> AccountRepository: ...

    @property
    def refresh_tokens(self) -> RefreshTokenStore: ...

    @property
    def verification_codes(self) -> VerificationCodeStore: ...


class PasswordHasher(Protocol):
    """Slow, salted hashing for passwords and verification codes."""

    async def hash(self, password: str) -> str: ...

    async def verify(self, password: str, password_hash: str) -> bool:
        """True if `password` matches `password_hash`; False for a malformed hash too."""
        ...

    async def dummy_verify(self, password: str) -> None:
        """Take as long as `verify`, so that an unknown phone number cannot be timed."""
        ...


class InvalidAccessToken(Exception):
    """The access token is malformed, badly signed, expired, or not an access token."""

    pass


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A signed access token and its lifetime in seconds."""

    token: str = field(repr=False)
    expires_in: int


class TokenService(Protocol):
    """Issues and checks access tokens, and generates refresh tokens."""

    def issue_access_token(self, account_id: UUID) -> AccessToken:
        """Signed, short-lived access token for `account_id`."""
        ...

    def verify_access_token(self, token: str) -> UUID:
        """Return the account id. Raise InvalidAccessToken otherwise."""
        ...

    def new_refresh_token(self) -> str:
        """A new random refresh token, to be returned once and stored hashed."""
        ...

    def hash_refresh_token(self, token: str) -> str:
        """The form a refresh token is stored and looked up in."""
        ...


class VerificationCodeSender(Protocol):
    """Delivers a verification code to a phone number (SMS, WhatsApp...)."""

    async def send(self, phone: PhoneNumber, code: str, purpose: CodePurpose) -> None:
        """Sends `code` to `phone`. Called after the commit that stored its hash."""
        ...
