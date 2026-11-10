"""Unit of Work of the customers module."""

from woassi.modules.customers.application.ports import CustomerRepository
from woassi.modules.customers.infrastructure.repository import SqlAlchemyCustomerRepository
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork


class SqlAlchemyCustomersUoW(SqlAlchemyUnitOfWork):
    """Gives the customer repository, on this Unit of Work's session."""

    @property
    def customers(self) -> CustomerRepository:
        return SqlAlchemyCustomerRepository(self._active_session(), self._track)
