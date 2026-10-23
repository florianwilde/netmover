"""rule targets

Weitere Ziele je Regel (bis zu 9 zusätzlich zum Hauptziel). Ein vorhandenes Archiv
(Migration 0004) wird zum ersten weiteren Ziel „Archiv“, seine Buchführung
(``archived_files``) wandert nach ``target_files``. Downgrade: das erste weitere
Ziel wird wieder zum Archiv, alle weiteren gehen verloren.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import netmover.models

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=24)


def upgrade() -> None:
    op.create_table(
        "rule_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", _enum("target_kind", "mount", "local"), nullable=False),
        sa.Column("mount_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("layout", _enum("target_layout", "flat", "date"), nullable=False),
        sa.ForeignKeyConstraint(
            ["mount_id"],
            ["mounts.id"],
            name=op.f("fk_rule_targets_mount_id_mounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
            name=op.f("fk_rule_targets_rule_id_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_targets")),
    )
    op.create_index(op.f("ix_rule_targets_rule_id"), "rule_targets", ["rule_id"])
    op.create_table(
        "target_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("src_relpath", sa.String(length=2048), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("dst_path", sa.String(length=2048), nullable=False),
        sa.Column("delivered_at", netmover.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["rule_targets.id"],
            name=op.f("fk_target_files_target_id_rule_targets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_files")),
        sa.UniqueConstraint(
            "target_id",
            "src_relpath",
            "size",
            "mtime_ns",
            name=op.f("uq_target_files_target_id_src_relpath_size_mtime_ns"),
        ),
    )
    op.create_index(op.f("ix_target_files_target_id"), "target_files", ["target_id"])

    # Archiv → erstes weiteres Ziel, Buchführung mitnehmen
    op.execute(
        """
        INSERT INTO rule_targets (rule_id, position, name, kind, mount_id, path, layout)
        SELECT id, 0, 'Archiv', archive_kind,
               CASE WHEN archive_kind = 'mount' THEN archive_mount_id END,
               archive_path, archive_layout
        FROM rules WHERE archive_kind != 'none'
        """
    )
    op.execute(
        """
        INSERT INTO target_files (target_id, src_relpath, size, mtime_ns, dst_path, delivered_at)
        SELECT t.id, a.src_relpath, a.size, a.mtime_ns, a.archive_path, a.archived_at
        FROM archived_files a JOIN rule_targets t ON t.rule_id = a.rule_id
        """
    )
    op.drop_table("archived_files")
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_rules_archive_mount_id_mounts"), type_="foreignkey")
        batch_op.drop_constraint(batch_op.f("ck_rules_archive_kind"), type_="check")
        batch_op.drop_constraint(batch_op.f("ck_rules_archive_layout"), type_="check")
        batch_op.drop_column("archive_layout")
        batch_op.drop_column("archive_path")
        batch_op.drop_column("archive_mount_id")
        batch_op.drop_column("archive_kind")


def downgrade() -> None:
    with op.batch_alter_table("rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "archive_kind",
                _enum("archive_kind", "none", "mount", "local"),
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
                _enum("archive_layout", "flat", "date"),
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
    for col, src in (
        ("archive_kind", "kind"),
        ("archive_mount_id", "mount_id"),
        ("archive_path", "path"),
        ("archive_layout", "layout"),
    ):
        # Spaltennamen sind feste Literale aus dieser Liste
        op.execute(
            f"UPDATE rules SET {col} = (SELECT t.{src} FROM rule_targets t "  # noqa: S608
            "WHERE t.rule_id = rules.id ORDER BY t.position, t.id LIMIT 1) "
            "WHERE EXISTS (SELECT 1 FROM rule_targets t WHERE t.rule_id = rules.id)"
        )
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
    op.execute(
        """
        INSERT INTO archived_files (rule_id, src_relpath, size, mtime_ns, archive_path,
                                    archived_at)
        SELECT t.rule_id, f.src_relpath, f.size, f.mtime_ns, f.dst_path, f.delivered_at
        FROM target_files f JOIN rule_targets t ON t.id = f.target_id
        WHERE t.id = (SELECT t2.id FROM rule_targets t2 WHERE t2.rule_id = t.rule_id
                      ORDER BY t2.position, t2.id LIMIT 1)
        """
    )
    op.drop_index(op.f("ix_target_files_target_id"), table_name="target_files")
    op.drop_table("target_files")
    op.drop_index(op.f("ix_rule_targets_rule_id"), table_name="rule_targets")
    op.drop_table("rule_targets")
