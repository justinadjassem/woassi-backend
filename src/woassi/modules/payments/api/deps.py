"""FastAPI dependencies of the payments api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.payments.application.facade import PaymentsUseCases


def get_payments(request: Request) -> PaymentsUseCases:
    """The payments use cases, provided by the `Container` stored on the app."""
    return cast(PaymentsUseCases, request.app.state.container)


PaymentsDep = Annotated[PaymentsUseCases, Depends(get_payments)]
