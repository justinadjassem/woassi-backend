"""SQLAlchemy models of the customers module."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.infrastructure.db.base import Base


class CustomerModel(Base):
    """Row of `customers`. A BSUID is unique per shop; the phone is indexed for lookups."""

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("shop_id", "user_id"),
        Index("ix_customers_shop_id_phone", "shop_id", "phone"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # No FK: modules stay independent.
    shop_id: Mapped[UUID]
    user_id: Mapped[str | None] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
