"""Shopping list: items to buy, matched to a product where possible, with price advice at read time

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shopping_list_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("raw_name", sa.String(200), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bought_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_shopping_list_items_product_id_products", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shopping_list_items"),
    )


def downgrade() -> None:
    op.drop_table("shopping_list_items")
