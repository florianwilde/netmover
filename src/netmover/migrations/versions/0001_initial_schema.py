"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import netmover.models

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "password",
                "ssh_key",
                name="credential_kind",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("created_at", netmover.models.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credentials")),
        sa.UniqueConstraint("name", name=op.f("uq_credentials_name")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "mounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "cifs",
                "nfs4",
                "nfs3",
                "sftp",
                name="mount_type",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("server", sa.String(length=255), nullable=False),
        sa.Column("share", sa.String(length=1024), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("host_key", sa.Text(), nullable=True),
        sa.Column("mountpoint", sa.String(length=1024), nullable=True),
        sa.Column("cred_id", sa.Integer(), nullable=True),
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.Column("gid", sa.Integer(), nullable=True),
        sa.Column("file_mode", sa.String(length=5), nullable=True),
        sa.Column("dir_mode", sa.String(length=5), nullable=True),
        sa.Column("extra_options", sa.Text(), nullable=False),
        sa.Column("auto_mount", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "unmounted",
                "mounted",
                "degraded",
                "error",
                name="mount_state",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("state_since", netmover.models.UTCDateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_mounted_at", netmover.models.UTCDateTime(), nullable=True),
        sa.Column("last_test_at", netmover.models.UTCDateTime(), nullable=True),
        sa.Column("last_test_result", sa.Text(), nullable=True),
        sa.Column("write_ok", sa.Boolean(), nullable=True),
        sa.Column("root_squash", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["cred_id"],
            ["credentials.id"],
            name=op.f("fk_mounts_cred_id_credentials"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mounts")),
        sa.UniqueConstraint("mountpoint", name=op.f("uq_mounts_mountpoint")),
        sa.UniqueConstraint("name", name=op.f("uq_mounts_name")),
        sa.UniqueConstraint("slug", name=op.f("uq_mounts_slug")),
    )
    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("src_mount_id", sa.Integer(), nullable=False),
        sa.Column("src_subpath", sa.String(length=1024), nullable=False),
        sa.Column("dst_mount_id", sa.Integer(), nullable=True),
        sa.Column("dst_path", sa.String(length=1024), nullable=False),
        sa.Column("pattern", sa.String(length=512), nullable=False),
        sa.Column(
            "pattern_type",
            sa.Enum(
                "glob",
                "regex",
                name="pattern_type",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("recursive", sa.Boolean(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("stability_seconds", sa.Integer(), nullable=False),
        sa.Column("min_age_seconds", sa.Integer(), nullable=False),
        sa.Column("max_size_mb", sa.Integer(), nullable=True),
        sa.Column("op_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "move",
                "copy",
                name="rule_action",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "on_collision",
            sa.Enum(
                "rename",
                "skip",
                "overwrite",
                name="collision_policy",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("set_owner", sa.String(length=64), nullable=True),
        sa.Column("set_mode", sa.String(length=5), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("quarantine_path", sa.String(length=1024), nullable=True),
        sa.Column("quarantine_after", sa.Integer(), nullable=False),
        sa.Column("last_run_at", netmover.models.UTCDateTime(), nullable=True),
        sa.Column("last_success_at", netmover.models.UTCDateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("created_at", netmover.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dst_mount_id"],
            ["mounts.id"],
            name=op.f("fk_rules_dst_mount_id_mounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["src_mount_id"],
            ["mounts.id"],
            name=op.f("fk_rules_src_mount_id_mounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")),
        sa.UniqueConstraint("name", name=op.f("uq_rules_name")),
    )
    op.create_table(
        "copied_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("src_relpath", sa.String(length=2048), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("dst_path", sa.String(length=2048), nullable=False),
        sa.Column("copied_at", netmover.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
            name=op.f("fk_copied_files_rule_id_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_copied_files")),
        sa.UniqueConstraint(
            "rule_id",
            "src_relpath",
            "size",
            "mtime_ns",
            name=op.f("uq_copied_files_rule_id_src_relpath_size_mtime_ns"),
        ),
    )
    op.create_table(
        "file_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("src_relpath", sa.String(length=2048), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", netmover.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
            name=op.f("fk_file_failures_rule_id_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_failures")),
        sa.UniqueConstraint(
            "rule_id", "src_relpath", name=op.f("uq_file_failures_rule_id_src_relpath")
        ),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("started_at", netmover.models.UTCDateTime(), nullable=False),
        sa.Column("finished_at", netmover.models.UTCDateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "ok",
                "partial",
                "error",
                "skipped",
                name="run_status",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            sa.Enum(
                "schedule",
                "manual",
                name="run_trigger",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("seen", sa.Integer(), nullable=False),
        sa.Column("moved", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rules.id"], name=op.f("fk_runs_rule_id_rules"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index("ix_runs_rule_started", ["rule_id", "started_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_started_at"), ["started_at"], unique=False)

    op.create_table(
        "run_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("src", sa.String(length=2048), nullable=False),
        sa.Column("dst", sa.String(length=2048), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "moved",
                "copied",
                "skipped",
                "failed",
                "quarantined",
                "planned",
                name="item_status",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_items_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_items")),
    )
    with op.batch_alter_table("run_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_items_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("run_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_items_run_id"))

    op.drop_table("run_items")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_started_at"))
        batch_op.drop_index("ix_runs_rule_started")

    op.drop_table("runs")
    op.drop_table("file_failures")
    op.drop_table("copied_files")
    op.drop_table("rules")
    op.drop_table("mounts")
    op.drop_table("settings")
    op.drop_table("credentials")
