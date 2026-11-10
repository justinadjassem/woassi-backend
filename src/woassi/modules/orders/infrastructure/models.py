"""SQLAlchemy models of the orders module."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.infrastructure.db.base import Base

_STATUSES = (
    "'AWAITING_CUSTOMER', 'AWAITING_MERCHANT', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'EXPIRED'"
)


class OrderModel(Base):
    """Row of `orders`. Lines and deadlines are stored as JSON, as they were at order time."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("shop_id", "number"),
        # One order per cart message, whatever Meta or the worker replays.
        UniqueConstraint("shop_id", "source_message_id"),
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        Index("ix_orders_shop_id_created_at", "shop_id", "created_at"),
        Index(
            "ix_orders_next_deadline",
            "next_deadline",
            postgresql_where="next_deadline IS NOT NULL",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # No FK: modules stay independent.
    shop_id: Mapped[UUID]
    number: Mapped[int]
    customer_id: Mapped[UUID] = mapped_column(index=True)
    source_message_id: Mapped[str] = mapped_column(String(128))
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    total: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text)
    deadlines: Mapped[dict[str, int]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    awaiting_merchant_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Earliest of the next reminder and the expiry, while the order is open: what the cron scans.
    next_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminders_sent: Mapped[int]
    merchant_message_ids: Mapped[list[str]] = mapped_column(JSONB)
    offer_message_id: Mapped[str | None] = mapped_column(String(128))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_reason: Mapped[str | None] = mapped_column(String(30))
    decided_by: Mapped[str | None] = mapped_column(String(20))


class OrderCounterModel(Base):
    """Per-shop order numbers: 1, 2, 3… Handed out under a row lock, so never twice."""

    __tablename__ = "order_counters"

    shop_id: Mapped[UUID] = mapped_column(primary_key=True)
    last_number: Mapped[int]
