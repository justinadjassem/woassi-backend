import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import respx

from tests.fakes import FakeMessagingUoW, FakeShopConnections, FakeTemplateRegistry, MessagingState
from woassi.integration.messaging import template_definitions
from woassi.modules.messaging.application.ports import SendError, ShopConnection
from woassi.modules.messaging.application.use_cases.processing import HandleMetaEvent
from woassi.modules.messaging.application.use_cases.sending import SendWhatsAppMessage
from woassi.modules.messaging.application.use_cases.templates import (
    EnsureTemplateSendable,
    ProvisionTemplates,
)
from woassi.modules.messaging.domain.content import Recipient, TemplateMessage
from woassi.modules.messaging.domain.errors import InvalidMessage, TemplateNotApproved
from woassi.modules.messaging.domain.event import TemplateStatusUpdate
from woassi.modules.messaging.domain.templates import (
    MessageTemplate,
    TemplateDefinition,
    TemplateStatus,
)
from woassi.modules.messaging.infrastructure.template_registry import GraphTemplateRegistry
from woassi.shared.infrastructure.clock import FrozenClock
from woassi.shared.infrastructure.meta_graph import GraphClient

CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
SHOP = ShopConnection(shop_id=uuid4(), waba_id="1001", phone_number_id="2002", access_token="t")
URL = "https://graph.facebook.com/v27.0/1001/message_templates"
DEFINITION = TemplateDefinition(
    name="woassi_order_validation",
    language="fr",
    body="Commande n°{{order_number}} : {{total}}. Merci !",
    examples={"order_number": "42", "total": "15 000 FCFA"},
    quick_replies=("Valider", "Indisponible"),
)


def test_woassi_ships_its_templates_in_french_and_english() -> None:
    definitions = template_definitions()
    assert {(d.name, d.language) for d in definitions} == {
        ("woassi_order_validation", "fr"),
        ("woassi_order_validation", "en"),
        ("woassi_order_payment", "fr"),
        ("woassi_order_payment", "en"),
        ("woassi_payment_push", "fr"),
        ("woassi_payment_push", "en"),
        ("woassi_payment_number", "fr"),
        ("woassi_payment_number", "en"),
        ("woassi_invoice_ready", "fr"),
        ("woassi_invoice_ready", "en"),
    }
    by_name = {(d.name, d.language): d for d in definitions}
    assert len(by_name["woassi_order_validation", "fr"].quick_replies) == 2
    payment = by_name["woassi_order_payment", "fr"]
    assert payment.quick_replies == ()
    assert set(payment.examples) == {"order_number", "total", "link"}
    invoice = by_name["woassi_invoice_ready", "en"]
    assert set(invoice.examples) == {"invoice_number", "total", "link"}
    assert by_name["woassi_payment_push", "en"].quick_replies == ("Another number", "Payment link")
    assert by_name["woassi_payment_number", "fr"].quick_replies == ("Lien de paiement",)
    # Meta refuses a body that starts or ends with a parameter: none of ours does.
    for definition in definitions:
        assert not definition.body.startswith("{{"), definition.name
        assert not definition.body.rstrip(" .!?:;,").endswith("}}"), definition.name


@pytest.mark.parametrize(
    "body",
    ["{{total}} à payer.", "Payez ici : {{link}}", "Payez ici : {{link}} .", "Total {{total}} !"],
)
def test_a_body_cannot_start_or_end_with_a_parameter(body: str) -> None:
    examples = {name: "x" for name in ("total", "link") if f"{{{{{name}}}}}" in body}
    with pytest.raises(InvalidMessage):
        TemplateDefinition(name="t", language="fr", body=body, examples=examples)
        assert not body.lstrip().startswith("{{")


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "Order Validation"},
        {"language": "french"},
        {"body": ""},
        {"examples": {"order_number": "1"}},
        {"examples": {"order_number": "1", "total": "2", "extra": "3"}},
        {"quick_replies": ("x" * 26,)},
    ],
)
def test_invalid_definitions(fields: dict[str, object]) -> None:
    values = {
        "name": DEFINITION.name,
        "language": DEFINITION.language,
        "body": DEFINITION.body,
        "examples": DEFINITION.examples,
        "quick_replies": DEFINITION.quick_replies,
        **fields,
    }
    with pytest.raises(InvalidMessage):
        TemplateDefinition(**values)  # type: ignore[arg-type]


def test_template_parameters_must_be_one_line() -> None:
    with pytest.raises(InvalidMessage):
        TemplateMessage("woassi_order_validation", "fr", {"items": "a\nb"})


@pytest.fixture
def registry() -> GraphTemplateRegistry:
    return GraphTemplateRegistry(GraphClient(httpx.AsyncClient(), graph_version="v27.0"))


@respx.mock
async def test_submission_request(registry: GraphTemplateRegistry) -> None:
    route = respx.post(URL).respond(
        200, json={"id": "555", "status": "PENDING", "category": "UTILITY"}
    )

    submitted = await registry.submit(SHOP, DEFINITION)

    assert (submitted.meta_template_id, submitted.status) == ("555", "PENDING")
    assert route.calls.last.request.headers["authorization"] == "Bearer t"
    assert json.loads(route.calls.last.request.content) == {
        "name": "woassi_order_validation",
        "language": "fr",
        "category": "UTILITY",
        "parameter_format": "NAMED",
        "components": [
            {
                "type": "BODY",
                "text": "Commande n°{{order_number}} : {{total}}. Merci !",
                "example": {
                    "body_text_named_params": [
                        {"param_name": "order_number", "example": "42"},
                        {"param_name": "total", "example": "15 000 FCFA"},
                    ]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Valider"},
                    {"type": "QUICK_REPLY", "text": "Indisponible"},
                ],
            },
        ],
    }


@respx.mock
async def test_an_existing_template_is_read_back(registry: GraphTemplateRegistry) -> None:
    respx.post(URL).respond(400, json={"error": {"code": 100, "error_subcode": 2388024}})
    respx.get(URL).respond(
        200,
        json={
            "data": [
                {
                    "id": "1",
                    "name": "woassi_order_validation",
                    "language": "en",
                    "status": "PENDING",
                },
                {
                    "id": "2",
                    "name": "woassi_order_validation",
                    "language": "fr",
                    "status": "APPROVED",
                },
            ]
        },
    )
    submitted = await registry.submit(SHOP, DEFINITION)
    assert (submitted.meta_template_id, submitted.status) == ("2", "APPROVED")


@respx.mock
async def test_a_refused_submission(registry: GraphTemplateRegistry) -> None:
    respx.post(URL).respond(400, json={"error": {"code": 100, "message": "secret detail"}})
    with pytest.raises(SendError) as caught:
        await registry.submit(SHOP, DEFINITION)
    assert "secret detail" not in str(caught.value)


@respx.mock
async def test_status_matches_meta_language_codes(registry: GraphTemplateRegistry) -> None:
    respx.get(URL).respond(
        200, json={"data": [{"id": "9", "name": "n", "language": "en_US", "status": "APPROVED"}]}
    )
    found = await registry.status(SHOP, "n", "en-US")
    assert found is not None and found.status == "APPROVED"
    assert await registry.status(SHOP, "n", "fr") is None


async def test_provisioning_submits_every_definition() -> None:
    database, registry = MessagingState(), FakeTemplateRegistry()
    provision = ProvisionTemplates(
        FakeMessagingUoW(database),
        FakeShopConnections(SHOP),
        registry,
        template_definitions(),
        CLOCK,
    )

    assert await provision.execute(SHOP.shop_id) == 0

    assert len(registry.submitted) == len(template_definitions()) == 10
    stored = database.templates[(SHOP.shop_id, "woassi_order_validation", "fr")]
    assert stored.status is TemplateStatus.PENDING
    assert stored.waba_id == "1001"


async def test_provisioning_a_disconnected_shop_does_nothing() -> None:
    registry = FakeTemplateRegistry()
    provision = ProvisionTemplates(
        FakeMessagingUoW(MessagingState()),
        FakeShopConnections(),
        registry,
        template_definitions(),
        CLOCK,
    )
    assert await provision.execute(uuid4()) == 0
    assert registry.submitted == []


class Sender:
    def __init__(self) -> None:
        self.sent = 0

    async def send(self, connection: object, recipient: object, content: object) -> str:
        self.sent += 1
        return "wamid.T"


def sending(
    database: MessagingState, registry: FakeTemplateRegistry, sender: Sender
) -> SendWhatsAppMessage:
    uow = FakeMessagingUoW(database)
    return SendWhatsAppMessage(
        uow, FakeShopConnections(SHOP), sender, EnsureTemplateSendable(uow, registry, CLOCK), CLOCK
    )


TEMPLATE = TemplateMessage("woassi_order_validation", "fr", {"order_number": "1"})


async def send_template(
    database: MessagingState, registry: FakeTemplateRegistry, sender: Sender
) -> str:
    return await sending(database, registry, sender).execute(
        shop_id=SHOP.shop_id, recipient=Recipient(user_id="TG.1"), content=TEMPLATE, kind="test"
    )


async def test_an_unapproved_template_is_not_sent() -> None:
    database, registry, sender = MessagingState(), FakeTemplateRegistry("PENDING"), Sender()
    with pytest.raises(TemplateNotApproved):
        await send_template(database, registry, sender)
    assert sender.sent == 0


async def test_a_missed_approval_webhook_is_caught_up() -> None:
    database, registry, sender = MessagingState(), FakeTemplateRegistry("APPROVED"), Sender()
    database.templates[(SHOP.shop_id, TEMPLATE.name, "fr")] = MessageTemplate(
        SHOP.shop_id, "1001", TEMPLATE.name, "fr", TemplateStatus.PENDING, CLOCK.now()
    )

    assert await send_template(database, registry, sender) == "wamid.T"

    assert database.templates[(SHOP.shop_id, TEMPLATE.name, "fr")].status is TemplateStatus.APPROVED
    # Known as approved now: Meta is not asked again.
    await send_template(database, registry, sender)
    assert registry.lookups == 1


async def test_status_webhook_updates_the_shops_of_that_account() -> None:
    database = MessagingState()
    for shop_id, waba in ((SHOP.shop_id, "1001"), (uuid4(), "other")):
        database.templates[(shop_id, TEMPLATE.name, "fr")] = MessageTemplate(
            shop_id, waba, TEMPLATE.name, "fr", TemplateStatus.APPROVED, CLOCK.now()
        )
    handle = HandleMetaEvent(
        FakeMessagingUoW(database),
        FakeShopConnections(SHOP),
        None,
        None,
        CLOCK,  # type: ignore[arg-type]
    )

    await handle.execute(TemplateStatusUpdate("1001", TEMPLATE.name, "fr", "PAUSED"))
    await handle.execute(TemplateStatusUpdate("1001", TEMPLATE.name, "fr", "FLAGGED"))

    statuses = {t.waba_id: t.status for t in database.templates.values()}
    assert statuses == {"1001": TemplateStatus.PAUSED, "other": TemplateStatus.APPROVED}


@respx.mock
async def test_quick_reply_payloads_are_sent() -> None:
    from woassi.modules.messaging.infrastructure.whatsapp_sender import HttpxWhatsAppSender

    route = respx.post("https://graph.facebook.com/v27.0/2002/messages").respond(
        200, json={"messages": [{"id": "wamid.X"}]}
    )
    sender = HttpxWhatsAppSender(GraphClient(httpx.AsyncClient(), graph_version="v27.0"))
    content = TemplateMessage(
        "woassi_order_validation", "fr", {"total": "1"}, ("order:accept:1", "order:refuse:1")
    )

    await sender.send(SHOP, Recipient(user_id="TG.1"), content)

    components = json.loads(route.calls.last.request.content)["template"]["components"]
    assert components[1:] == [
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(index),
            "parameters": [{"type": "payload", "payload": payload}],
        }
        for index, payload in enumerate(("order:accept:1", "order:refuse:1"))
    ]
