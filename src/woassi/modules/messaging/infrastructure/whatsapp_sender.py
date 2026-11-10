"""Sends messages through the Cloud API.

https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes
https://developers.facebook.com/documentation/business-messaging/whatsapp/throughput/
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from woassi.modules.messaging.application.ports import SendError, SendFailure, ShopConnection
from woassi.modules.messaging.domain.content import (
    ButtonsMessage,
    MessageContent,
    Recipient,
    TemplateMessage,
    TextMessage,
)
from woassi.shared.infrastructure.meta_graph import GraphClient, GraphUnavailable

OUTSIDE_WINDOW = 131047
# 130429: the number's throughput; 131056: one message every 6 s to the same user.
RATE_LIMITED = frozenset({130429, 131056})
INVALID_RECIPIENT = frozenset({131026})
INVALID_TOKEN = 190
# Long enough for the 6 s pair limit. Only for errors where Meta certainly sent nothing.
RETRY_DELAYS_S = (2.0, 6.0)


def _recipient(recipient: Recipient) -> dict[str, str]:
    # `to` takes precedence when both are sent.
    """Recipient fields of the request body: `to` (phone) or `recipient` (BSUID)."""
    if recipient.phone is not None:
        return {"to": recipient.phone.value}
    assert recipient.user_id is not None
    return {"recipient": recipient.user_id}


def _content(content: MessageContent) -> dict[str, Any]:
    """Type-specific fields of the request body."""
    match content:
        case TextMessage():
            return {"type": "text", "text": {"preview_url": False, "body": content.body}}
        case ButtonsMessage():
            interactive: dict[str, Any] = {
                "type": "button",
                "body": {"text": content.body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": button.id, "title": button.title}}
                        for button in content.buttons
                    ]
                },
            }
            if content.footer:
                interactive["footer"] = {"text": content.footer}
            return {"type": "interactive", "interactive": interactive}
        case TemplateMessage():
            template: dict[str, Any] = {
                "name": content.name,
                "language": {"code": content.language},
            }
            if content.body_parameters:
                template["components"] = [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "parameter_name": name, "text": value}
                            for name, value in content.body_parameters.items()
                        ],
                    }
                ]
            for index, button_payload in enumerate(content.button_payloads):
                template.setdefault("components", []).append(
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": str(index),
                        "parameters": [{"type": "payload", "payload": button_payload}],
                    }
                )
            return {"type": "template", "template": template}


def _failure(status: int, code: int | None) -> SendFailure:
    """Classifies a refusal by Meta's error code, then by HTTP status."""
    if code == OUTSIDE_WINDOW:
        return SendFailure.OUTSIDE_WINDOW
    if code in RATE_LIMITED:
        return SendFailure.RATE_LIMITED
    if code in INVALID_RECIPIENT:
        return SendFailure.INVALID_RECIPIENT
    if code == INVALID_TOKEN:
        return SendFailure.INVALID_TOKEN
    if status >= 500:
        return SendFailure.UNAVAILABLE
    return SendFailure.REJECTED


class HttpxWhatsAppSender:
    """`WhatsAppSender` on the Graph API, with a short retry on rate limits."""

    def __init__(
        self,
        graph: GraphClient,
        *,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._graph = graph
        self._retry_delays_s = retry_delays_s
        self._sleep = sleep

    async def send(
        self, connection: ShopConnection, recipient: Recipient, content: MessageContent
    ) -> str:
        """Sends `content` and returns Meta's message id (wamid).

        Retries only when Meta certainly sent nothing: connection failure or rate limit.
        Raises
        SendError otherwise.
        """
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            **_recipient(recipient),
            **_content(content),
        }
        delays = list(self._retry_delays_s)
        while True:
            try:
                response = await self._graph.request(
                    "POST",
                    f"/{connection.phone_number_id}/messages",
                    json=body,
                    token=connection.access_token,
                )
            except GraphUnavailable as error:
                # Once the request is out, Meta may have sent it: a retry could send it twice.
                if not error.request_sent and delays:
                    await self._sleep(delays.pop(0))
                    continue
                raise SendError(SendFailure.UNAVAILABLE) from None

            if response.ok:
                return _message_id(response.data)
            failure = _failure(response.status, response.error_code)
            if failure is SendFailure.RATE_LIMITED and delays:
                await self._sleep(delays.pop(0))
                continue
            raise SendError(failure, meta_code=response.error_code)


def _message_id(data: dict[str, Any]) -> str:
    """Message id from Meta's answer; raises SendError if missing."""
    messages = data.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        message_id = messages[0].get("id")
        if isinstance(message_id, str) and message_id:
            return message_id
    raise SendError(SendFailure.UNAVAILABLE)
