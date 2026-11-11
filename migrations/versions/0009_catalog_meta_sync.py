"""Sync state of each product with the Meta catalog of the shop's WhatsApp number

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing products were never sent to Meta: PENDING, and due now for the retry cron.
    op.add_column(
        "products",
        sa.Column(
            "meta_sync_status", sa.String(length=20), server_default="PENDING", nullable=False
        ),
    )
    op.add_column(
        "products",
        sa.Column("meta_sync_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "products",
        sa.Column("meta_sync_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "products", sa.Column("meta_sync_next_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "products", sa.Column("meta_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("products", sa.Column("meta_sync_error", sa.String(length=50), nullable=True))
    op.add_column(
        "products",
        sa.Column("meta_published", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE products SET meta_sync_next_at = now()")
    for column in ("meta_sync_status", "meta_sync_version", "meta_sync_attempts", "meta_published"):
        op.alter_column("products", column, server_default=None)

    op.create_check_constraint(
        op.f("ck_products_meta_sync_status_valid"),
        "products",
        "meta_sync_status IN ('PENDING', 'SYNCED', 'FAILED')",
    )
    op.create_index(op.f("ix_products_meta_sync_next_at"), "products", ["meta_sync_next_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_products_meta_sync_next_at"), table_name="products")
    op.drop_constraint(op.f("ck_products_meta_sync_status_valid"), "products", type_="check")
    for column in (
        "meta_published",
        "meta_sync_error",
        "meta_synced_at",
        "meta_sync_next_at",
        "meta_sync_attempts",
        "meta_sync_version",
        "meta_sync_status",
    ):
        op.drop_column("products", column)
