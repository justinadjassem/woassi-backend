"""The customer's answers on WhatsApp: a tapped button, or the number that will pay."""

import re
from uuid import UUID

import structlog

from woassi.modules.payments.application.ports import PayableOrder, ShopInfo, Text
from woassi.modules.payments.application.texts import language_of, text
from woassi.modules.payments.application.use_cases.flow import (
    CONFIRMED,
    GET_LINK,
    OTHER_NUMBER,
    PaymentFlow,
)
from woassi.modules.payments.domain.mobile_money import togolese_mobile
from woassi.modules.payments.domain.payment import Payment
from woassi.modules.payments.domain.provider_account import ProviderAccount
from woassi.shared.domain.errors import InvalidData

log = structlog.get_logger()

# Something that looks like a phone number inside a free text.
_NUMBER = re.compile(r"\+?\d[\d .\-()]{5,}\d")
# Fewer digits: an ordinary message, which follows the usual path.
MIN_DIGITS = 4


class ReplyToPayment(PaymentFlow):
    """A payment button: 'another number' or 'payment link'."""

    async def execute(self, *, shop_id: UUID, payload: str, context_message_id: str | None) -> bool:
        """False for a reply that does not apply: unknown, stale, or not an answer to us."""
        parts = payload.split(":")
        if len(parts) != 3 or parts[0] != "payment" or parts[1] not in (OTHER_NUMBER, GET_LINK):
            return False
        try:
            payment_id = UUID(parts[2])
        except ValueError:
            return False
        async with self._uow as uow:
            payment = await uow.payments.get(shop_id, payment_id)
        # Only a reply to a message we sent for this payment counts: anyone can type a payload.
        if payment is None or not payment.is_pending or not payment.trusts(context_message_id):
            return False
        prepared = await self._prepare(payment)
        if prepared is None:
            return False
        account, order, shop = prepared
        if parts[1] == OTHER_NUMBER and payment.can_push:
            await self._ask_number(payment, order, shop)
        else:
            await self._link(payment, account, order, shop)
        return True

    async def _prepare(
        self, payment: Payment
    ) -> tuple[ProviderAccount, PayableOrder, ShopInfo] | None:
        account = await self._account(payment.shop_id)
        context = await self._context(payment.shop_id, payment.order_id)
        if account is None or context is None or context[0].status != CONFIRMED:
            return None
        return account, *context


class ReceivePayerNumber(PaymentFlow):
    """The customer typed the Mobile Money number that will pay."""

    async def execute(self, *, shop_id: UUID, customer_id: UUID, body: str) -> bool:
        """True if the message was taken as a payer number (valid or not).

        Only while this customer has a payment waiting for a number in this shop, and only for
        a message with digits in it: anything else follows the usual path.
        """
        if sum(c.isdigit() for c in body) < MIN_DIGITS:
            return False
        async with self._uow as uow:
            payment = await uow.payments.awaiting_payer(shop_id, customer_id)
        if payment is None:
            return False
        account = await self._account(shop_id)
        context = await self._context(shop_id, payment.order_id)
        if account is None or context is None or context[0].status != CONFIRMED:
            return False
        order, shop = context

        payer = None
        found = _NUMBER.search(body)
        if found is not None:
            try:
                payer = togolese_mobile(self._phones.normalize(found.group()).value)
            except InvalidData:
                payer = None
        if payer is None:
            log.info("payer_number_invalid", payment_id=str(payment.id))
            await self._notify(
                payment,
                order,
                Text(text(language_of(shop.language), "invalid_number")),
                None,
                kind="payment.invalid_number",
            )
            return True
        await self._push(payment, payer, account, order, shop, announce="push_sent")
        return True
