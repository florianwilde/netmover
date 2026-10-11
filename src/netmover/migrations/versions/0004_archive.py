"""archive

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import netmover.models

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "archived_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("src_relpath", sa.String(length=2048), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("archive_path", sa.String(length=2048), nullable=False),
        sa.Column("archived_at", netmover.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
            name=op.f("fk_archived_files_rule_id_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archived_files")),
        sa.UniqueConstraint(
            "rule_id",
            "src_relpath",
            "size",
            "mtime_ns",
            name=op.f("uq_archived_files_rule_id_src_relpath_size_mtime_ns"),
        ),
    )
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "archive_kind",
                sa.Enum(
                    "none",
                    "mount",
                    "local",
                    name="archive_kind",
                    native_enum=False,
                    create_constraint=True,
                    length=24,
                ),
                server_default="none",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("archive_mount_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("archive_path", sa.String(length=1024), server_default="", nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "archive_layout",
                sa.Enum(
                    "flat",
                    "date",
                    name="archive_layout",
                    native_enum=False,
                    create_constraint=True,
                    length=24,
                ),
                server_default="flat",
                nullable=False,
            )
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_rules_archive_mount_id_mounts"),
            "mounts",
            ["archive_mount_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_rules_archive_mount_id_mounts"), type_="foreignkey")
        # CHECK-Regeln der Auswahlfelder zuerst entfernen – sonst verweisen sie beim
        # Neuaufbau der Tabelle auf Spalten, die es nicht mehr gibt.
        batch_op.drop_constraint(batch_op.f("ck_rules_archive_kind"), type_="check")
        batch_op.drop_constraint(batch_op.f("ck_rules_archive_layout"), type_="check")
        batch_op.drop_column("archive_layout")
        batch_op.drop_column("archive_path")
        batch_op.drop_column("archive_mount_id")
        batch_op.drop_column("archive_kind")

    op.drop_table("archived_files")
