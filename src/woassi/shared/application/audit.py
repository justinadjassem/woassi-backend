"""Reading the audit log."""

from uuid import UUID

from woassi.shared.application.pagination import PageResult
from woassi.shared.application.ports import UnitOfWork
from woassi.shared.domain.audit import AuditEntry


class ListAuditEntries:
    """Lists a shop's audit entries, one page at a time."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, offset: int, limit: int) -> PageResult[AuditEntry]:
        async with self._uow as uow:
            entries = await uow.audit.list(shop_id, offset, limit)
            total = await uow.audit.count(shop_id)
        return PageResult(items=list(entries), total=total, offset=offset, limit=limit)
