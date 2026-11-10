"""Optional `Idempotency-Key` support for sensitive POST routes.

The `idempotent` dependency reserves the key before the route runs, and
`IdempotencyMiddleware` stores the response once it is sent.
"""

import hashlib
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from woassi.shared.api.deps import SharedDep
from woassi.shared.application.ports import IdempotencyStore, StoredResponse
from woassi.shared.domain.errors import IdempotencyInProgress, IdempotencyKeyReused

REPLAY_HEADER = "Idempotency-Replayed"


class IdempotentReplay(Exception):
    """Short-circuits the route: a dependency cannot return a response by itself."""

    def __init__(self, response: StoredResponse) -> None:
        super().__init__("Idempotent replay")
        self.response = response


async def replay_response(_: Request, error: Exception) -> Response:
    """Sends back the stored response, marked with the `Idempotency-Replayed` header."""
    assert isinstance(error, IdempotentReplay)
    stored = error.response
    return Response(
        content=stored.body,
        status_code=stored.status,
        media_type=stored.content_type,
        headers={REPLAY_HEADER: "true"},
    )


@dataclass(frozen=True, slots=True)
class _Pending:
    """A reserved key, waiting for the response of the request that reserved it."""

    store: IdempotencyStore
    key: str
    fingerprint: str

    async def finish(self, response: StoredResponse) -> None:
        # A server error is not a final answer: the client may retry with the same key.
        """Stores the response, or frees the key after a server error."""
        if response.status >= 500:
            await self.store.release(self.key)
        else:
            await self.store.complete(self.key, self.fingerprint, response)


async def idempotent(
    request: Request,
    shared: SharedDep,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> None:
    """Optional `Idempotency-Key` header: replays the first response for 24 hours.

    Apply with `dependencies=[Depends(idempotent)]`. Needs IdempotencyMiddleware.
    """
    if idempotency_key is None:
        return
    store = shared.idempotency_store()
    key = f"{request.method}:{request.url.path}:{idempotency_key}"
    fingerprint = hashlib.sha256(await request.body()).hexdigest()

    existing = await store.begin(key, fingerprint)
    if existing is None:
        request.state.idempotency = _Pending(store, key, fingerprint)
    elif existing.fingerprint != fingerprint:
        raise IdempotencyKeyReused("This Idempotency-Key was used with a different body.")
    elif existing.response is None:
        raise IdempotencyInProgress("A request with this Idempotency-Key is still running.")
    else:
        raise IdempotentReplay(existing.response)


class IdempotencyMiddleware:
    """Stores the response of requests marked by `idempotent`; passive for all the others."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Captures the response status, content type and body while sending it, then stores
        them if the request reserved an idempotency key.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        # Stays 500 if the app never starts a response.
        status = 500
        content_type: str | None = None
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            """Forwards `message` to the client, keeping a copy of the response."""
            nonlocal status, content_type
            if "idempotency" in state:
                if message["type"] == "http.response.start":
                    status = message["status"]
                    headers = dict(message.get("headers", []))
                    raw_type = headers.get(b"content-type")
                    content_type = raw_type.decode() if raw_type else None
                elif message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self._app(scope, receive, capture)
        except Exception:
            if "idempotency" in state:
                await state["idempotency"].store.release(state["idempotency"].key)
            raise
        if "idempotency" in state:
            response = StoredResponse(status, content_type, b"".join(chunks))
            await state["idempotency"].finish(response)
