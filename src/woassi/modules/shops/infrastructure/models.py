"""SQLAlchemy models of the shops module. The aggregate's parts load with the shop."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from woassi.shared.infrastructure.db.base import Base


class ShopSettingsModel(Base):
    """Row of `shop_settings`, one per shop."""

    __tablename__ = "shop_settings"

    shop_id: Mapped[UUID] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), primary_key=True
    )
    notification_number: Mapped[str | None] = mapped_column(String(16))
    payment_modes: Mapped[list[str]] = mapped_column(JSONB)
    reminder_1_minutes: Mapped[int]
    reminder_2_minutes: Mapped[int]
    expiration_hours: Mapped[int]
    welcome_message: Mapped[str] = mapped_column(Text)
    invoice_details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    invoice_prefix: Mapped[str] = mapped_column(String(10))
    language: Mapped[str] = mapped_column(String(5))
    validation_mode: Mapped[str] = mapped_column(String(20))
    auto_validation_max_amount: Mapped[int | None] = mapped_column(BigInteger)


class WhatsAppAccountModel(Base):
    """Row of `whatsapp_accounts`, at most one per shop. The token is encrypted."""

    __tablename__ = "whatsapp_accounts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'CONNECTED', 'ERROR', 'DISCONNECTED')", name="status_valid"
        ),
    )

    shop_id: Mapped[UUID] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), primary_key=True
    )
    waba_id: Mapped[str] = mapped_column(String(64))
    # Webhook routing key. Unique across the platform; NULL until Meta has confirmed it.
    phone_number_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_phone_number: Mapped[str | None] = mapped_column(String(32))
    verified_name: Mapped[str | None] = mapped_column(String(255))
    catalog_id: Mapped[str | None] = mapped_column(String(64))
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(String(500))


class ReceivingAccountModel(Base):
    """Row of `receiving_accounts`, at most one per shop."""

    __tablename__ = "receiving_accounts"
    __table_args__ = (CheckConstraint("operator IN ('MIXX', 'MOOV')", name="operator_valid"),)

    shop_id: Mapped[UUID] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), primary_key=True
    )
    operator: Mapped[str] = mapped_column(String(10))
    number: Mapped[str] = mapped_column(String(16))
    holder_name: Mapped[str] = mapped_column(String(200))
    encrypted_merchant_id: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool]


class ShopModel(Base):
    """Row of `shops`. An archived shop has `archived_at` set and is never deleted."""

    __tablename__ = "shops"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'SUSPENDED')", name="status_valid"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # No FK: modules stay independent, and accounts are never physically deleted.
    account_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    category: Mapped[str] = mapped_column(String(100))
    city: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(300))
    logo_key: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    settings: Mapped[ShopSettingsModel] = relationship(
        cascade="all, delete-orphan", lazy="selectin", single_parent=True
    )
    whatsapp: Mapped[WhatsAppAccountModel | None] = relationship(
        cascade="all, delete-orphan", lazy="selectin", single_parent=True
    )
    receiving_account: Mapped[ReceivingAccountModel | None] = relationship(
        cascade="all, delete-orphan", lazy="selectin", single_parent=True
    )
