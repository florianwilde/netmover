"""SQLAlchemy-Datenmodell.

Abweichungen bzw. Ergänzungen gegenüber dem Mindestumfang aus der Spezifikation:

* ``credentials.domain`` – Windows-/AD-Freigaben brauchen oft eine Domäne.
* ``mounts.state_since`` – für „hängt seit …“ / „gestört seit …“ auf der Statusseite.
* ``mounts.write_ok`` / ``mounts.root_squash`` / ``mounts.last_test_*`` – Ergebnis
  des letzten Mount-Tests; ``root_squash`` entscheidet, ob ``chown`` auf NFS-Zielen
  versucht wird (Fallstrick 12).
* ``rules.op_timeout_seconds`` – Zeitlimit des Worker-Laufs (Fallstrick 13).
* ``rules.quarantine_after`` + Tabelle ``file_failures`` – Zählung wiederholt
  scheiternder Dateien für den Quarantäneordner.
* ``runs.status`` / ``runs.trigger`` / ``runs.dry_run`` – Filter im Protokoll.
* ``copied_files.mtime_ns`` – ``mtime`` als ganzzahlige Nanosekunden statt Float,
  damit der Gleichheitsvergleich exakt ist (Fallstrick 11).
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Speichert naive UTC-Zeitstempel (SQLite kennt keine Zeitzonen) und liefert
    immer zeitzonenbewusste ``datetime``-Objekte zurück."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime – bitte zeitzonenbewusst (UTC) übergeben")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


# Einheitliche Constraint-Namen, damit Alembic-Batch-Migrationen auf SQLite
# Constraints wiederfinden.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=24,
        values_callable=lambda e: [m.value for m in e],
    )


class MountType(enum.StrEnum):
    CIFS = "cifs"
    NFS4 = "nfs4"
    NFS3 = "nfs3"


class MountState(enum.StrEnum):
    UNMOUNTED = "unmounted"
    MOUNTED = "mounted"
    DEGRADED = "degraded"  # gestört: Wiederherstellung läuft
    ERROR = "error"  # Wiederherstellung gescheitert


class PatternType(enum.StrEnum):
    GLOB = "glob"
    REGEX = "regex"


class RuleAction(enum.StrEnum):
    MOVE = "move"
    COPY = "copy"


class CollisionPolicy(enum.StrEnum):
    RENAME = "rename"
    SKIP = "skip"
    OVERWRITE = "overwrite"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"  # keine Dateifehler
    PARTIAL = "partial"  # einzelne Dateien gescheitert
    ERROR = "error"  # Lauf abgebrochen (Mount fehlt, Zeitlimit, …)
    SKIPPED = "skipped"  # Termin übersprungen, voriger Lauf noch aktiv


class RunTrigger(enum.StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class ItemStatus(enum.StrEnum):
    MOVED = "moved"
    COPIED = "copied"
    SKIPPED = "skipped"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    PLANNED = "planned"  # Probelauf


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    username: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))
    secret_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # passive_deletes="all": das ORM soll cred_id nicht still auf NULL setzen,
    # sondern das RESTRICT der Datenbank greifen lassen.
    mounts: Mapped[list[Mount]] = relationship(back_populates="credential", passive_deletes="all")


class Mount(Base):
    __tablename__ = "mounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[MountType] = mapped_column(_enum(MountType, "mount_type"))
    server: Mapped[str] = mapped_column(String(255))
    share: Mapped[str] = mapped_column(String(1024))
    mountpoint: Mapped[str] = mapped_column(String(1024), unique=True)
    cred_id: Mapped[int | None] = mapped_column(ForeignKey("credentials.id", ondelete="RESTRICT"))
    uid: Mapped[int | None] = mapped_column(Integer)
    gid: Mapped[int | None] = mapped_column(Integer)
    file_mode: Mapped[str | None] = mapped_column(String(5))
    dir_mode: Mapped[str | None] = mapped_column(String(5))
    extra_options: Mapped[str] = mapped_column(Text, default="")
    auto_mount: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    state: Mapped[MountState] = mapped_column(
        _enum(MountState, "mount_state"), default=MountState.UNMOUNTED
    )
    state_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_mounted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_test_result: Mapped[str | None] = mapped_column(Text)  # JSON
    write_ok: Mapped[bool | None] = mapped_column(Boolean)
    root_squash: Mapped[bool | None] = mapped_column(Boolean)

    credential: Mapped[Credential | None] = relationship(back_populates="mounts")


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    src_mount_id: Mapped[int] = mapped_column(ForeignKey("mounts.id", ondelete="RESTRICT"))
    src_subpath: Mapped[str] = mapped_column(String(1024), default="")
    # dst_mount_id NULL → dst_path ist ein absoluter lokaler Pfad
    dst_mount_id: Mapped[int | None] = mapped_column(ForeignKey("mounts.id", ondelete="RESTRICT"))
    dst_path: Mapped[str] = mapped_column(String(1024), default="")

    pattern: Mapped[str] = mapped_column(String(512), default="*")
    pattern_type: Mapped[PatternType] = mapped_column(
        _enum(PatternType, "pattern_type"), default=PatternType.GLOB
    )
    recursive: Mapped[bool] = mapped_column(Boolean, default=False)

    interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    stability_seconds: Mapped[int] = mapped_column(Integer, default=5)
    min_age_seconds: Mapped[int] = mapped_column(Integer, default=0)
    max_size_mb: Mapped[int | None] = mapped_column(Integer)
    op_timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)

    action: Mapped[RuleAction] = mapped_column(
        _enum(RuleAction, "rule_action"), default=RuleAction.MOVE
    )
    on_collision: Mapped[CollisionPolicy] = mapped_column(
        _enum(CollisionPolicy, "collision_policy"), default=CollisionPolicy.RENAME
    )
    set_owner: Mapped[str | None] = mapped_column(String(64))  # "uid:gid", NULL = unverändert
    set_mode: Mapped[str | None] = mapped_column(String(5))  # "0644", NULL = unverändert
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    quarantine_path: Mapped[str | None] = mapped_column(String(1024))
    quarantine_after: Mapped[int] = mapped_column(Integer, default=3)

    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    src_mount: Mapped[Mount] = relationship(foreign_keys=[src_mount_id])
    dst_mount: Mapped[Mount | None] = relationship(foreign_keys=[dst_mount_id])
    runs: Mapped[list[Run]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", passive_deletes=True
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_rule_started", "rule_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.RUNNING
    )
    trigger: Mapped[RunTrigger] = mapped_column(
        _enum(RunTrigger, "run_trigger"), default=RunTrigger.SCHEDULE
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    moved: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)

    rule: Mapped[Rule] = relationship(back_populates="runs")
    items: Mapped[list[RunItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class RunItem(Base):
    __tablename__ = "run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    src: Mapped[str] = mapped_column(String(2048))
    dst: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[ItemStatus] = mapped_column(_enum(ItemStatus, "item_status"))
    size: Mapped[int | None] = mapped_column(BigInteger)
    message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="items")


class CopiedFile(Base):
    """Buchführung für Aktion ``copy`` (Fallstrick 11)."""

    __tablename__ = "copied_files"
    __table_args__ = (UniqueConstraint("rule_id", "src_relpath", "size", "mtime_ns"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"))
    src_relpath: Mapped[str] = mapped_column(String(2048))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    dst_path: Mapped[str] = mapped_column(String(2048))
    copied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FileFailure(Base):
    """Fehlversuche pro Datei, Grundlage für die Quarantäne."""

    __tablename__ = "file_failures"
    __table_args__ = (UniqueConstraint("rule_id", "src_relpath"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"))
    src_relpath: Mapped[str] = mapped_column(String(2048))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
