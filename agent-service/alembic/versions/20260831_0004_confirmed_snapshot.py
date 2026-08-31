"""Persist the exact quote snapshot presented for confirmation.

Revision ID: 20260831_0004
Revises: 20260829_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "20260831_0004"
down_revision = "20260829_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("confirmed_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "sessions", sa.Column("confirmed_snapshot_hash", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("sessions", "confirmed_snapshot_hash")
    op.drop_column("sessions", "confirmed_snapshot")
