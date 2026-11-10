"""Read-only access to a shop's audit log."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from woassi.shared.api.deps import SharedDep, get_authorized_shop
from woassi.shared.api.pagination import Limit, Offset, Page
from woassi.shared.domain.audit import AuditEntry

router = APIRouter(
    prefix="/shops/{shop_id}/audit-log",
    tags=["audit"],
    dependencies=[Depends(get_authorized_shop)],
)


class AuditEntryResponse(BaseModel):
    """One audit log entry, as returned by the API."""

    id: UUID
    account_id: UUID
    shop_id: UUID | None
    action: str
    target_type: str
    target_id: str
    details: Mapping[str, Any]
    ip: str | None
    created_at: datetime

    @classmethod
    def from_entry(cls, entry: AuditEntry) -> "AuditEntryResponse":
        """Builds the response from a domain entry."""
        return cls(
            id=entry.id,
            account_id=entry.account_id,
            shop_id=entry.shop_id,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            details=entry.details,
            ip=entry.ip,
            created_at=entry.created_at,
        )


@router.get("")
async def list_audit_entries(
    shop_id: UUID, shared: SharedDep, offset: Offset = 0, limit: Limit = 50
) -> Page[AuditEntryResponse]:
    """Lists the shop's audit entries, newest first."""
    result = await shared.list_audit_entries().execute(shop_id=shop_id, offset=offset, limit=limit)
    return Page[AuditEntryResponse].from_result(result, AuditEntryResponse.from_entry)
