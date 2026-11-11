import json
import logging

import httpx
import pytest
import respx
import structlog

from woassi.modules.messaging.application.ports import SendError, SendFailure, ShopConnection
from woassi.modules.messaging.domain.content import (
    ButtonsMessage,
    DocumentMessage,
    Recipient,
    ReplyButton,
    TemplateMessage,
    TextMessage,
)
from woassi.modules.messaging.infrastructure.whatsapp_sender import HttpxWhatsAppSender
from woassi.shared.domain.values import PhoneNumber
from woassi.shared.infrastructure.meta_graph import GraphClient

TOKEN = "EAAG-a-recognizable-business-token"
URL = "https://graph.facebook.com/v27.0/2002/messages"
SHOP = ShopConnection(
    shop_id=__import__("uuid").uuid4(), waba_id="1001", phone_number_id="2002", access_token=TOKEN
)
BY_PHONE = Recipient(phone=PhoneNumber("+22890123456"))
BY_USER_ID = Recipient(user_id="TG.13491208655302741918")
ACCEPTED = {"messaging_product": "whatsapp", "messages": [{"id": "wamid.SENT"}]}


def meta_error(code: int, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "secret detail"}})


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def sender(sleeps: Sleeps) -> HttpxWhatsAppSender:
    return HttpxWhatsAppSender(
        GraphClient(httpx.AsyncClient(), graph_version="v27.0"), sleep=sleeps
    )


def sent(route: respx.Route, index: int = -1) -> dict[str, object]:
    return json.loads(route.calls[index].request.content)


@respx.mock
async def test_text(sender: HttpxWhatsAppSender) -> None:
    route = respx.post(URL).respond(200, json=ACCEPTED)

    assert await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour")) == "wamid.SENT"

    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent(route) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+22890123456",
        "type": "text",
        "text": {"preview_url": False, "body": "Bonjour"},
    }


@respx.mock
async def test_document(sender: HttpxWhatsAppSender) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ACCEPTED))
    message = DocumentMessage(
        "https://files.example/FAC-1.pdf?X-Amz-Signature=abc", "FAC-1.pdf", "Votre facture"
    )
    assert await sender.send(SHOP, BY_PHONE, message) == "wamid.SENT"
    body = sent(route)
    assert body["type"] == "document"
    assert body["document"] == {
        "link": "https://files.example/FAC-1.pdf?X-Amz-Signature=abc",
        "filename": "FAC-1.pdf",
        "caption": "Votre facture",
    }
    await sender.send(SHOP, BY_PHONE, DocumentMessage("https://f.example/a.pdf", "a.pdf"))
    assert "caption" not in sent(route)["document"]  # type: ignore[operator]


@respx.mock
async def test_a_customer_without_a_known_phone_gets_it_by_user_id(
    sender: HttpxWhatsAppSender,
) -> None:
    route = respx.post(URL).respond(200, json=ACCEPTED)
    await sender.send(SHOP, BY_USER_ID, TextMessage("Bonjour"))
    body = sent(route)
    assert body["recipient"] == "TG.13491208655302741918"
    assert "to" not in body


@respx.mock
async def test_buttons(sender: HttpxWhatsAppSender) -> None:
    route = respx.post(URL).respond(200, json=ACCEPTED)
    content = ButtonsMessage(
        "Nouvelle commande de 15 000 FCFA",
        (ReplyButton("accept:42", "Valider"), ReplyButton("refuse:42", "Indisponible")),
        footer="Woassi",
    )

    await sender.send(SHOP, BY_PHONE, content)

    assert sent(route)["interactive"] == {
        "type": "button",
        "body": {"text": "Nouvelle commande de 15 000 FCFA"},
        "footer": {"text": "Woassi"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "accept:42", "title": "Valider"}},
                {"type": "reply", "reply": {"id": "refuse:42", "title": "Indisponible"}},
            ]
        },
    }


@respx.mock
async def test_template(sender: HttpxWhatsAppSender) -> None:
    route = respx.post(URL).respond(200, json=ACCEPTED)
    await sender.send(SHOP, BY_PHONE, TemplateMessage("order_request", "fr", {"total": "15 000"}))
    assert sent(route)["template"] == {
        "name": "order_request",
        "language": {"code": "fr"},
        "components": [
            {
                "type": "body",
                "parameters": [{"type": "text", "parameter_name": "total", "text": "15 000"}],
            }
        ],
    }


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (meta_error(131047), SendFailure.OUTSIDE_WINDOW),
        (meta_error(131026), SendFailure.INVALID_RECIPIENT),
        (meta_error(190, 401), SendFailure.INVALID_TOKEN),
        (meta_error(1, 500), SendFailure.UNAVAILABLE),
        (meta_error(100), SendFailure.REJECTED),
        (httpx.Response(200, json={"messages": []}), SendFailure.UNAVAILABLE),
    ],
)
@respx.mock
async def test_failures(
    sender: HttpxWhatsAppSender, response: httpx.Response, failure: SendFailure
) -> None:
    respx.post(URL).mock(return_value=response)
    with pytest.raises(SendError) as caught:
        await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour"))
    assert caught.value.failure is failure
    assert "secret detail" not in str(caught.value)


@pytest.mark.parametrize("code", [130429, 131056])
@respx.mock
async def test_rate_limits_are_retried(
    sender: HttpxWhatsAppSender, sleeps: Sleeps, code: int
) -> None:
    route = respx.post(URL).mock(side_effect=[meta_error(code), httpx.Response(200, json=ACCEPTED)])
    assert await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour")) == "wamid.SENT"
    assert route.call_count == 2
    assert sleeps.delays == [2.0]


@respx.mock
async def test_retries_give_up(sender: HttpxWhatsAppSender, sleeps: Sleeps) -> None:
    route = respx.post(URL).mock(return_value=meta_error(130429))
    with pytest.raises(SendError) as caught:
        await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour"))
    assert caught.value.failure is SendFailure.RATE_LIMITED
    assert route.call_count == 3
    assert sleeps.delays == [2.0, 6.0]


@respx.mock
async def test_a_failed_connection_is_retried(sender: HttpxWhatsAppSender) -> None:
    route = respx.post(URL).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=ACCEPTED)]
    )
    assert await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour")) == "wamid.SENT"
    assert route.call_count == 2


@respx.mock
async def test_a_timeout_is_not_retried(sender: HttpxWhatsAppSender) -> None:
    """Meta may have sent the message already: a retry could reach the customer twice."""
    route = respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SendError) as caught:
        await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour"))
    assert caught.value.failure is SendFailure.UNAVAILABLE
    assert route.call_count == 1


@respx.mock
async def test_outside_the_window_is_not_retried(sender: HttpxWhatsAppSender) -> None:
    route = respx.post(URL).mock(return_value=meta_error(131047))
    with pytest.raises(SendError):
        await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour"))
    assert route.call_count == 1


@respx.mock
async def test_nothing_secret_reaches_the_logs(
    sender: HttpxWhatsAppSender, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(URL).mock(side_effect=[httpx.ConnectError("x"), meta_error(131047)])
    with (
        caplog.at_level(logging.DEBUG),
        structlog.testing.capture_logs() as logs,
        pytest.raises(SendError) as caught,
    ):
        await sender.send(SHOP, BY_PHONE, TextMessage("Bonjour"))
    written = caplog.text + repr(logs) + str(caught.value) + repr(caught.value.__context__)
    for secret in (TOKEN, "22890123456", "Bonjour"):
        assert secret not in written
