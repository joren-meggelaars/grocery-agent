"""Phase 4: cupboard (products at home) and scanned barcodes waiting for a name

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cupboard_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("heavy_use", sa.Boolean(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_cupboard_items_product_id_products", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cupboard_items"),
        sa.UniqueConstraint("product_id", name="uq_cupboard_items_product_id"),
    )
    op.create_table(
        "cupboard_scans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ean", sa.String(14), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("off_name", sa.String(300), nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_cupboard_scans"),
        sa.UniqueConstraint("ean", name="uq_cupboard_scans_ean"),
    )


def downgrade() -> None:
    op.drop_table("cupboard_scans")
    op.drop_table("cupboard_items")
