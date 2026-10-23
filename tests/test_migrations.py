"""Migrationen auf Datenbanken MIT Daten.

Praxisbefund beim Update 0.1.1 → 0.2.0: Migration 0002 baut auf SQLite Tabellen neu
auf (batch mode) und löscht dafür die alte Tabelle. Mit eingeschalteter
Fremdschlüsselprüfung scheiterte das, sobald Anbindungen auf Zugangsdaten
verwiesen – die Tests liefen bis dahin nur auf leeren Datenbanken. Und weil die
Migration nicht in einer Transaktion lief, blieb ein halber Zustand zurück.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from netmover.db import alembic_config, upgrade


def _head() -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    assert head is not None
    return head


def _seed(db: Path) -> None:
    """Datenbank auf Stand 0001 mit verknüpften Daten, wie auf einem echten System."""
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(
        """
        INSERT INTO credentials (id, name, kind, username, secret_enc, created_at)
            VALUES (1, 'mount:nas', 'password', 'scanner', 'gAAAA-x', '2026-10-10 20:00:00'),
                   (2, 'mount:paperless', 'ssh_key', 'root', 'gAAAA-y', '2026-10-10 20:00:00');
        INSERT INTO mounts (id, name, slug, type, server, share, mountpoint, cred_id,
                            extra_options, auto_mount, enabled, state)
            VALUES (1, 'NAS', 'nas', 'cifs', '10.0.10.6', 'Scans', '/mnt/netmover/nas', 1,
                    '', 1, 1, 'mounted'),
                   (2, 'paperless', 'paperless', 'sftp', '10.0.10.17', '/consume', NULL, 2,
                    '', 0, 1, 'mounted');
        INSERT INTO rules (id, name, enabled, src_mount_id, src_subpath, dst_mount_id, dst_path,
                           pattern, pattern_type, recursive, interval_seconds, stability_seconds,
                           min_age_seconds, op_timeout_seconds, action, on_collision, dry_run,
                           quarantine_after, consecutive_failures, created_at)
            VALUES (1, 'Scans → Paperless', 0, 1, '', 2, '', '*.pdf', 'glob', 0, 30, 5, 0, 120,
                    'move', 'rename', 0, 3, 0, '2026-10-10 20:00:00');
        INSERT INTO runs (id, rule_id, started_at, status, "trigger", dry_run, seen, moved,
                          skipped, failed)
            VALUES (1, 1, '2026-10-10 20:00:00', 'ok', 'manual', 1, 3, 3, 0, 0);
        INSERT INTO run_items (run_id, src, status) VALUES (1, 'a.pdf', 'planned');
        """
    )
    c.commit()
    c.close()


@pytest.fixture
def db_0001(tmp_path: Path) -> tuple[str, Path]:
    db = tmp_path / "alt.db"
    url = f"sqlite:///{db}"
    upgrade(url, "0001")
    _seed(db)
    return url, db


def _state(db: Path) -> dict[str, object]:
    c = sqlite3.connect(db)
    try:
        return {
            "version": c.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "tmp_tables": c.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).fetchall(),
            "cred_columns": [r[1] for r in c.execute("PRAGMA table_info(credentials)")],
            "mounts": c.execute("SELECT name, cred_id FROM mounts ORDER BY id").fetchall(),
            "rules": c.execute("SELECT name, src_mount_id, dst_mount_id FROM rules").fetchall(),
            "run_items": c.execute("SELECT COUNT(*) FROM run_items").fetchone()[0],
            "fk_violations": c.execute("PRAGMA foreign_key_check").fetchall(),
        }
    finally:
        c.close()


def test_upgrade_with_linked_data(db_0001: tuple[str, Path]) -> None:
    url, db = db_0001
    upgrade(url)
    st = _state(db)
    assert st["version"] == _head()
    assert st["tmp_tables"] == [] and st["fk_violations"] == []
    assert "managed" in st["cred_columns"]  # type: ignore[operator]
    assert st["mounts"] == [("NAS", 1), ("paperless", 2)]
    assert st["rules"] == [("Scans → Paperless", 1, 2)]
    assert st["run_items"] == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT managed FROM credentials ORDER BY id").fetchall() == [(0,), (0,)]
    assert c.execute("SELECT username FROM mounts ORDER BY id").fetchall() == [(None,), (None,)]
    c.close()


def test_downgrade_with_linked_data(db_0001: tuple[str, Path]) -> None:
    url, db = db_0001
    upgrade(url)
    command.downgrade(alembic_config(url), "0001")
    st = _state(db)
    assert st["version"] == "0001" and st["fk_violations"] == []
    assert st["mounts"] == [("NAS", 1), ("paperless", 2)]


def test_failed_migration_leaves_database_untouched(
    db_0001: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bricht eine Migration mittendrin ab, muss alles beim Alten bleiben – keine
    halb umgebauten Tabellen, kein _alembic_tmp-Rest, Version unverändert."""
    from alembic.ddl.impl import DefaultImpl

    url, db = db_0001
    before = _state(db)
    real_drop = DefaultImpl.drop_table

    def boom(self: DefaultImpl, *a: object, **kw: object) -> None:
        raise RuntimeError("Absturz mitten in der Migration")

    monkeypatch.setattr(DefaultImpl, "drop_table", boom)
    with pytest.raises(RuntimeError, match="Absturz"):
        upgrade(url)
    assert _state(db) == before

    monkeypatch.setattr(DefaultImpl, "drop_table", real_drop)
    upgrade(url)  # danach klappt es ohne Handarbeit
    assert _state(db)["version"] == _head()


def test_app_engine_still_enforces_foreign_keys(db_url: str) -> None:
    from sqlalchemy import text

    from netmover.db import make_engine

    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    engine.dispose()


def test_upgrade_from_0002_keeps_everything_linked_to_rules(tmp_path: Path) -> None:
    """0003 baut ``rules`` neu auf – Läufe, Buchführung und Fehlerzähler verweisen darauf."""
    db = tmp_path / "v02.db"
    url = f"sqlite:///{db}"
    upgrade(url, "0001")
    _seed(db)
    upgrade(url, "0002")
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(
        """
        UPDATE credentials SET managed = 1 WHERE id = 2;
        UPDATE mounts SET username = 'paperless' WHERE id = 2;
        INSERT INTO copied_files (rule_id, src_relpath, size, mtime_ns, dst_path, copied_at)
            VALUES (1, 'a.pdf', 1, 1, '/x/a.pdf', '2026-10-10 20:00:00');
        INSERT INTO file_failures (rule_id, src_relpath, size, mtime_ns, attempts, last_attempt_at)
            VALUES (1, 'b.pdf', 1, 1, 2, '2026-10-10 20:00:00');
        """
    )
    c.commit()
    c.close()
    upgrade(url)
    st = _state(db)
    assert st["version"] == _head() and st["fk_violations"] == [] and st["tmp_tables"] == []
    assert st["rules"] == [("Scans → Paperless", 1, 2)] and st["run_items"] == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM copied_files").fetchone()[0] == 1
    assert c.execute("SELECT attempts FROM file_failures").fetchone()[0] == 2
    assert c.execute("SELECT waiting_since FROM rules").fetchone()[0] is None
    assert c.execute("SELECT username FROM mounts WHERE id = 2").fetchone()[0] == "paperless"
    c.close()


# -- Pflicht für jede (auch künftige) Migration -------------------------------------------------


def _revisions() -> list[str]:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def _invariants(db: Path) -> tuple[object, ...]:
    st = _state(db)
    assert st["tmp_tables"] == [], "Reste einer Batch-Migration"
    assert st["fk_violations"] == [], "Fremdschlüssel verletzt"
    return st["mounts"], st["rules"], st["run_items"]


@pytest.mark.parametrize("rev", _revisions()[1:])
def test_every_migration_up_and_down_on_populated_db(tmp_path: Path, rev: str) -> None:
    """Jede Migration einzeln: hoch und wieder runter auf einer Datenbank mit Daten –
    ohne Fremdschlüsselfehler, ohne Reste, ohne Datenverlust. Neue Migrationen werden
    hier automatisch erfasst."""
    revs = _revisions()
    prev = revs[revs.index(rev) - 1]
    db = tmp_path / "pflicht.db"
    url = f"sqlite:///{db}"
    upgrade(url, revs[0])
    _seed(db)
    upgrade(url, prev)
    before = _invariants(db)
    upgrade(url, rev)
    assert _state(db)["version"] == rev
    assert _invariants(db) == before
    command.downgrade(alembic_config(url), prev)
    assert _state(db)["version"] == prev
    assert _invariants(db) == before


def test_migration_files_are_named_after_their_revision() -> None:
    versions = Path(__file__).parent.parent / "src/netmover/migrations/versions"
    names = sorted(p.name.split("_", 1)[0] for p in versions.glob("[0-9]*.py"))
    assert names == _revisions()


def test_0005_turns_archive_into_first_target_and_back(tmp_path: Path) -> None:
    """Archiv (0004) samt Buchführung wird zum weiteren Ziel „Archiv“ – und zurück."""
    db = tmp_path / "v09.db"
    url = f"sqlite:///{db}"
    upgrade(url, "0001")
    _seed(db)
    upgrade(url, "0004")
    c = sqlite3.connect(db)
    c.executescript(
        """
        UPDATE rules SET archive_kind = 'mount', archive_mount_id = 1,
                         archive_path = 'archiv', archive_layout = 'date';
        INSERT INTO archived_files (rule_id, src_relpath, size, mtime_ns, archive_path,
                                    archived_at)
            VALUES (1, 'a.pdf', 3, 7, '/mnt/netmover/nas/archiv/2026/10/a.pdf',
                    '2026-10-12 09:00:00');
        """
    )
    c.commit()
    c.close()

    upgrade(url, "0005")
    c = sqlite3.connect(db)
    assert c.execute(
        "SELECT rule_id, position, name, kind, mount_id, path, layout FROM rule_targets"
    ).fetchall() == [(1, 0, "Archiv", "mount", 1, "archiv", "date")]
    assert c.execute("SELECT src_relpath, size, mtime_ns FROM target_files").fetchall() == [
        ("a.pdf", 3, 7)
    ]
    cols = [r[1] for r in c.execute("PRAGMA table_info(rules)")]
    assert not [col for col in cols if col.startswith("archive_")]
    assert (
        c.execute("SELECT name FROM sqlite_master WHERE name = 'archived_files'").fetchall() == []
    )
    # zweites Ziel – geht beim Downgrade verloren, das erste bleibt
    c.execute(
        "INSERT INTO rule_targets (rule_id, position, name, kind, mount_id, path, layout) "
        "VALUES (1, 1, 'Buchhaltung', 'local', NULL, '/srv/buha', 'flat')"
    )
    c.commit()
    c.close()
    assert _state(db)["fk_violations"] == []

    command.downgrade(alembic_config(url), "0004")
    c = sqlite3.connect(db)
    assert c.execute(
        "SELECT archive_kind, archive_mount_id, archive_path, archive_layout FROM rules"
    ).fetchall() == [("mount", 1, "archiv", "date")]
    assert c.execute("SELECT rule_id, src_relpath FROM archived_files").fetchall() == [(1, "a.pdf")]
    c.close()
    assert _state(db)["fk_violations"] == []
