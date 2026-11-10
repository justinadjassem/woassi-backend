"""SQLAlchemy implementation of the audit log."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.application.ports import Clock
from woassi.shared.domain.audit import AuditEntry
from woassi.shared.infrastructure.db.base import Base


class AuditEntryModel(Base):
    """Row of the append-only `audit_log` table."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_shop_id_created_at", "shop_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # No FK: modules stay independent, and shops and accounts are never physically deleted.
    account_id: Mapped[UUID]
    shop_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _to_domain(model: AuditEntryModel) -> AuditEntry:
    """Converts a row to a domain `AuditEntry`."""
    return AuditEntry(
        id=model.id,
        account_id=model.account_id,
        shop_id=model.shop_id,
        action=model.action,
        target_type=model.target_type,
        target_id=model.target_id,
        details=model.details,
        ip=model.ip,
        created_at=model.created_at,
    )


class SqlAlchemyAuditLog:
    """Uses the caller's session: an entry is committed or rolled back with its action."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def record(
        self,
        *,
        account_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        shop_id: UUID | None = None,
        details: Mapping[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        """Adds the entry to the session and flushes, so a database error shows here."""
        self._session.add(
            AuditEntryModel(
                id=uuid4(),
                account_id=account_id,
                shop_id=shop_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=dict(details or {}),
                ip=ip,
                created_at=self._clock.now(),
            )
        )
        await self._session.flush()

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[AuditEntry]:
        """Entries of `shop_id`, newest first; the id breaks ties for a stable order."""
        models = await self._session.scalars(
            select(AuditEntryModel)
            .where(AuditEntryModel.shop_id == shop_id)
            .order_by(AuditEntryModel.created_at.desc(), AuditEntryModel.id)
            .offset(offset)
            .limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def count(self, shop_id: UUID) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(AuditEntryModel)
            .where(AuditEntryModel.shop_id == shop_id)
        )
        return total or 0
