"""key management

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("credentials", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("managed", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )

    with op.batch_alter_table("mounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("username", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mounts", schema=None) as batch_op:
        batch_op.drop_column("username")

    with op.batch_alter_table("credentials", schema=None) as batch_op:
        batch_op.drop_column("managed")
