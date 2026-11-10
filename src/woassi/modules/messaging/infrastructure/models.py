"""SQLAlchemy models of the messaging module."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.infrastructure.db.base import Base


class MessageTemplateModel(Base):
    """Row of `message_templates`: one per shop, template name and language."""

    __tablename__ = "message_templates"

    shop_id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(512), primary_key=True)
    language: Mapped[str] = mapped_column(String(10), primary_key=True)
    waba_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20))
    meta_template_id: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboundMessageModel(Base):
    """Row of `outbound_messages`, keyed by Meta's message id (wamid)."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACCEPTED', 'SENT', 'DELIVERED', 'READ', 'FAILED')", name="status_valid"
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # No FK: modules stay independent.
    shop_id: Mapped[UUID] = mapped_column(index=True)
    recipient: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    error_code: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
