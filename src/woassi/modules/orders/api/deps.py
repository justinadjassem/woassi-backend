"""FastAPI dependencies of the orders api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.orders.application.facade import OrdersUseCases


def get_orders(request: Request) -> OrdersUseCases:
    """The orders use cases, provided by the `Container` stored on the app."""
    return cast(OrdersUseCases, request.app.state.container)


OrdersDep = Annotated[OrdersUseCases, Depends(get_orders)]
