"""SQLAlchemy models of the billing module. No FK to shops, orders or customers: modules stay
independent. Rows are never deleted (10-year retention, art. 63 LPF): the migration also
installs a trigger refusing DELETE on these tables.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from woassi.shared.infrastructure.db.base import Base

DOCUMENT_TYPES = "'INVOICE', 'CREDIT_NOTE'"
TAX_CODES = "'VAT_18', 'NOT_SUBJECT', 'EXEMPT'"
CERTIFICATION_STATUSES = (
    "'NOT_REQUIRED', 'PENDING', 'SUBMITTED', 'CERTIFIED', 'REJECTED', 'FAILED_RETRYABLE'"
)
DELIVERY_STATUSES = "'PENDING', 'SENT', 'FAILED'"


class BillingCounterModel(Base):
    """Per-shop, per-type document numbers: 1, 2, 3... Handed out under a row lock, in the
    transaction that issues the document, so a rollback gives the number back: no gap.
    """

    __tablename__ = "billing_counters"
    __table_args__ = (
        CheckConstraint(f"document_type IN ({DOCUMENT_TYPES})", name="document_type_valid"),
    )

    shop_id: Mapped[UUID] = mapped_column(primary_key=True)
    document_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger)


class BillingDocumentModel(Base):
    """Row of `billing_documents`: an invoice or a credit note."""

    __tablename__ = "billing_documents"
    __table_args__ = (
        CheckConstraint(f"document_type IN ({DOCUMENT_TYPES})", name="document_type_valid"),
        CheckConstraint(
            f"certification_status IN ({CERTIFICATION_STATUSES})", name="certification_valid"
        ),
        CheckConstraint(f"delivery_status IN ({DELIVERY_STATUSES})", name="delivery_valid"),
        CheckConstraint(
            "total_excl_tax >= 0 AND total_tax >= 0 AND total > 0 "
            "AND total = total_excl_tax + total_tax",
            name="totals_valid",
        ),
        CheckConstraint("credited_total >= 0 AND credited_total <= total", name="credit_valid"),
        CheckConstraint(
            "document_type = 'INVOICE' OR original_document_id IS NOT NULL",
            name="credit_note_has_invoice",
        ),
        UniqueConstraint("shop_id", "document_type", "sequence"),
        UniqueConstraint("shop_id", "number"),
        # One invoice per order, whatever the race or the replay.
        Index(
            "uq_billing_documents_invoice_order",
            "shop_id",
            "order_id",
            unique=True,
            postgresql_where=text("document_type = 'INVOICE'"),
        ),
        Index("ix_billing_documents_shop_type_issued", "shop_id", "document_type", "issued_at"),
        Index("ix_billing_documents_original_document_id", "original_document_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    shop_id: Mapped[UUID]
    document_type: Mapped[str] = mapped_column(String(20))
    number: Mapped[str] = mapped_column(String(40))
    sequence: Mapped[int] = mapped_column(BigInteger)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    language: Mapped[str] = mapped_column(String(5))
    currency: Mapped[str] = mapped_column(String(3))
    price_basis: Mapped[str] = mapped_column(String(3))
    seller: Mapped[dict[str, Any]] = mapped_column(JSONB)
    buyer: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    total_excl_tax: Mapped[int] = mapped_column(BigInteger)
    total_tax: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[int] = mapped_column(BigInteger)
    customer_id: Mapped[UUID | None]
    order_id: Mapped[UUID | None]
    order_number: Mapped[int | None] = mapped_column(BigInteger)
    payment_status: Mapped[str | None] = mapped_column(String(10))
    payment_id: Mapped[UUID | None]
    payment_method: Mapped[str | None] = mapped_column(String(20))
    payment_amount: Mapped[int | None] = mapped_column(BigInteger)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    original_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("billing_documents.id", ondelete="RESTRICT")
    )
    original_number: Mapped[str | None] = mapped_column(String(40))
    original_certified_number: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(Text)
    refund_reference: Mapped[str | None] = mapped_column(String(100))
    issued_by: Mapped[UUID | None]
    content_sha256: Mapped[str] = mapped_column(String(64))
    credited_total: Mapped[int] = mapped_column(BigInteger)
    pdf_key: Mapped[str | None] = mapped_column(String(300))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    # OTR certified e-invoice: empty until a shop is in scope.
    certification_status: Mapped[str] = mapped_column(String(20))
    otr_certified_number: Mapped[str | None] = mapped_column(String(100))
    otr_signature: Mapped[str | None] = mapped_column(Text)
    otr_qr_payload: Mapped[str | None] = mapped_column(Text)
    otr_certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otr_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    otr_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    certification_attempts: Mapped[int] = mapped_column(Integer)
    certification_error: Mapped[str | None] = mapped_column(String(100))
    delivery_status: Mapped[str] = mapped_column(String(10))
    delivery_message_id: Mapped[str | None] = mapped_column(String(200))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BillingTaxTotalModel(Base):
    """Totals of a document per tax code, written once at issue: the input of tax reports."""

    __tablename__ = "billing_tax_totals"
    __table_args__ = (
        CheckConstraint(f"tax_code IN ({TAX_CODES})", name="tax_code_valid"),
        CheckConstraint(
            "amount_excl_tax >= 0 AND tax_amount >= 0 AND total = amount_excl_tax + tax_amount",
            name="amounts_valid",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("billing_documents.id", ondelete="RESTRICT"), primary_key=True
    )
    tax_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    rate_bp: Mapped[int] = mapped_column(Integer)
    amount_excl_tax: Mapped[int] = mapped_column(BigInteger)
    tax_amount: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[int] = mapped_column(BigInteger)


class BillingAccessTokenModel(Base):
    """Links of the `woassi_invoice_ready` template. Only the token's SHA-256 is stored."""

    __tablename__ = "billing_access_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("billing_documents.id", ondelete="RESTRICT"), index=True
    )
    shop_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
