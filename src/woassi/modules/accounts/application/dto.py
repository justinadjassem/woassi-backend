"""Data returned by the accounts use cases."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from woassi.modules.accounts.domain.account import Account, AccountStatus


@dataclass(frozen=True, slots=True)
class AccountView:
    """Read-only view of an account, without its password hash."""

    id: UUID
    full_name: str
    phone: str
    email: str | None
    status: AccountStatus
    phone_verified: bool
    created_at: datetime
    last_login_at: datetime | None


def account_view(account: Account) -> AccountView:
    """Builds the view of `account`."""
    return AccountView(
        id=account.id,
        full_name=account.full_name,
        phone=account.phone.value,
        email=account.email,
        status=account.status,
        phone_verified=account.phone_verified,
        created_at=account.created_at,
        last_login_at=account.last_login_at,
    )


@dataclass(frozen=True, slots=True)
class TokenPair:
    """Access token and refresh token of a session, with their lifetimes in seconds.

    The tokens are hidden from `repr` so they never end up in logs.
    """

    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str = field(repr=False)
    refresh_expires_in: int
