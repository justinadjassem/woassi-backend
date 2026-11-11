"""Billing: invoices and credit notes, their numbering, tax totals and links

Revision ID: 0008
Revises: 0007

The shops' new tax settings (tax regime, VAT registration) live in the `invoice_details` JSONB
of `shop_settings`: no column to add, rows without them read as not VAT-registered.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CERTIFICATION_STATUSES = (
    "'NOT_REQUIRED', 'PENDING', 'SUBMITTED', 'CERTIFIED', 'REJECTED', 'FAILED_RETRYABLE'"
)
TOTALS_VALID = (
    "total_excl_tax >= 0 AND total_tax >= 0 AND total > 0 AND total = total_excl_tax + total_tax"
)
AMOUNTS_VALID = "amount_excl_tax >= 0 AND tax_amount >= 0 AND total = amount_excl_tax + tax_amount"
RETAINED = ("billing_documents", "billing_tax_totals", "billing_access_tokens")
# 10-year retention (art. 63 LPF): billing rows are never deleted, whatever the code does.
FORBID_DELETE = """
CREATE FUNCTION billing_forbid_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'billing rows are kept for 10 years and cannot be deleted';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "billing_counters",
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "document_type IN ('INVOICE', 'CREDIT_NOTE')",
            name=op.f("ck_billing_counters_document_type_valid"),
        ),
        sa.PrimaryKeyConstraint("shop_id", "document_type", name=op.f("pk_billing_counters")),
    )
    op.create_table(
        "billing_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("price_basis", sa.String(length=3), nullable=False),
        sa.Column("seller", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("buyer", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total_excl_tax", sa.BigInteger(), nullable=False),
        sa.Column("total_tax", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("order_number", sa.BigInteger(), nullable=True),
        sa.Column("payment_status", sa.String(length=10), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("payment_method", sa.String(length=20), nullable=True),
        sa.Column("payment_amount", sa.BigInteger(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_document_id", sa.Uuid(), nullable=True),
        sa.Column("original_number", sa.String(length=40), nullable=True),
        sa.Column("original_certified_number", sa.String(length=100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("refund_reference", sa.String(length=100), nullable=True),
        sa.Column("issued_by", sa.Uuid(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("credited_total", sa.BigInteger(), nullable=False),
        sa.Column("pdf_key", sa.String(length=300), nullable=True),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column("certification_status", sa.String(length=20), nullable=False),
        sa.Column("otr_certified_number", sa.String(length=100), nullable=True),
        sa.Column("otr_signature", sa.Text(), nullable=True),
        sa.Column("otr_qr_payload", sa.Text(), nullable=True),
        sa.Column("otr_certified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("otr_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("otr_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("certification_attempts", sa.Integer(), nullable=False),
        sa.Column("certification_error", sa.String(length=100), nullable=True),
        sa.Column("delivery_status", sa.String(length=10), nullable=False),
        sa.Column("delivery_message_id", sa.String(length=200), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"certification_status IN ({CERTIFICATION_STATUSES})",
            name=op.f("ck_billing_documents_certification_valid"),
        ),
        sa.CheckConstraint(
            "delivery_status IN ('PENDING', 'SENT', 'FAILED')",
            name=op.f("ck_billing_documents_delivery_valid"),
        ),
        sa.CheckConstraint(
            "document_type = 'INVOICE' OR original_document_id IS NOT NULL",
            name=op.f("ck_billing_documents_credit_note_has_invoice"),
        ),
        sa.CheckConstraint(
            "document_type IN ('INVOICE', 'CREDIT_NOTE')",
            name=op.f("ck_billing_documents_document_type_valid"),
        ),
        sa.CheckConstraint(
            "credited_total >= 0 AND credited_total <= total",
            name=op.f("ck_billing_documents_credit_valid"),
        ),
        sa.CheckConstraint(TOTALS_VALID, name=op.f("ck_billing_documents_totals_valid")),
        sa.ForeignKeyConstraint(
            ["original_document_id"],
            ["billing_documents.id"],
            name=op.f("fk_billing_documents_original_document_id_billing_documents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_documents")),
        sa.UniqueConstraint(
            "shop_id",
            "document_type",
            "sequence",
            name=op.f("uq_billing_documents_shop_id_document_type_sequence"),
        ),
        sa.UniqueConstraint("shop_id", "number", name=op.f("uq_billing_documents_shop_id_number")),
    )
    op.create_index(
        "ix_billing_documents_original_document_id",
        "billing_documents",
        ["original_document_id"],
        unique=False,
    )
    op.create_index(
        "ix_billing_documents_shop_type_issued",
        "billing_documents",
        ["shop_id", "document_type", "issued_at"],
        unique=False,
    )
    op.create_index(
        "uq_billing_documents_invoice_order",
        "billing_documents",
        ["shop_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("document_type = 'INVOICE'"),
    )
    op.create_table(
        "billing_access_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["billing_documents.id"],
            name=op.f("fk_billing_access_tokens_document_id_billing_documents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_billing_access_tokens")),
    )
    op.create_index(
        op.f("ix_billing_access_tokens_document_id"),
        "billing_access_tokens",
        ["document_id"],
        unique=False,
    )
    op.create_table(
        "billing_tax_totals",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("tax_code", sa.String(length=20), nullable=False),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("amount_excl_tax", sa.BigInteger(), nullable=False),
        sa.Column("tax_amount", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "tax_code IN ('VAT_18', 'NOT_SUBJECT', 'EXEMPT')",
            name=op.f("ck_billing_tax_totals_tax_code_valid"),
        ),
        sa.CheckConstraint(AMOUNTS_VALID, name=op.f("ck_billing_tax_totals_amounts_valid")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["billing_documents.id"],
            name=op.f("fk_billing_tax_totals_document_id_billing_documents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("document_id", "tax_code", name=op.f("pk_billing_tax_totals")),
    )
    op.execute(FORBID_DELETE)
    for table in RETAINED:
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION billing_forbid_delete()"
        )


def downgrade() -> None:
    for table in RETAINED:
        op.execute(f"DROP TRIGGER {table}_no_delete ON {table}")
    op.execute("DROP FUNCTION billing_forbid_delete()")
    op.drop_table("billing_tax_totals")
    op.drop_index(op.f("ix_billing_access_tokens_document_id"), table_name="billing_access_tokens")
    op.drop_table("billing_access_tokens")
    op.drop_index(
        "uq_billing_documents_invoice_order",
        table_name="billing_documents",
        postgresql_where=sa.text("document_type = 'INVOICE'"),
    )
    op.drop_index("ix_billing_documents_shop_type_issued", table_name="billing_documents")
    op.drop_index("ix_billing_documents_original_document_id", table_name="billing_documents")
    op.drop_table("billing_documents")
    op.drop_table("billing_counters")
