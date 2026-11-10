"""Request and response bodies of the accounts api."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from woassi.modules.accounts.application.dto import AccountView, TokenPair
from woassi.modules.accounts.domain.account import AccountStatus

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
Phone = Field(min_length=1, max_length=30)
# Length rules live in the domain; this only bounds what reaches the password hasher.
Password = Field(max_length=1024)


class RegisterRequest(BaseModel):
    """Body of `POST /auth/register`."""

    full_name: str = Field(min_length=1, max_length=200)
    phone: str = Phone
    password: str = Password
    email: str | None = Field(default=None, max_length=254, pattern=EMAIL_PATTERN)


class VerifyPhoneRequest(BaseModel):
    """Body of `POST /auth/verify`."""

    phone: str = Phone
    code: str = Field(max_length=6)


class LoginRequest(BaseModel):
    """Body of `POST /auth/login`."""

    phone: str = Phone
    password: str = Password


class RefreshRequest(BaseModel):
    """Optional for browsers, which send the cookie instead."""

    refresh_token: str | None = Field(default=None, max_length=128)


class PasswordResetRequest(BaseModel):
    """Body of `POST /auth/password/request`."""

    phone: str = Phone


class PasswordReset(BaseModel):
    """Body of `POST /auth/password/reset`."""

    phone: str = Phone
    code: str = Field(max_length=6)
    new_password: str = Password


class PasswordChange(BaseModel):
    """Body of `POST /me/password`."""

    current_password: str = Password
    new_password: str = Password


class AccountUpdate(BaseModel):
    """Body of `PATCH /me`: only the fields sent are changed."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254, pattern=EMAIL_PATTERN)


class AccountResponse(BaseModel):
    """An account, as returned by the API. Never includes the password hash."""

    id: UUID
    full_name: str
    phone: str
    email: str | None
    status: AccountStatus
    phone_verified: bool
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def from_view(cls, view: AccountView) -> "AccountResponse":
        """Builds the response from an `AccountView`."""
        return cls(
            id=view.id,
            full_name=view.full_name,
            phone=view.phone,
            email=view.email,
            status=view.status,
            phone_verified=view.phone_verified,
            created_at=view.created_at,
            last_login_at=view.last_login_at,
        )


class TokenResponse(BaseModel):
    """Token pair; `expires_in` is the access token lifetime in seconds."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str

    @classmethod
    def from_pair(cls, pair: TokenPair) -> "TokenResponse":
        """Builds the response from a `TokenPair`."""
        return cls(
            access_token=pair.access_token,
            expires_in=pair.expires_in,
            refresh_token=pair.refresh_token,
        )
