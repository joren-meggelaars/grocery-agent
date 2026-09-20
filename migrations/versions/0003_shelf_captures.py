"""Phase 2: barcodes, Open Food Facts cache, shelf captures, price observations

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "product_eans",
        sa.Column("ean", sa.String(14), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_product_eans_product_id_products", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("ean", name="pk_product_eans"),
    )
    op.create_index("ix_product_eans_product_id", "product_eans", ["product_id"])

    op.create_table(
        "off_cache",
        sa.Column("ean", sa.String(14), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column("payload", JSONType, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ean", name="pk_off_cache"),
    )

    op.create_table(
        "shelf_captures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_uuid", sa.String(36), nullable=False),
        sa.Column("ean", sa.String(14), nullable=True),
        sa.Column("store_id", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("extraction_id", sa.Integer(), nullable=True),
        sa.Column("off_name", sa.String(300), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_name", sa.String(200), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("effective_price_cents", sa.Integer(), nullable=True),
        sa.Column("unit_price_cents", sa.Integer(), nullable=True),
        sa.Column("unit_basis", sa.String(4), nullable=True),
        sa.Column("promo_kind", sa.String(16), nullable=True),
        sa.Column("promo_text", sa.String(200), nullable=True),
        sa.Column("requires_card", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_shelf_captures_store_id_stores"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_shelf_captures_file_id_files"),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["extractions.id"], name="fk_shelf_captures_extraction_id_extractions"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_shelf_captures_product_id_products"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], name="fk_shelf_captures_category_id_categories"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shelf_captures"),
        sa.UniqueConstraint("client_uuid", name="uq_shelf_captures_client_uuid"),
    )
    op.create_index("ix_shelf_captures_ean", "shelf_captures", ["ean"])
    op.create_index("ix_shelf_captures_status", "shelf_captures", ["status"])

    op.create_table(
        "price_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("ean", sa.String(14), nullable=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=True),
        sa.Column("unit_basis", sa.String(4), nullable=True),
        sa.Column("is_promo", sa.Boolean(), nullable=False),
        sa.Column("promo_text", sa.String(200), nullable=True),
        sa.Column("requires_card", sa.Boolean(), nullable=False),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("source_ref_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_price_observations_product_id_products", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_price_observations_store_id_stores"),
        sa.PrimaryKeyConstraint("id", name="pk_price_observations"),
    )
    op.create_index(
        "ix_price_observations_product_observed", "price_observations", ["product_id", "observed_on"]
    )


def downgrade() -> None:
    op.drop_table("price_observations")
    op.drop_table("shelf_captures")
    op.drop_table("off_cache")
    op.drop_table("product_eans")
