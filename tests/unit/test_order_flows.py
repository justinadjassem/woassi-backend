from datetime import timedelta

import pytest

from tests.orders_fakes import CUSTOMER, MERCHANT_PHONE, OrdersStack
from woassi.modules.orders.application.ports import Buttons, Template, Text
from woassi.modules.orders.domain.errors import OrderNotOpen
from woassi.modules.orders.domain.events import OrderConfirmed
from woassi.modules.orders.domain.order import OrderStatus, ValidationMode


@pytest.fixture
def stack() -> OrdersStack:
    stack = OrdersStack()
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)
    stack.catalog.add("SKU-SAC", "Sac", 12500, stock=1)
    return stack


def merchant_buttons(stack: OrdersStack) -> tuple[str, str, str]:
    """(accept payload, refuse payload, message id) of the last request to the merchant."""
    template, message_id = stack.messaging.to_merchant()[-1]
    accept, refuse = template.button_payloads
    return accept, refuse, message_id


# Receiving a cart


async def test_manual_mode(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-SAC", 1))

    assert order.status is OrderStatus.AWAITING_MERCHANT
    assert order.number == 1
    assert order.total.value == 22500
    assert (stack.catalog.stock("SKU-PAGNE"), stack.catalog.stock("SKU-SAC")) == (8, 0)

    [(to_customer, _)] = stack.messaging.to_customer()
    assert isinstance(to_customer, Text) and "n°1" in to_customer.body
    [(template, message_id)] = stack.messaging.to_merchant()
    assert template.name == "woassi_order_validation" and template.language == "fr"
    assert template.parameters == {
        "order_number": "1",
        "customer_name": "Ama",
        "items": "2 x Pagne wax, 1 x Sac",
        "total": "22 500 FCFA",
    }
    assert template.button_payloads == (f"order:accept:{order.id}", f"order:refuse:{order.id}")
    assert stack.order(order).merchant_message_ids == [message_id]


async def test_numbers_follow_each_other(stack: OrdersStack) -> None:
    first = await stack.receive(("SKU-PAGNE", 1), message_id="a")
    second = await stack.receive(("SKU-PAGNE", 1), message_id="b")
    assert (first.number, second.number) == (1, 2)


async def test_automatic_mode_confirms_at_once() -> None:
    stack = OrdersStack(mode=ValidationMode.AUTOMATIC)
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)

    order = await stack.receive(("SKU-PAGNE", 2))

    assert order.status is OrderStatus.CONFIRMED
    assert stack.messaging.to_merchant() == []
    [(text, _)] = stack.messaging.to_customer()
    assert isinstance(text, Text)
    assert text.body == (
        "Votre commande n°1 est confirmée : 2 x Pagne wax. Total : 10 000 FCFA.\n"
        "Vous allez recevoir les instructions de paiement."
    )
    assert "facture" not in text.body.lower()
    assert [type(e) for e in stack.events] == [OrderConfirmed]


async def test_automatic_mode_above_the_limit() -> None:
    stack = OrdersStack(mode=ValidationMode.AUTOMATIC, max_amount=8000)
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)
    assert (await stack.receive(("SKU-PAGNE", 2))).status is OrderStatus.AWAITING_MERCHANT


async def test_same_product_twice_in_a_cart_is_added_up(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-PAGNE", 3))
    assert [(line.retailer_id, line.quantity) for line in order.lines] == [("SKU-PAGNE", 5)]


async def test_a_replayed_cart_gives_the_same_order(stack: OrdersStack) -> None:
    first = await stack.receive(("SKU-PAGNE", 2))
    again = await stack.receive(("SKU-PAGNE", 2))
    assert again.id == first.id
    assert stack.catalog.stock("SKU-PAGNE") == 8
    assert len(stack.messaging.sent) == 2


async def test_out_of_stock(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-UNKNOWN", 1))
    assert order.status is OrderStatus.REJECTED
    [(text, _)] = stack.messaging.to_customer()
    assert isinstance(text, Text) and "plus disponibles" in text.body
    assert stack.messaging.to_merchant() == []


async def test_a_shop_without_profile_takes_no_order() -> None:
    stack = OrdersStack()
    stack.shops.current = None
    from woassi.modules.orders.application.use_cases.orders import Cart, ReceiveCart

    result = await stack._flow(ReceiveCart).execute(
        shop_id=stack.shop_id, cart=Cart("m", stack.customer_id, (("x", 1),))
    )
    assert result is None


async def test_a_failed_notification_does_not_lose_the_order(stack: OrdersStack) -> None:
    stack.messaging.failing = True
    order = await stack.receive(("SKU-PAGNE", 1))
    assert order.status is OrderStatus.AWAITING_MERCHANT
    assert stack.order(order).merchant_message_ids == []


async def test_without_notification_number_the_order_waits_in_the_dashboard() -> None:
    stack = OrdersStack(notification_number=None)
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)
    order = await stack.receive(("SKU-PAGNE", 1))
    assert order.status is OrderStatus.AWAITING_MERCHANT
    assert stack.messaging.to_merchant() == []


async def test_english_shop() -> None:
    stack = OrdersStack(language="en")
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)
    await stack.receive(("SKU-PAGNE", 1))
    [(text, _)] = stack.messaging.to_customer()
    assert isinstance(text, Text) and text.body.startswith("Thank you")
    assert stack.messaging.to_merchant()[0][0].language == "en"


# The merchant answers on WhatsApp


async def test_merchant_accepts(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    accept, _, message_id = merchant_buttons(stack)

    assert await stack.reply(accept, message_id)

    assert stack.order(order).status is OrderStatus.CONFIRMED
    assert stack.catalog.stock("SKU-PAGNE") == 8
    text = stack.messaging.to_customer()[-1][0]
    assert isinstance(text, Text)
    assert "confirmée : 2 x Pagne wax. Total : 10 000 FCFA." in text.body
    assert text.body.endswith("Vous allez recevoir les instructions de paiement.")
    [entry] = stack.audit_log.entries
    assert entry.action == "order.validated"
    assert entry.details["channel"] == "whatsapp"
    assert OrderConfirmed in [type(e) for e in stack.events]


async def test_merchant_refuses_and_the_stock_comes_back(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    _, refuse, message_id = merchant_buttons(stack)

    assert await stack.reply(refuse, message_id)

    assert stack.order(order).status is OrderStatus.REJECTED
    assert stack.catalog.stock("SKU-PAGNE") == 10
    assert stack.audit_log.entries[0].action == "order.refused"


async def test_a_reply_to_another_message_is_ignored(stack: OrdersStack) -> None:
    """The payload alone proves nothing: anyone can type `order:accept:<id>`."""
    order = await stack.receive(("SKU-PAGNE", 2))
    accept, _, _ = merchant_buttons(stack)

    assert not await stack.reply(accept, "wamid.something-else")
    assert not await stack.reply(accept, None)
    assert stack.order(order).status is OrderStatus.AWAITING_MERCHANT


async def test_a_customer_cannot_use_the_merchant_buttons(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    customer_message_id = stack.messaging.to_customer()[0][1]
    accept, _, _ = merchant_buttons(stack)
    assert not await stack.reply(accept, customer_message_id)
    assert stack.order(order).status is OrderStatus.AWAITING_MERCHANT


async def test_a_second_tap_changes_nothing(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    accept, refuse, message_id = merchant_buttons(stack)
    await stack.reply(accept, message_id)
    assert not await stack.reply(refuse, message_id)
    assert stack.order(order).status is OrderStatus.CONFIRMED


@pytest.mark.parametrize("payload", ["hello", "order:accept", "order:accept:not-a-uuid", "x:y:z"])
async def test_unrelated_payloads(stack: OrdersStack, payload: str) -> None:
    await stack.receive(("SKU-PAGNE", 1))
    assert not await stack.reply(payload, "wamid.2")


# Part of the cart is missing


async def test_partial_cart_is_offered_to_the_customer(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-SAC", 3))

    assert order.status is OrderStatus.AWAITING_CUSTOMER
    assert stack.messaging.to_merchant() == []
    [(offer, offer_id)] = stack.messaging.to_customer()
    assert isinstance(offer, Buttons)
    assert "3 x Sac" in offer.body and "2 x Pagne wax, 1 x Sac" in offer.body
    assert "22 500 FCFA" in offer.body
    assert [title for _, title in offer.buttons] == ["Oui, commander", "Non, annuler"]
    assert stack.order(order).offer_message_id == offer_id


async def test_customer_takes_the_rest(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-SAC", 3))
    offer, offer_id = stack.messaging.to_customer()[0]
    assert isinstance(offer, Buttons)

    assert await stack.reply(offer.buttons[0][0], offer_id)

    updated = stack.order(order)
    assert updated.status is OrderStatus.AWAITING_MERCHANT
    assert [(line.name, line.quantity) for line in updated.lines] == [("Pagne wax", 2), ("Sac", 1)]
    assert len(stack.messaging.to_merchant()) == 1

    accept, _, message_id = merchant_buttons(stack)
    assert await stack.reply(accept, message_id)
    confirmation = stack.messaging.to_customer()[-1][0]
    assert isinstance(confirmation, Text)
    assert "confirmée : 2 x Pagne wax, 1 x Sac. Total : 22 500 FCFA." in confirmation.body
    assert "3 x Sac" not in confirmation.body


async def test_customer_takes_the_rest_in_automatic_mode() -> None:
    stack = OrdersStack(mode=ValidationMode.AUTOMATIC, language="en")
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)
    stack.catalog.add("SKU-SAC", "Bag", 12500, stock=1)
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-SAC", 3))
    offer, offer_id = stack.messaging.to_customer()[0]
    assert isinstance(offer, Buttons)

    assert await stack.reply(offer.buttons[0][0], offer_id)

    assert stack.order(order).status is OrderStatus.CONFIRMED
    confirmation = stack.messaging.to_customer()[-1][0]
    assert isinstance(confirmation, Text)
    assert confirmation.body == (
        "Your order #1 is confirmed: 2 x Pagne wax, 1 x Bag. Total: 22 500 FCFA.\n"
        "You will receive the payment instructions shortly."
    )
    assert "invoice" not in confirmation.body.lower()


async def test_a_long_cart_is_cut_in_the_confirmation() -> None:
    stack = OrdersStack(mode=ValidationMode.AUTOMATIC)
    skus = [f"SKU-{i}" for i in range(200)]
    for i, sku in enumerate(skus):
        stack.catalog.add(sku, f"Pagne wax motif numéro {i} " + "x" * 40, 1000, stock=5)

    await stack.receive(*((sku, 1) for sku in skus))

    [(confirmation, _)] = stack.messaging.to_customer()
    assert isinstance(confirmation, Text)
    assert len(confirmation.body) < 1024
    assert "1 x Pagne wax motif numéro 0 " in confirmation.body
    assert "numéro 199" not in confirmation.body
    kept = confirmation.body.count("1 x Pagne wax")
    assert f", … (+{200 - kept}). Total : 200 000 FCFA." in confirmation.body


async def test_the_offer_buttons_only_answer_the_offer(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-SAC", 3))
    offer, _ = stack.messaging.to_customer()[0]
    assert isinstance(offer, Buttons)

    assert not await stack.reply(offer.buttons[0][0], "wamid.another-message")
    assert stack.order(order).status is OrderStatus.AWAITING_CUSTOMER


async def test_customer_declines_and_the_stock_comes_back(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2), ("SKU-SAC", 3))
    offer, offer_id = stack.messaging.to_customer()[0]
    assert isinstance(offer, Buttons)

    assert await stack.reply(offer.buttons[1][0], offer_id)

    assert stack.order(order).status is OrderStatus.CANCELLED
    assert (stack.catalog.stock("SKU-PAGNE"), stack.catalog.stock("SKU-SAC")) == (10, 1)


# Reminders and expiry


async def test_reminders_at_15_and_60_minutes_then_expiry(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))

    stack.clock.advance(timedelta(minutes=14))
    assert await stack.tick() == 0
    stack.clock.advance(timedelta(minutes=1))
    assert await stack.tick() == 1
    stack.clock.advance(timedelta(minutes=45))
    assert await stack.tick() == 1
    stack.clock.advance(timedelta(hours=1))
    assert await stack.tick() == 0

    kinds = [kind for _, _, kind, _ in stack.messaging.sent if kind.startswith("order.validation")]
    assert kinds == [
        "order.validation_request",
        "order.validation_reminder",
        "order.validation_reminder",
    ]
    assert len(stack.order(order).merchant_message_ids) == 3

    stack.clock.advance(timedelta(hours=22))
    assert await stack.tick() == 1
    expired = stack.order(order)
    assert expired.status is OrderStatus.EXPIRED
    assert stack.catalog.stock("SKU-PAGNE") == 10
    text = stack.messaging.to_customer()[-1][0]
    assert isinstance(text, Text) and "expiré" in text.body
    assert await stack.tick() == 0


async def test_a_reminder_button_works_too(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    stack.clock.advance(timedelta(minutes=15))
    await stack.tick()
    accept, _, reminder_id = merchant_buttons(stack)

    assert await stack.reply(accept, reminder_id)
    assert stack.order(order).status is OrderStatus.CONFIRMED


async def test_a_late_answer_after_expiry_is_ignored(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    accept, _, message_id = merchant_buttons(stack)
    stack.clock.advance(timedelta(hours=24))
    await stack.tick()

    assert not await stack.reply(accept, message_id)
    assert stack.order(order).status is OrderStatus.EXPIRED


async def test_an_offer_without_answer_expires(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-SAC", 3))
    stack.clock.advance(timedelta(hours=24))
    await stack.tick()
    assert stack.order(order).status is OrderStatus.EXPIRED
    assert stack.catalog.stock("SKU-SAC") == 1


# From the dashboard


async def test_dashboard_decision_is_audited(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    decided = await stack.decide(order, accept=False)
    assert decided.status is OrderStatus.REJECTED
    assert stack.audit_log.entries[0].details["channel"] == "dashboard"
    assert stack.catalog.stock("SKU-PAGNE") == 10


async def test_dashboard_cannot_decide_twice(stack: OrdersStack) -> None:
    order = await stack.receive(("SKU-PAGNE", 2))
    await stack.decide(order, accept=True)
    with pytest.raises(OrderNotOpen):
        await stack.decide(order, accept=False)


async def test_customer_messages_go_to_the_customer(stack: OrdersStack) -> None:
    await stack.receive(("SKU-PAGNE", 2))
    to, _, _, _ = stack.messaging.sent[0]
    assert to == CUSTOMER
    assert stack.messaging.sent[1][0].phone == MERCHANT_PHONE
    assert isinstance(stack.messaging.sent[1][1], Template)


# Payment


async def test_a_payment_marks_the_order_paid() -> None:
    from tests.orders_fakes import FakeOrdersUoW
    from woassi.modules.orders.application.use_cases.orders import MarkOrderPaid
    from woassi.modules.orders.domain.events import OrderPaid

    stack = OrdersStack(mode=ValidationMode.AUTOMATIC)
    stack.catalog.add("SKU-PAGNE", "Pagne wax", 5000, stock=10)
    order = await stack.receive(("SKU-PAGNE", 2))
    mark_paid = MarkOrderPaid(FakeOrdersUoW(stack), stack.clock)

    assert await mark_paid.execute(shop_id=stack.shop_id, order_id=order.id)
    paid = stack.order(order)
    assert paid.status is OrderStatus.PAID and paid.paid_at == stack.clock.now()
    assert isinstance(stack.events[-1], OrderPaid)
    # Replayed event: nothing changes.
    assert not await mark_paid.execute(shop_id=stack.shop_id, order_id=order.id)
    # A paid order keeps its stock and can no longer be closed.
    assert stack.catalog.stock("SKU-PAGNE") == 8
    with pytest.raises(OrderNotOpen):
        paid.refuse(stack.clock.now())
    with pytest.raises(OrderNotOpen):
        paid.expire(stack.clock.now())
    assert paid.due_action(stack.clock.now() + timedelta(days=30)) is None


async def test_only_a_confirmed_order_can_be_paid(stack: OrdersStack) -> None:
    from tests.orders_fakes import FakeOrdersUoW
    from woassi.modules.orders.application.use_cases.orders import MarkOrderPaid

    order = await stack.receive(("SKU-PAGNE", 1))
    assert order.status is OrderStatus.AWAITING_MERCHANT
    mark_paid = MarkOrderPaid(FakeOrdersUoW(stack), stack.clock)
    assert not await mark_paid.execute(shop_id=stack.shop_id, order_id=order.id)
    assert stack.order(order).status is OrderStatus.AWAITING_MERCHANT
    with pytest.raises(OrderNotOpen):
        order.mark_paid(stack.clock.now())
