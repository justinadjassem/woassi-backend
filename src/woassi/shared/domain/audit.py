"""Audit log entry: who did what, on which shop and target."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded action, never modified once written.

    `action` looks like `order.validated`; `target_type` and `target_id` name what it acted on,
    and `details` holds action-specific data.
    """

    id: UUID
    account_id: UUID
    shop_id: UUID | None
    action: str
    target_type: str
    target_id: str
    details: Mapping[str, Any]
    ip: str | None
    created_at: datetime
