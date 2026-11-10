"""The merchant account: who logs in and owns shops."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from woassi.modules.accounts.domain.errors import AccountSuspended, InvalidAccount, WeakPassword
from woassi.modules.accounts.domain.events import AccountCreated, AccountLoggedIn
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import PhoneNumber

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128  # Argon2 is deliberately slow: do not let clients feed it megabytes.


def validate_password(password: str) -> None:
    """Raises WeakPassword unless the password has 8 to 128 characters."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise WeakPassword(
            f"A password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters long."
        )


def _clean_name(full_name: str) -> str:
    """Strips `full_name`; raises InvalidAccount if nothing is left."""
    full_name = full_name.strip()
    if not full_name:
        raise InvalidAccount("Full name is required.")
    return full_name


class AccountStatus(StrEnum):
    """A suspended account can no longer log in or use its tokens."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


@dataclass(slots=True)
class Account(Entity):
    """A merchant account, identified by its phone number."""

    id: UUID
    full_name: str
    phone: PhoneNumber
    password_hash: str = field(repr=False)
    created_at: datetime
    email: str | None = None
    status: AccountStatus = AccountStatus.ACTIVE
    phone_verified: bool = False
    last_login_at: datetime | None = None

    @classmethod
    def register(
        cls,
        *,
        full_name: str,
        phone: PhoneNumber,
        password_hash: str,
        now: datetime,
        email: str | None = None,
    ) -> "Account":
        """Creates an active, unverified account, and records `AccountCreated`."""
        account = cls(
            id=uuid4(),
            full_name=_clean_name(full_name),
            phone=phone,
            password_hash=password_hash,
            created_at=now,
            email=email,
        )
        account.record_event(AccountCreated(account_id=account.id))
        return account

    @property
    def is_active(self) -> bool:
        """True unless the account is suspended."""
        return self.status is AccountStatus.ACTIVE

    def record_login(self, now: datetime) -> None:
        """Stamps a successful login. Raises AccountSuspended for a suspended account."""
        if not self.is_active:
            raise AccountSuspended("This account is suspended.")
        self.last_login_at = now
        self.record_event(AccountLoggedIn(account_id=self.id))

    def verify_phone(self) -> None:
        """The owner proved they receive codes on this phone number."""
        self.phone_verified = True

    def change_password(self, password_hash: str) -> None:
        """Replaces the password hash. Ending the sessions is the caller's job."""
        self.password_hash = password_hash

    def rename(self, full_name: str) -> None:
        """Changes the full name; raises InvalidAccount if it is blank."""
        self.full_name = _clean_name(full_name)

    def change_email(self, email: str | None) -> None:
        """Sets or removes (None) the email address."""
        self.email = email

    def suspend(self) -> None:
        """Blocks the account: no login, and its access tokens stop working."""
        self.status = AccountStatus.SUSPENDED
