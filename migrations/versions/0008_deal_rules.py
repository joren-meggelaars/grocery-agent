"""Deal rules: what you told the radar is not interesting, and whether an offer is a house brand

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("private_label", sa.Boolean(), nullable=True))
    op.create_table(
        "deal_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_deal_rules"),
        sa.UniqueConstraint("kind", "value", name="uq_deal_rules_kind_value"),
    )


def downgrade() -> None:
    op.drop_table("deal_rules")
    op.drop_column("deals", "private_label")
