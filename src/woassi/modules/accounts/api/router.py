"""Authentication (`/auth`) and account (`/me`) routes.

The refresh token is returned in the body and also set as an HttpOnly cookie scoped to
`/auth`:
browsers use the cookie, other clients send the token in the body.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response, status

from woassi.modules.accounts.api.deps import AccountsDep
from woassi.modules.accounts.api.schemas import (
    AccountResponse,
    AccountUpdate,
    LoginRequest,
    PasswordChange,
    PasswordReset,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    VerifyPhoneRequest,
)
from woassi.modules.accounts.application.dto import TokenPair
from woassi.modules.accounts.domain.errors import InvalidRefreshToken
from woassi.shared.api.deps import CurrentAccount, client_ip

COOKIE_NAME = "refresh_token"
COOKIE_PATH = "/auth"

auth_router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(prefix="/me", tags=["account"])

RefreshCookie = Annotated[str | None, Cookie(alias=COOKIE_NAME)]


def _set_refresh_cookie(response: Response, pair: TokenPair) -> None:
    """Stores the refresh token in an HttpOnly, Secure cookie, readable by `/auth` only."""
    response.set_cookie(
        COOKIE_NAME,
        pair.refresh_token,
        max_age=pair.refresh_expires_in,
        path=COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Deletes the refresh token cookie."""
    response.delete_cookie(
        COOKIE_NAME, path=COOKIE_PATH, httponly=True, secure=True, samesite="lax"
    )


@auth_router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, accounts: AccountsDep) -> AccountResponse:
    """Creates an account and sends a code to verify its phone number."""
    account = await accounts.register().execute(**body.model_dump())
    return AccountResponse.from_view(account)


@auth_router.post("/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_phone(body: VerifyPhoneRequest, accounts: AccountsDep) -> None:
    """Marks the phone number as verified with the code sent at registration."""
    await accounts.verify_phone().execute(phone=body.phone, code=body.code)


@auth_router.post("/login")
async def login(
    body: LoginRequest, request: Request, response: Response, accounts: AccountsDep
) -> TokenResponse:
    """Opens a session. Rate limited per IP and per phone, on failures only."""
    pair = await accounts.login().execute(
        phone=body.phone, password=body.password, ip=client_ip(request)
    )
    _set_refresh_cookie(response, pair)
    return TokenResponse.from_pair(pair)


@auth_router.post("/refresh")
async def refresh(
    response: Response,
    accounts: AccountsDep,
    cookie: RefreshCookie = None,
    body: RefreshRequest | None = None,
) -> TokenResponse:
    """Rotates the refresh token (body first, then cookie) and returns a new token pair."""
    token = (body.refresh_token if body else None) or cookie
    if token is None:
        raise InvalidRefreshToken("Invalid refresh token.")
    pair = await accounts.refresh_tokens().execute(token)
    _set_refresh_cookie(response, pair)
    return TokenResponse.from_pair(pair)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    accounts: AccountsDep,
    cookie: RefreshCookie = None,
    body: RefreshRequest | None = None,
) -> None:
    """Ends the current session. Always succeeds, even without a valid token."""
    token = (body.refresh_token if body else None) or cookie
    if token is not None:
        await accounts.logout().execute(token)
    _clear_refresh_cookie(response)


@auth_router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(response: Response, account: CurrentAccount, accounts: AccountsDep) -> None:
    """Ends every session of the current account, on every device."""
    await accounts.logout_everywhere().execute(account.id)
    _clear_refresh_cookie(response)


@auth_router.post("/password/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(body: PasswordResetRequest, accounts: AccountsDep) -> None:
    """Sends a reset code. Answers 202 whether or not the phone is registered."""
    await accounts.request_password_reset().execute(body.phone)


@auth_router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: PasswordReset, accounts: AccountsDep) -> None:
    """Sets a new password with a reset code, and ends every session."""
    await accounts.reset_password().execute(
        phone=body.phone, code=body.code, new_password=body.new_password
    )


@me_router.get("")
async def get_me(account: CurrentAccount, accounts: AccountsDep) -> AccountResponse:
    """The current account."""
    return AccountResponse.from_view(await accounts.get_my_account().execute(account.id))


@me_router.patch("")
async def update_me(
    body: AccountUpdate, account: CurrentAccount, accounts: AccountsDep
) -> AccountResponse:
    """Updates the name and/or email. Fields left out are unchanged; `email: null` removes it."""
    updated = await accounts.update_my_account().execute(
        account_id=account.id, **body.model_dump(exclude_unset=True)
    )
    return AccountResponse.from_view(updated)


@me_router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange, response: Response, account: CurrentAccount, accounts: AccountsDep
) -> None:
    """Changes the password, knowing the current one, and ends every session."""
    await accounts.change_password().execute(
        account_id=account.id,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    _clear_refresh_cookie(response)
