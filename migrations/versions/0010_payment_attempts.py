"""Payment attempts (USSD push or checkout), payer numbers, and payment prompts

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELLED'"


def upgrade() -> None:
    op.add_column("payments", sa.Column("customer_id", sa.Uuid(), nullable=True))
    op.add_column("payments", sa.Column("payer_phone", sa.String(length=16), nullable=True))
    op.add_column(
        "payments",
        sa.Column("awaiting_payer", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "payments",
        sa.Column(
            "prompt_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("payments", "awaiting_payer", server_default=None)
    op.alter_column("payments", "prompt_message_ids", server_default=None)
    op.create_index(
        "ix_payments_awaiting_payer",
        "payments",
        ["shop_id", "customer_id"],
        unique=False,
        postgresql_where=sa.text("awaiting_payer"),
    )

    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("operator", sa.String(length=20), nullable=True),
        sa.Column("payer_phone", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_reference", sa.String(length=100), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel IN ('ussd_push', 'checkout')", name=op.f("ck_payment_attempts_channel_valid")
        ),
        sa.CheckConstraint(
            "operator IN ('MIXX-YAS-TG', 'MOOV-MONEY-TG')",
            name=op.f("ck_payment_attempts_operator_valid"),
        ),
        sa.CheckConstraint(
            f"status IN ({STATUSES})", name=op.f("ck_payment_attempts_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_payment_attempts_payment_id_payments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_attempts")),
    )
    op.create_index(
        op.f("ix_payment_attempts_payment_id"), "payment_attempts", ["payment_id"], unique=False
    )

    # Every existing KPrimePay payment was one checkout whose transaction id is the payment id:
    # it becomes an attempt with that same id, so webhooks and queued checks still find it.
    op.execute(
        """
        INSERT INTO payment_attempts (
            id, payment_id, channel, status, submitted_at, provider_reference, checkout_url,
            expires_at, failure_reason, created_at, updated_at
        )
        SELECT id, id, 'checkout', status,
               CASE WHEN checkout_url IS NOT NULL THEN updated_at END,
               provider_reference, checkout_url, expires_at, failure_reason,
               created_at, updated_at
        FROM payments
        WHERE method = 'kprimepay'
        """
    )
    op.drop_column("payments", "checkout_url")
    op.drop_column("payments", "provider_reference")


def downgrade() -> None:
    op.add_column("payments", sa.Column("provider_reference", sa.String(length=100), nullable=True))
    op.add_column("payments", sa.Column("checkout_url", sa.Text(), nullable=True))
    # Keeps the latest checkout of each payment; push attempts are lost.
    op.execute(
        """
        UPDATE payments p
        SET checkout_url = a.checkout_url, provider_reference = a.provider_reference
        FROM (
            SELECT DISTINCT ON (payment_id) payment_id, checkout_url, provider_reference
            FROM payment_attempts
            WHERE channel = 'checkout'
            ORDER BY payment_id, created_at DESC
        ) a
        WHERE a.payment_id = p.id
        """
    )
    op.drop_index(op.f("ix_payment_attempts_payment_id"), table_name="payment_attempts")
    op.drop_table("payment_attempts")
    op.drop_index("ix_payments_awaiting_payer", table_name="payments")
    op.drop_column("payments", "prompt_message_ids")
    op.drop_column("payments", "awaiting_payer")
    op.drop_column("payments", "payer_phone")
    op.drop_column("payments", "customer_id")
