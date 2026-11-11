"""FastAPI dependencies of the billing api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.billing.application.facade import BillingUseCases


def get_billing(request: Request) -> BillingUseCases:
    """The billing use cases, provided by the `Container` stored on the app."""
    return cast(BillingUseCases, request.app.state.container)


BillingDep = Annotated[BillingUseCases, Depends(get_billing)]
