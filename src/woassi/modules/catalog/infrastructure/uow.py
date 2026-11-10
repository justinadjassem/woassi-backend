"""Unit of Work of the catalog module."""

from woassi.modules.catalog.application.ports import ProductRepository
from woassi.modules.catalog.infrastructure.repository import SqlAlchemyProductRepository
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork


class SqlAlchemyCatalogUoW(SqlAlchemyUnitOfWork):
    """Gives the product repository, on this Unit of Work's session."""

    @property
    def products(self) -> ProductRepository:
        return SqlAlchemyProductRepository(self._active_session(), self._track)
