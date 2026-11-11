"""SQLAlchemy models of the catalog module."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from woassi.shared.infrastructure.db.base import Base


class ProductModel(Base):
    """Row of `products`. Photos load with the product (`selectin`)."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("shop_id", "retailer_id"),
        CheckConstraint("price >= 0", name="price_positive"),
        CheckConstraint("stock >= 0", name="stock_positive"),
        CheckConstraint(
            "meta_sync_status IN ('PENDING', 'SYNCED', 'FAILED')", name="meta_sync_status_valid"
        ),
        # The retry cron looks for due products across every shop.
        Index("ix_products_meta_sync_next_at", "meta_sync_next_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # No FK: modules stay independent, and shops are archived, never deleted.
    shop_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[int] = mapped_column(BigInteger)
    stock: Mapped[int]
    retailer_id: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    active: Mapped[bool]
    # Sync with Meta's catalog: see `MetaSync` in the domain.
    meta_sync_status: Mapped[str] = mapped_column(String(20))
    meta_sync_version: Mapped[int]
    meta_sync_attempts: Mapped[int]
    meta_sync_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta_sync_error: Mapped[str | None] = mapped_column(String(50))
    meta_published: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    photos: Mapped[list["ProductPhotoModel"]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ProductPhotoModel.position",
    )


class ProductPhotoModel(Base):
    """Row of `product_photos`. `variants` maps each width (as a string) to its key."""

    __tablename__ = "product_photos"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'READY', 'REJECTED')", name="status_valid"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    original_key: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    position: Mapped[int]
    variants: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
