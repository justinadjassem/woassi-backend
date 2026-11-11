"""Payments, KPrimePay accounts of shops, and the PAID status of orders

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDER_STATUSES = (
    "'AWAITING_CUSTOMER', 'AWAITING_MERCHANT', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'EXPIRED'"
)
ORDER_STATUSES_WITH_PAID = (
    "'AWAITING_CUSTOMER', 'AWAITING_MERCHANT', 'CONFIRMED', 'PAID', 'REJECTED', 'CANCELLED', "
    "'EXPIRED'"
)
PAYMENT_STATUSES = "'PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELLED'"


def upgrade() -> None:
    op.add_column("orders", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_orders_status_valid"), "orders", type_="check")
    op.create_check_constraint(
        op.f("ck_orders_status_valid"), "orders", f"status IN ({ORDER_STATUSES_WITH_PAID})"
    )

    op.create_table(
        "payment_provider_accounts",
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encrypted_callback_token", sa.Text(), nullable=False),
        sa.Column("callback_token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('sandbox', 'live')", name=op.f("ck_payment_provider_accounts_mode_valid")
        ),
        sa.PrimaryKeyConstraint("shop_id", name=op.f("pk_payment_provider_accounts")),
        sa.UniqueConstraint(
            "callback_token_hash",
            name=op.f("uq_payment_provider_accounts_callback_token_hash"),
        ),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider_reference", sa.String(length=100), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=100), nullable=True),
        sa.Column("manual_reference", sa.String(length=100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checks", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.CheckConstraint(
            "method IN ('kprimepay', 'manual')", name=op.f("ck_payments_method_valid")
        ),
        sa.CheckConstraint(
            f"status IN ({PAYMENT_STATUSES})", name=op.f("ck_payments_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(op.f("ix_payments_order_id"), "payments", ["order_id"], unique=False)
    op.create_index(
        "ix_payments_shop_id_created_at", "payments", ["shop_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_payments_next_check_at",
        "payments",
        ["next_check_at"],
        unique=False,
        postgresql_where=sa.text("next_check_at IS NOT NULL"),
    )
    # At most one PAID and one PENDING payment per order.
    op.create_index(
        "uq_payments_paid_order",
        "payments",
        ["shop_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PAID'"),
    )
    op.create_index(
        "uq_payments_pending_order",
        "payments",
        ["shop_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_payments_pending_order", table_name="payments")
    op.drop_index("uq_payments_paid_order", table_name="payments")
    op.drop_index("ix_payments_next_check_at", table_name="payments")
    op.drop_index("ix_payments_shop_id_created_at", table_name="payments")
    op.drop_index(op.f("ix_payments_order_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_table("payment_provider_accounts")
    op.drop_constraint(op.f("ck_orders_status_valid"), "orders", type_="check")
    op.create_check_constraint(
        op.f("ck_orders_status_valid"), "orders", f"status IN ({ORDER_STATUSES})"
    )
    op.drop_column("orders", "paid_at")
