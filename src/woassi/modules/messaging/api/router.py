"""Meta webhook: one public endpoint for every shop, routed by `phone_number_id`.

Public on purpose: Meta authenticates with the `X-Hub-Signature-256` HMAC, not a token.
"""

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from woassi.modules.messaging.api.deps import MessagingDep
from woassi.modules.messaging.domain.errors import InvalidSignature, VerificationRefused
from woassi.shared.api.errors import error_response

router = APIRouter(prefix="/webhooks/meta", tags=["messaging"])


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    messaging: MessagingDep,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    """Subscription handshake: echoes `hub.challenge` if `hub.verify_token` matches."""
    try:
        response = messaging.verify_meta_subscription().execute(
            mode=mode, token=token, challenge=challenge
        )
    except VerificationRefused as error:
        return error_response(error, status.HTTP_403_FORBIDDEN)
    return PlainTextResponse(response)


@router.post("")
async def receive_webhook(
    request: Request,
    messaging: MessagingDep,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> Response:
    # Raw body required: the signature covers the exact bytes sent by Meta.
    """Checks the signature and queues the events for the worker.

    Answers 200 as soon as they are queued, so Meta does not resend: retries are the worker's
    job.
    """
    body = await request.body()
    try:
        await messaging.receive_meta_webhook().execute(body=body, signature=signature)
    except InvalidSignature as error:
        return error_response(error, status.HTTP_401_UNAUTHORIZED)
    return Response(status_code=status.HTTP_200_OK)
