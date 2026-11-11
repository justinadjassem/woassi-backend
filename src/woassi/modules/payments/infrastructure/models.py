"""SQLAlchemy models of the payments module. No FK to shops or orders: modules stay
independent.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.infrastructure.db.base import Base

_STATUSES = "'PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELLED'"
_METHODS = "'kprimepay', 'manual'"
_CHANNELS = "'ussd_push', 'checkout'"
_OPERATORS = "'MIXX-YAS-TG', 'MOOV-MONEY-TG'"


class ProviderAccountModel(Base):
    """Row of `payment_provider_accounts`: one KPrimePay account per shop."""

    __tablename__ = "payment_provider_accounts"
    __table_args__ = (CheckConstraint("mode IN ('sandbox', 'live')", name="mode_valid"),)

    shop_id: Mapped[UUID] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    # Both encrypted with SecretCipher: never in clear text.
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    encrypted_callback_token: Mapped[str] = mapped_column(Text)
    callback_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaymentModel(Base):
    """Row of `payments`."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint(f"method IN ({_METHODS})", name="method_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # At most one PAID payment per order, whatever the race.
        Index(
            "uq_payments_paid_order",
            "shop_id",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'PAID'"),
        ),
        # At most one pending link per order: a repeated request reuses it.
        Index(
            "uq_payments_pending_order",
            "shop_id",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_payments_shop_id_created_at", "shop_id", "created_at"),
        Index(
            "ix_payments_awaiting_payer",
            "shop_id",
            "customer_id",
            postgresql_where=text("awaiting_payer"),
        ),
        Index(
            "ix_payments_next_check_at",
            "next_check_at",
            postgresql_where=text("next_check_at IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    shop_id: Mapped[UUID]
    order_id: Mapped[UUID] = mapped_column(index=True)
    customer_id: Mapped[UUID | None]
    amount: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(100))
    manual_reference: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[UUID | None]
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checks: Mapped[int]
    # Personal data: the last number a push went to (E.164).
    payer_phone: Mapped[str | None] = mapped_column(String(16))
    awaiting_payer: Mapped[bool]
    prompt_message_ids: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaymentAttemptModel(Base):
    """Row of `payment_attempts`: one KPrimePay transaction (a push or a checkout).

    `id` is KPrimePay's `transaction_id` and the request's Idempotency-Key.
    """

    __tablename__ = "payment_attempts"
    __table_args__ = (
        CheckConstraint(f"channel IN ({_CHANNELS})", name="channel_valid"),
        CheckConstraint(f"operator IN ({_OPERATORS})", name="operator_valid"),
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # Same module: a foreign key is fine here.
    payment_id: Mapped[UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(20))
    operator: Mapped[str | None] = mapped_column(String(20))
    payer_phone: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_reference: Mapped[str | None] = mapped_column(String(100))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
