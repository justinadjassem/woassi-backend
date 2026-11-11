"""Unit of Work of the billing module."""

from woassi.modules.billing.application.ports import BillingDocumentRepository
from woassi.modules.billing.infrastructure.repository import SqlAlchemyBillingDocuments
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork


class SqlAlchemyBillingUoW(SqlAlchemyUnitOfWork):
    """Gives the billing repository, on this Unit of Work's session."""

    @property
    def documents(self) -> BillingDocumentRepository:
        return SqlAlchemyBillingDocuments(self._active_session(), self._track)
