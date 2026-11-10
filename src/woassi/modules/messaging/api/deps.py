"""FastAPI dependencies of the messaging api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.messaging.application.facade import MessagingUseCases


def get_messaging(request: Request) -> MessagingUseCases:
    """The messaging use cases, provided by the `Container` stored on the app."""
    return cast(MessagingUseCases, request.app.state.container)


MessagingDep = Annotated[MessagingUseCases, Depends(get_messaging)]
