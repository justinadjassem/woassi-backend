"""Reading a shop's payments."""

from uuid import UUID

from woassi.modules.payments.application.ports import PaymentsUnitOfWork
from woassi.modules.payments.domain.errors import PaymentNotFound
from woassi.modules.payments.domain.payment import Payment, PaymentStatus
from woassi.shared.application.pagination import PageResult


class ListPayments:
    """A page of the shop's payments, newest first, by status and order if given."""

    def __init__(self, uow: PaymentsUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        shop_id: UUID,
        status: PaymentStatus | None = None,
        order_id: UUID | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PageResult[Payment]:
        async with self._uow as uow:
            items = await uow.payments.list(shop_id, status, order_id, offset, limit)
            total = await uow.payments.count(shop_id, status, order_id)
        return PageResult(items=list(items), total=total, offset=offset, limit=limit)


class GetPayment:
    """One payment of the shop."""

    def __init__(self, uow: PaymentsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, payment_id: UUID) -> Payment:
        """Raises PaymentNotFound if the shop has no such payment."""
        async with self._uow as uow:
            payment = await uow.payments.get(shop_id, payment_id)
        if payment is None:
            raise PaymentNotFound(f"Payment {payment_id} not found.")
        return payment
