"""Payments the merchant received outside Woassi: cash, or mobile money sent to them directly."""

from uuid import UUID

from woassi.modules.payments.application.ports import (
    DuplicatePayment,
    OrdersPort,
    PaymentsUnitOfWork,
)
from woassi.modules.payments.application.use_cases.requests import require_payable
from woassi.modules.payments.domain.errors import OrderAlreadyPaid
from woassi.modules.payments.domain.payment import Payment, PaymentStatus
from woassi.shared.application.ports import Clock
from woassi.shared.domain.values import Amount


class RecordManualPayment:
    """The merchant says the order is paid. Cancels its pending KPrimePay payment, audited."""

    def __init__(self, uow: PaymentsUnitOfWork, orders: OrdersPort, clock: Clock) -> None:
        self._uow = uow
        self._orders = orders
        self._clock = clock

    async def execute(
        self,
        *,
        shop_id: UUID,
        order_id: UUID,
        account_id: UUID,
        reference: str | None = None,
        note: str | None = None,
        ip: str | None = None,
    ) -> Payment:
        """Raises OrderNotFound, OrderNotPayable (not CONFIRMED) or OrderAlreadyPaid.

        KPrimePay has no way to cancel a checkout: if the customer still pays the old link,
        the worker logs `payment_received_after_cancellation`, and the merchant refunds.
        """
        order = require_payable(await self._orders.get(shop_id, order_id), order_id)
        now = self._clock.now()
        async with self._uow as uow:
            payments = await uow.payments.for_order(shop_id, order_id, lock=True)
            if any(p.status is PaymentStatus.PAID for p in payments):
                raise OrderAlreadyPaid("This order is already paid.")
            cancelled = []
            for pending in payments:
                if pending.is_pending:
                    pending.cancel(now)
                    await uow.payments.save(pending)
                    cancelled.append(str(pending.id))
            payment = Payment.manual(
                shop_id=shop_id,
                order_id=order_id,
                amount=Amount(order.total),  # always the whole total
                recorded_by=account_id,
                reference=reference,
                note=note,
                now=now,
            )
            try:
                await uow.payments.add(payment)
            except DuplicatePayment:
                raise OrderAlreadyPaid("This order is already paid.") from None
            await uow.audit.record(
                account_id=account_id,
                action="payment.recorded",
                target_type="payment",
                target_id=str(payment.id),
                shop_id=shop_id,
                details={
                    "order_id": str(order_id),
                    "number": order.number,
                    "amount": payment.amount.value,
                    "method": payment.method.value,
                    "cancelled_payments": cancelled,
                },
                ip=ip,
            )
            await uow.commit()
        return payment
