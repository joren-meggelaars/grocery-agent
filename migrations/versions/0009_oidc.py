"""Sign-in through Authentik: link a local user to an OIDC subject

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Batch mode: SQLite (used by the test suite) cannot ALTER a table to add a constraint directly.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("oidc_sub", sa.String(255), nullable=True))
        batch.create_unique_constraint("uq_users_oidc_sub", ["oidc_sub"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_oidc_sub", type_="unique")
        batch.drop_column("oidc_sub")
