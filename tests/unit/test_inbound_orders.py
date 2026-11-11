from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from woassi.integration.orders import OrdersInboundHandler
from woassi.modules.messaging.domain.event import InboundMessage
from woassi.modules.orders.application.use_cases.orders import Cart
from woassi.shared.domain.values import PhoneNumber
from woassi.shared.infrastructure.clock import FrozenClock

SHOP = uuid4()
CUSTOMER = uuid4()


class Recorder:
    def __init__(self, reply_result: bool = True) -> None:
        self.contacts: list[dict[str, Any]] = []
        self.carts: list[Cart] = []
        self.replies: list[dict[str, Any]] = []
        self.user_id_changes: list[dict[str, Any]] = []
        self.payment_replies: list[dict[str, Any]] = []
        self.payer_numbers: list[dict[str, Any]] = []
        self.reply_result = reply_result
        self.number_taken = False

    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - replaced below
        raise NotImplementedError


class Contacts:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def execute(self, **kwargs: Any) -> UUID:
        self.recorder.contacts.append(kwargs)
        return CUSTOMER


class Changes:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def execute(self, **kwargs: Any) -> bool:
        self.recorder.user_id_changes.append(kwargs)
        return True


class Carts:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def execute(self, *, shop_id: UUID, cart: Cart) -> UUID:
        self.recorder.carts.append(cart)
        return uuid4()


class Replies:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def execute(self, **kwargs: Any) -> bool:
        self.recorder.replies.append(kwargs)
        return self.recorder.reply_result


class PaymentReplies:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def execute(self, **kwargs: Any) -> bool:
        self.recorder.payment_replies.append(kwargs)
        return self.recorder.reply_result


class PayerNumbers:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def execute(self, **kwargs: Any) -> bool:
        self.recorder.payer_numbers.append(kwargs)
        return self.recorder.number_taken


def handler(recorder: Recorder) -> OrdersInboundHandler:
    return OrdersInboundHandler(
        Contacts(recorder),  # type: ignore[arg-type]
        Changes(recorder),  # type: ignore[arg-type]
        Carts(recorder),  # type: ignore[arg-type]
        Replies(recorder),  # type: ignore[arg-type]
        PaymentReplies(recorder),  # type: ignore[arg-type]
        PayerNumbers(recorder),  # type: ignore[arg-type]
        FrozenClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )


def message(type_: str, payload: dict[str, Any], **extra: Any) -> InboundMessage:
    return InboundMessage(
        phone_number_id="2002",
        message_id="wamid.IN",
        type=type_,
        sender_user_id="TG.1",
        sender_phone="22891234567",
        profile_name="Ama",
        payload=payload,
        timestamp=1750096325,
        **extra,
    )


ORDER = {
    "catalog_id": "1",
    "text": "Livraison à Bè",
    "product_items": [
        {"product_retailer_id": "SKU-PAGNE", "quantity": 2, "item_price": 5000, "currency": "XOF"},
        {"product_retailer_id": "SKU-SAC", "quantity": 1, "item_price": 12500, "currency": "XOF"},
        {"product_retailer_id": "", "quantity": 1},
        "garbage",
    ],
}


async def test_a_cart_becomes_an_order() -> None:
    recorder = Recorder()
    await handler(recorder).handle(SHOP, message("order", ORDER))

    [contact] = recorder.contacts
    assert contact["user_id"] == "TG.1"
    assert contact["phone"] == PhoneNumber("+22891234567")
    assert contact["at"] == datetime.fromtimestamp(1750096325, UTC)
    [cart] = recorder.carts
    assert cart.items == (("SKU-PAGNE", 2), ("SKU-SAC", 1))
    assert cart.customer_id == CUSTOMER
    assert cart.note == "Livraison à Bè"
    assert cart.message_id == "wamid.IN"


async def test_an_empty_cart_creates_no_order() -> None:
    recorder = Recorder()
    await handler(recorder).handle(SHOP, message("order", {"product_items": []}))
    assert recorder.carts == []
    assert len(recorder.contacts) == 1


async def test_a_template_quick_reply() -> None:
    recorder = Recorder()
    await handler(recorder).handle(
        SHOP,
        message(
            "button", {"payload": "order:accept:x", "text": "Valider"}, context_message_id="wamid.Q"
        ),
    )
    assert recorder.replies == [
        {"shop_id": SHOP, "payload": "order:accept:x", "context_message_id": "wamid.Q"}
    ]
    # The merchant is not recorded as a customer of their own shop.
    assert recorder.contacts == []


async def test_a_reply_button() -> None:
    recorder = Recorder()
    payload = {"type": "button_reply", "button_reply": {"id": "order:offer_yes:x", "title": "Oui"}}
    await handler(recorder).handle(SHOP, message("interactive", payload, context_message_id="w"))
    assert recorder.replies[0]["payload"] == "order:offer_yes:x"


async def test_a_button_that_is_not_ours_comes_from_a_customer() -> None:
    recorder = Recorder(reply_result=False)
    await handler(recorder).handle(SHOP, message("button", {"payload": "Unsubscribe"}))
    assert len(recorder.contacts) == 1


@pytest.mark.parametrize("type_", ["text", "image", "location"])
async def test_other_messages_only_keep_the_customer(type_: str) -> None:
    recorder = Recorder()
    await handler(recorder).handle(SHOP, message(type_, {"body": "Bonjour"}))
    assert len(recorder.contacts) == 1
    assert recorder.carts == recorder.replies == []


async def test_payment_buttons_go_to_payments() -> None:
    recorder = Recorder()
    await handler(recorder).handle(
        SHOP,
        message(
            "interactive",
            {"type": "button_reply", "button_reply": {"id": "payment:link:x", "title": "Lien"}},
            context_message_id="wamid.PROMPT",
        ),
    )
    assert recorder.replies == []
    assert recorder.payment_replies == [
        {"shop_id": SHOP, "payload": "payment:link:x", "context_message_id": "wamid.PROMPT"}
    ]
    assert recorder.contacts == []


async def test_a_text_may_be_the_payer_number() -> None:
    recorder = Recorder()
    recorder.number_taken = True
    await handler(recorder).handle(SHOP, message("text", {"body": "90 12 34 56"}))
    # The customer is recorded first (the 24-hour window), then payments sees the text.
    assert len(recorder.contacts) == 1
    assert recorder.payer_numbers == [
        {"shop_id": SHOP, "customer_id": CUSTOMER, "body": "90 12 34 56"}
    ]


async def test_carts_never_reach_payments() -> None:
    recorder = Recorder()
    await handler(recorder).handle(SHOP, message("order", ORDER))
    assert recorder.payer_numbers == [] and len(recorder.carts) == 1


async def test_a_customer_with_a_username_has_no_phone() -> None:
    recorder = Recorder()
    no_phone = InboundMessage(
        phone_number_id="2002", message_id="m", type="text", sender_user_id="TG.1"
    )
    await handler(recorder).handle(SHOP, no_phone)
    assert recorder.contacts[0]["phone"] is None


async def test_a_changed_user_id_is_followed() -> None:
    recorder = Recorder()
    payload = {"type": "user_changed_number", "previous_user_id": "TG.1", "user_id": "TG.2"}
    await handler(recorder).handle(SHOP, message("system", payload))
    assert recorder.user_id_changes == [
        {"shop_id": SHOP, "previous_user_id": "TG.1", "user_id": "TG.2"}
    ]
    assert recorder.contacts == []
