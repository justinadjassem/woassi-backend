"""Asking the customer to pay: a USSD push to their Mobile Money number, or a checkout link."""

from uuid import UUID

from woassi.modules.payments.application.ports import DuplicatePayment, PayableOrder
from woassi.modules.payments.application.use_cases.flow import CONFIRMED, PAID, PaymentFlow
from woassi.modules.payments.domain.errors import (
    NoPayerNumber,
    OrderAlreadyPaid,
    OrderNotFound,
    OrderNotPayable,
    ProviderNotConfigured,
    PushLimitReached,
)
from woassi.modules.payments.domain.mobile_money import togolese_mobile
from woassi.modules.payments.domain.payment import (
    Channel,
    Payment,
    PaymentStatus,
    check_provider_amount,
)
from woassi.shared.domain.values import Amount


def require_payable(order: PayableOrder | None, order_id: UUID) -> PayableOrder:
    """The order if it can be paid. Raises OrderNotFound, OrderAlreadyPaid or OrderNotPayable."""
    if order is None:
        raise OrderNotFound(f"Order {order_id} not found.")
    if order.status == PAID:
        raise OrderAlreadyPaid("This order is already paid.")
    if order.status != CONFIRMED:
        raise OrderNotPayable(f"Only a confirmed order can be paid; this one is {order.status}.")
    return order


class RequestPayment(PaymentFlow):
    """Asks the customer for the money.

    `channel=None` (an order was just confirmed, `payments.request_payment`): a USSD push to the
    customer's WhatsApp number if it is a Togolese mobile, else a question: which number will
    pay? From the dashboard: `CHECKOUT` sends the usable link (or a new one), `USSD_PUSH` pushes
    again to the last number. Never more than one pending payment per order.
    """

    async def execute(
        self,
        *,
        shop_id: UUID,
        order_id: UUID,
        channel: Channel | None = None,
        account_id: UUID | None = None,
        ip: str | None = None,
    ) -> Payment:
        """Raises ProviderNotConfigured, OrderNotFound, OrderNotPayable, OrderAlreadyPaid,
        AmountOutOfRange, NoPayerNumber or PushLimitReached. A KPrimePay failure does not
        raise: it shows on the attempt.
        """
        account = await self._account(shop_id)
        if account is None:
            raise ProviderNotConfigured("This shop has no KPrimePay account.")
        context = await self._context(shop_id, order_id)
        order = require_payable(context[0] if context else None, order_id)
        assert context is not None
        shop = context[1]
        amount = Amount(order.total)
        check_provider_amount(amount)

        payment, created = await self._pending_payment(shop_id, order, amount, account_id, ip)
        if channel is Channel.CHECKOUT:
            return await self._link(payment, account, order, shop)
        contact = await self._customers.contact(shop_id, order.customer_id)
        if channel is Channel.USSD_PUSH:
            payer = payment.payer or togolese_mobile(contact.phone)
            if payer is None:
                raise NoPayerNumber("No Togolese Mobile Money number is known for this payment.")
            if not payment.can_push:
                raise PushLimitReached("This payment already had 3 requests by phone.")
            return await self._push(payment, payer, account, order, shop)
        if not created:
            # A replayed confirmation: the customer was already asked.
            return payment
        payer = togolese_mobile(contact.phone)
        if payer is None:
            # A WhatsApp username or a foreign number: ask which number will pay.
            await self._ask_number(payment, order, shop)
            return payment
        return await self._push(payment, payer, account, order, shop)

    async def _pending_payment(
        self,
        shop_id: UUID,
        order: PayableOrder,
        amount: Amount,
        account_id: UUID | None,
        ip: str | None,
    ) -> tuple[Payment, bool]:
        """The order's pending payment, created if there is none; True if created."""
        async with self._uow as uow:
            payments = await uow.payments.for_order(shop_id, order.order_id, lock=True)
            if any(p.status is PaymentStatus.PAID for p in payments):
                raise OrderAlreadyPaid("This order is already paid.")
            pending = next((p for p in payments if p.is_pending), None)
            if pending is not None:
                return pending, False
            payment = Payment.request(
                shop_id=shop_id,
                order_id=order.order_id,
                customer_id=order.customer_id,
                amount=amount,
                now=self._clock.now(),
            )
            try:
                await uow.payments.add(payment)
            except DuplicatePayment:
                # A concurrent request created it first: use that one.
                await uow.rollback()
                payments = await uow.payments.for_order(shop_id, order.order_id)
                existing = next((p for p in payments if p.is_pending), None)
                if existing is None:
                    raise OrderAlreadyPaid("This order is already paid.") from None
                return existing, False
            if account_id is not None:
                await uow.audit.record(
                    account_id=account_id,
                    action="payment.requested",
                    target_type="payment",
                    target_id=str(payment.id),
                    shop_id=shop_id,
                    details={"order_id": str(order.order_id), "amount": amount.value},
                    ip=ip,
                )
            await uow.commit()
        return payment, True
