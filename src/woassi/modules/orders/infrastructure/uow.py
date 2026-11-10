"""Unit of Work of the orders module."""

from woassi.modules.orders.application.ports import OrderRepository
from woassi.modules.orders.infrastructure.repository import SqlAlchemyOrderRepository
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork


class SqlAlchemyOrdersUoW(SqlAlchemyUnitOfWork):
    """Gives the order repository, on this Unit of Work's session."""

    @property
    def orders(self) -> OrderRepository:
        return SqlAlchemyOrderRepository(self._active_session(), self._track)
