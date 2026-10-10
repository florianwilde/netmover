"""rule waiting state

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import netmover.models

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("waiting_since", netmover.models.UTCDateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("waiting_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.drop_column("waiting_reason")
        batch_op.drop_column("waiting_since")
