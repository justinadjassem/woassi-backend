"""FastAPI dependencies of the customers api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.customers.application.facade import CustomersUseCases


def get_customers(request: Request) -> CustomersUseCases:
    """The customers use cases, provided by the `Container` stored on the app."""
    return cast(CustomersUseCases, request.app.state.container)


CustomersDep = Annotated[CustomersUseCases, Depends(get_customers)]
