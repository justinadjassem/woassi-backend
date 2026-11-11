"""Unit of Work of the payments module."""

from woassi.modules.payments.application.ports import (
    Inbox,
    PaymentRepository,
    ProviderAccountRepository,
)
from woassi.modules.payments.infrastructure.repository import (
    SqlAlchemyPaymentRepository,
    SqlAlchemyPaymentsInbox,
    SqlAlchemyProviderAccounts,
)
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork


class SqlAlchemyPaymentsUoW(SqlAlchemyUnitOfWork):
    """Gives the payments repositories, all on this Unit of Work's session."""

    @property
    def payments(self) -> PaymentRepository:
        return SqlAlchemyPaymentRepository(self._active_session(), self._track)

    @property
    def provider_accounts(self) -> ProviderAccountRepository:
        return SqlAlchemyProviderAccounts(self._active_session())

    @property
    def inbox(self) -> Inbox:
        return SqlAlchemyPaymentsInbox(self._active_session())
