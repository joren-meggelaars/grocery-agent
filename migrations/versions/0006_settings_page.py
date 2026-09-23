"""Settings page: no schema change, just the spending cycle's first override

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))


def upgrade() -> None:
    # The spending cycle now starts on payday (the 23rd, set on 2026-09-23) instead of the calendar
    # month; change it any time on the new Settings page. Every other setting there still falls back
    # to .env until it is changed there too.
    op.bulk_insert(settings, [{"key": "app.cycle_start_day", "value": "23"}])


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'app.cycle_start_day'")
