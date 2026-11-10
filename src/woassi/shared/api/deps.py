"""FastAPI dependencies shared by every module: authentication and shop access."""

from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from woassi.shared.application.facade import SharedUseCases
from woassi.shared.application.ports import AuthenticatedAccount
from woassi.shared.domain.errors import ShopNotFound, Unauthorized

_bearer = HTTPBearer(auto_error=False)


def get_shared(request: Request) -> SharedUseCases:
    """The shared use cases, provided by the `Container` stored on the app."""
    return cast(SharedUseCases, request.app.state.container)


SharedDep = Annotated[SharedUseCases, Depends(get_shared)]


async def get_current_account(
    shared: SharedDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedAccount:
    """Authenticates the `Authorization: Bearer` access token.

    Raises Unauthorized if the header is missing or the token is not valid.
    """
    if credentials is None:
        raise Unauthorized("Authentication required.")
    return await shared.account_authenticator().authenticate(credentials.credentials)


def client_ip(request: Request) -> str:
    """IP of the direct client, for rate limits and the audit log."""
    return request.client.host if request.client else "unknown"


CurrentAccount = Annotated[AuthenticatedAccount, Depends(get_current_account)]


async def get_authorized_shop(shop_id: UUID, account: CurrentAccount, shared: SharedDep) -> UUID:
    """404 rather than 403: a stranger must not learn that the shop exists."""
    if not await shared.shop_ownership().owns(account.id, shop_id):
        raise ShopNotFound(f"Shop {shop_id} not found.")
    return shop_id
