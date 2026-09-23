"""Deals radar: offers from the weekly folders and the refresh runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "deal_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("plan", JSONType, nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stats", JSONType, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_deal_runs"),
    )
    op.create_table(
        "deals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("retailer", sa.String(16), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("brand", sa.String(100), nullable=True),
        sa.Column("ean", sa.String(14), nullable=True),
        sa.Column("category", sa.String(40), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("original_price_cents", sa.Integer(), nullable=True),
        sa.Column("savings_pct", sa.Float(), nullable=True),
        sa.Column("savings_cents", sa.Integer(), nullable=True),
        sa.Column("promo_type", sa.String(20), nullable=True),
        sa.Column("promo_text", sa.String(200), nullable=True),
        sa.Column("buy_quantity", sa.Integer(), nullable=True),
        sa.Column("bundle_price_cents", sa.Integer(), nullable=True),
        sa.Column("unit_price_cents", sa.Integer(), nullable=True),
        sa.Column("unit_basis", sa.String(4), nullable=True),
        sa.Column("quantity_text", sa.String(60), nullable=True),
        sa.Column("loyalty_price_cents", sa.Integer(), nullable=True),
        sa.Column("loyalty_program", sa.String(40), nullable=True),
        sa.Column("in_store_only", sa.Boolean(), nullable=True),
        sa.Column("product_url", sa.String(500), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matched_product_id", sa.Integer(), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("relevance", sa.String(10), nullable=True),
        sa.Column("usual_price_cents", sa.Integer(), nullable=True),
        sa.Column("vs_usual_pct", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("size_unverified", sa.Boolean(), nullable=False),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["matched_product_id"], ["products.id"], name="fk_deals_matched_product_id_products", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deals"),
        sa.UniqueConstraint("source", "external_id", name="uq_deals_source_external"),
    )
    op.create_index("ix_deals_valid_until", "deals", ["valid_until"])


def downgrade() -> None:
    op.drop_index("ix_deals_valid_until", table_name="deals")
    op.drop_table("deals")
    op.drop_table("deal_runs")
