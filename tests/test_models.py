"""Schritt 1: Schema, Migrationen und Integritätsregeln."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from netmover.db import make_engine
from netmover.models import (
    Base,
    CopiedFile,
    Credential,
    ItemStatus,
    Mount,
    MountState,
    MountType,
    Rule,
    Run,
    RunItem,
)


def _mount(name: str = "scans", **kw: object) -> Mount:
    defaults: dict[str, object] = {
        "name": name,
        "slug": name,
        "type": MountType.CIFS,
        "server": "192.168.1.10",
        "share": "scans",
        "mountpoint": f"/mnt/netmover/{name}",
    }
    defaults.update(kw)
    return Mount(**defaults)


def test_migration_matches_models(db_url: str) -> None:
    engine = make_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_and_upgrade_roundtrip(db_url: str) -> None:
    from alembic import command

    from netmover.db import alembic_config, upgrade

    command.downgrade(alembic_config(db_url), "base")
    upgrade(db_url)


def test_defaults(session: Session) -> None:
    m = _mount()
    session.add(m)
    session.flush()
    r = Rule(name="r", src_mount_id=m.id, dst_path="/opt/consume")
    session.add(r)
    session.commit()
    assert m.state is MountState.UNMOUNTED
    assert m.auto_mount is True
    assert r.interval_seconds == 30
    assert r.stability_seconds == 5
    assert r.op_timeout_seconds == 120
    assert r.dst_mount_id is None


def test_datetime_roundtrip_is_utc_aware(session: Session) -> None:
    berlin = timezone(timedelta(hours=2))
    t = datetime(2026, 10, 10, 17, 0, tzinfo=berlin)
    m = _mount(last_mounted_at=t)
    session.add(m)
    session.commit()
    session.expire_all()
    loaded = session.scalars(select(Mount)).one()
    assert loaded.last_mounted_at == t
    assert loaded.last_mounted_at is not None
    assert loaded.last_mounted_at.tzinfo is UTC


def test_naive_datetime_rejected(session: Session) -> None:
    session.add(_mount(last_mounted_at=datetime(2026, 1, 1)))
    with pytest.raises(StatementError):
        session.commit()


def test_mount_in_use_cannot_be_deleted(session: Session) -> None:
    m = _mount()
    session.add(m)
    session.flush()
    session.add(Rule(name="r", src_mount_id=m.id, dst_path="/x"))
    session.commit()
    session.delete(m)
    with pytest.raises(IntegrityError):
        session.commit()


def test_credential_in_use_cannot_be_deleted(session: Session) -> None:
    c = Credential(name="nas", username="scan", secret_enc="x")
    session.add(c)
    session.flush()
    session.add(_mount(cred_id=c.id))
    session.commit()
    session.delete(c)
    with pytest.raises(IntegrityError):
        session.commit()


def test_rule_delete_cascades_runs_items_and_bookkeeping(session: Session) -> None:
    m = _mount()
    session.add(m)
    session.flush()
    r = Rule(name="r", src_mount_id=m.id, dst_path="/x")
    session.add(r)
    session.flush()
    run = Run(rule_id=r.id)
    session.add(run)
    session.flush()
    session.add(RunItem(run_id=run.id, src="a.pdf", status=ItemStatus.MOVED))
    session.add(
        CopiedFile(rule_id=r.id, src_relpath="a.pdf", size=1, mtime_ns=1, dst_path="/x/a.pdf")
    )
    session.commit()

    session.delete(r)
    session.commit()
    assert session.scalars(select(Run)).all() == []
    assert session.scalars(select(RunItem)).all() == []
    assert session.scalars(select(CopiedFile)).all() == []


def test_copied_files_unique_per_version(session: Session) -> None:
    m = _mount()
    session.add(m)
    session.flush()
    r = Rule(name="r", src_mount_id=m.id, dst_path="/x")
    session.add(r)
    session.flush()
    session.add(
        CopiedFile(rule_id=r.id, src_relpath="a.pdf", size=1, mtime_ns=1, dst_path="/x/a.pdf")
    )
    # neue Version derselben Datei ist erlaubt
    session.add(
        CopiedFile(rule_id=r.id, src_relpath="a.pdf", size=2, mtime_ns=2, dst_path="/x/a-1.pdf")
    )
    session.commit()
    session.add(
        CopiedFile(rule_id=r.id, src_relpath="a.pdf", size=1, mtime_ns=1, dst_path="/x/a-2.pdf")
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_enum_check_constraint_enforced_in_db(session: Session) -> None:
    from sqlalchemy import text

    session.add(_mount())
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE mounts SET type = 'ftp'"))
