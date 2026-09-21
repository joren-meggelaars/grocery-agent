"""Pack content per product, so a pack price can be compared per kg or l

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("pack_content", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("pack_basis", sa.String(2), nullable=True))
    op.add_column("products", sa.Column("sold_per_piece", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("products", "sold_per_piece")
    op.drop_column("products", "pack_basis")
    op.drop_column("products", "pack_content")
