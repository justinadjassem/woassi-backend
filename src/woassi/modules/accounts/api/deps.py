"""FastAPI dependencies of the accounts api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.accounts.application.facade import AccountsUseCases


def get_accounts(request: Request) -> AccountsUseCases:
    """The accounts use cases, provided by the `Container` stored on the app."""
    return cast(AccountsUseCases, request.app.state.container)


AccountsDep = Annotated[AccountsUseCases, Depends(get_accounts)]
