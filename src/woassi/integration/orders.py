"""Inbound WhatsApp messages seen by `orders` and `customers`.

Messaging hands every inbound message to `OrdersInboundHandler`, which turns carts into
orders,
button replies into order decisions, and keeps customer records up to date.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from woassi.modules.customers.application.use_cases.customers import (
    ChangeCustomerUserId,
    RecordContact,
)
from woassi.modules.messaging.domain.event import InboundMessage
from woassi.modules.orders.application.use_cases.orders import Cart, ReceiveCart, ReplyToOrder
from woassi.modules.payments.application.use_cases.conversation import (
    ReceivePayerNumber,
    ReplyToPayment,
)
from woassi.shared.application.ports import Clock
from woassi.shared.domain.errors import InvalidData
from woassi.shared.domain.values import PhoneNumber

log = structlog.get_logger()

MAX_CART_LINES = 100


def _phone(raw: str | None) -> PhoneNumber | None:
    """Parses Meta's `from` (digits without the plus sign); None if missing or invalid."""
    if not raw:
        return None
    try:
        return PhoneNumber(raw if raw.startswith("+") else f"+{raw}")
    except InvalidData:
        return None


def _cart_items(payload: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    """(retailer id, quantity) pairs of a cart message.

    Malformed items are skipped, and ids and line count are capped: the payload comes from
    outside.
    """
    items = []
    for item in payload.get("product_items") or []:
        if not isinstance(item, Mapping):
            continue
        retailer_id, quantity = item.get("product_retailer_id"), item.get("quantity")
        if isinstance(retailer_id, str) and retailer_id and isinstance(quantity, int):
            items.append((retailer_id[:100], quantity))
    return tuple(items[:MAX_CART_LINES])


def _reply_payload(message: InboundMessage) -> str | None:
    """Quick reply of a template (`button`), or reply button of a free-form message."""
    if message.type == "button":
        payload = message.payload.get("payload")
    elif message.type == "interactive":
        reply = message.payload.get("button_reply")
        payload = reply.get("id") if isinstance(reply, Mapping) else None
    else:
        payload = None
    return payload if isinstance(payload, str) else None


class OrdersInboundHandler:
    """InboundMessageHandler: what a customer or a merchant sends to a shop's number."""

    def __init__(
        self,
        record_contact: RecordContact,
        change_user_id: ChangeCustomerUserId,
        receive_cart: ReceiveCart,
        reply_to_order: ReplyToOrder,
        reply_to_payment: ReplyToPayment,
        receive_payer_number: ReceivePayerNumber,
        clock: Clock,
    ) -> None:
        self._record_contact = record_contact
        self._change_user_id = change_user_id
        self._receive_cart = receive_cart
        self._reply_to_order = reply_to_order
        self._reply_to_payment = reply_to_payment
        self._receive_payer_number = receive_payer_number
        self._clock = clock

    async def handle(self, shop_id: UUID, message: InboundMessage) -> None:
        """Routes one inbound message.

        - `system`: a customer changed number, so their BSUID changed;
        - a button reply to one of our order messages: `ReplyToOrder`; to one of our payment
          messages (`payment:...`): `ReplyToPayment`;
        - anything else: records the customer; a cart (`order`) becomes an order, and a text
          may be the Mobile Money number a payment waits for (`ReceivePayerNumber`).
        """
        if message.type == "system":
            await self._system(shop_id, message)
            return

        payload = _reply_payload(message)
        if payload is not None:
            reply = (
                self._reply_to_payment if payload.startswith("payment:") else self._reply_to_order
            )
            handled = await reply.execute(
                shop_id=shop_id, payload=payload, context_message_id=message.context_message_id
            )
            # False when the button does not answer one of our order messages: then it is handled
            # like any other message below.
            if handled:
                return

        # Anything else comes from a customer: keep their record, which also tracks the
        # 24-hour window, up to date.
        customer_id = await self._record_contact.execute(
            shop_id=shop_id,
            user_id=message.sender_user_id,
            phone=_phone(message.sender_phone),
            name=message.profile_name,
            at=self._sent_at(message),
        )
        if message.type == "order":
            items = _cart_items(message.payload)
            if not items:
                return
            note = message.payload.get("text")
            await self._receive_cart.execute(
                shop_id=shop_id,
                cart=Cart(
                    message_id=message.message_id,
                    customer_id=customer_id,
                    items=items,
                    note=note[:1000] if isinstance(note, str) else "",
                ),
            )
        elif message.type == "text" and await self._payer_number(shop_id, customer_id, message):
            return
        else:
            log.info(
                "inbound_message_not_an_order",
                shop_id=str(shop_id),
                message_id=message.message_id,
                type=message.type,
            )

    async def _payer_number(
        self, shop_id: UUID, customer_id: UUID, message: InboundMessage
    ) -> bool:
        """True if the text was the number a payment of this customer waits for."""
        body = message.payload.get("body")
        if not isinstance(body, str):
            return False
        return await self._receive_payer_number.execute(
            shop_id=shop_id, customer_id=customer_id, body=body[:200]
        )

    async def _system(self, shop_id: UUID, message: InboundMessage) -> None:
        """Moves a customer to their new BSUID after `user_changed_number`."""
        previous, current = message.payload.get("previous_user_id"), message.payload.get("user_id")
        if isinstance(previous, str) and isinstance(current, str) and previous != current:
            await self._change_user_id.execute(
                shop_id=shop_id, previous_user_id=previous, user_id=current
            )

    def _sent_at(self, message: InboundMessage) -> datetime:
        """When the customer sent the message, as Meta stamped it; now if missing."""
        if message.timestamp:
            return datetime.fromtimestamp(message.timestamp, UTC)
        return self._clock.now()
