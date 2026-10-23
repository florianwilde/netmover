"""0.10.0: Datensicherung – verschlüsselt auf eine Anbindung, Wiederherstellen per CLI."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from netmover import __version__, backup
from netmover.app_state import NetMover
from netmover.backup import (
    BackupError,
    is_due,
    load_state,
    make_backup,
    open_archive,
    prune_names,
)
from netmover.cli import cmd_backup, cmd_restore
from netmover.config import Settings
from netmover.models import Mount, MountType
from netmover.mounting.fake import FakeMountBackend
from netmover.settings_store import AppSettings, load_settings, save_settings
from tests.webutil import post

PASS = "korrekt-pferd-batterie"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _nas(state: NetMover, backend: FakeMountBackend) -> tuple[int, Path]:
    """CIFS-Anbindung „Backup“ einhängen; liefert (id, Freigabe auf dem „NAS“)."""
    share = backend.export("nas", "backup")
    with state.session_factory() as s:
        m = Mount(
            name="Backup",
            slug="backup",
            type=MountType.CIFS,
            server="nas",
            share="backup",
            mountpoint=str(state.settings.mount_base.resolve() / "backup"),
        )
        s.add(m)
        s.commit()
        mid = m.id
    state.mounts.mount(mid)
    return mid, share


def _configure(state: NetMover, mount_id: int, **kw: Any) -> AppSettings:
    cfg = load_settings(state.session_factory)
    cfg.backup_enabled = True
    cfg.backup_mount_id = mount_id
    cfg.backup_path = "netmover"
    cfg.backup_passphrase_enc = state.secrets.encrypt(PASS)
    for k, v in kw.items():
        setattr(cfg, k, v)
    save_settings(state.session_factory, cfg)
    return cfg


# -- Format ----------------------------------------------------------------------------------


def test_encrypted_roundtrip_and_wrong_passphrase(state: NetMover) -> None:
    blob, manifest = make_backup(state.settings, PASS)
    assert backup.is_encrypted(blob)
    # Weder Schlüssel noch Datenbank im Klartext
    key = state.settings.secret_key_path.read_bytes().strip()
    assert key not in blob and b"SQLite format 3" not in blob
    archive = open_archive(blob, PASS)
    assert archive.manifest["version"] == __version__
    assert archive.manifest["revision"]
    assert archive.files["secret.key"].strip() == key
    assert archive.files["netmover.db"].startswith(b"SQLite format 3")
    assert manifest["files"] == sorted(archive.files)
    with pytest.raises(BackupError, match="Passphrase falsch"):
        open_archive(blob, "falsche-passphrase")
    with pytest.raises(BackupError, match="verschlüsselt"):
        open_archive(blob, None)


def test_tampered_backup_is_rejected(state: NetMover) -> None:
    blob = bytearray(make_backup(state.settings, PASS)[0])
    blob[-10] ^= 0x01
    with pytest.raises(BackupError):
        open_archive(bytes(blob), PASS)


def test_env_file_is_included(state: NetMover, tmp_path: Path) -> None:
    env = tmp_path / "netmover.env"
    env.write_text("NETMOVER_PORT=8080\n")
    settings = state.settings.model_copy(update={"config_file": env})
    archive = open_archive(make_backup(settings, None)[0], None)
    assert archive.files["netmover.env"] == b"NETMOVER_PORT=8080\n"


def test_archive_without_secret_key_is_rejected() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'{"format": 1}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(BackupError, match="unvollständig"):
        open_archive(buf.getvalue(), None)


def test_prune_keeps_newest_and_ignores_foreign_files() -> None:
    ours = [f"netmover-h-202610{d:02d}-033000.nmbak" for d in range(1, 8)]
    names = [*ours, "urlaub.jpg", "netmover-notizen.txt"]
    assert sorted(prune_names(names, 3)) == ours[:4]
    assert prune_names(names, 10) == []


# -- Termin ----------------------------------------------------------------------------------


def _cfg(time: str = "03:30") -> AppSettings:
    return AppSettings(backup_enabled=True, backup_mount_id=1, backup_time=time)


def test_due_after_slot_once_per_day_and_hourly_retry() -> None:
    local = datetime(2026, 10, 23, 2, 0).astimezone()  # 02:00 Ortszeit
    st = backup.BackupState()
    yesterday_ok = backup.BackupState(last_success=local - timedelta(hours=3))
    assert is_due(_cfg(), st, local)  # noch nie gesichert → nachholen
    assert not is_due(_cfg(), yesterday_ok, local)  # 23:00 gestern nach 03:30 gestern
    later = local + timedelta(hours=2)  # 04:00 → heutiger Termin ist fällig
    assert is_due(_cfg(), yesterday_ok, later)
    failed = backup.BackupState(
        last_success=yesterday_ok.last_success, last_attempt=later - timedelta(minutes=10)
    )
    assert not is_due(_cfg(), failed, later)  # gerade gescheitert → nicht jeden Takt
    assert is_due(_cfg(), failed, later + timedelta(hours=1))
    assert not is_due(AppSettings(backup_enabled=False, backup_mount_id=1), st, later)
    with pytest.raises(ValueError):
        backup.parse_time("25:00")


# -- Dienst ----------------------------------------------------------------------------------


def test_service_writes_encrypted_backup_to_mount_and_prunes(
    state: NetMover, backend: FakeMountBackend
) -> None:
    mid, share = _nas(state, backend)
    _configure(state, mid, backup_keep=3)
    (share / "netmover").mkdir()
    (share / "netmover" / "fremd.txt").write_text("bleibt")
    for i in range(5):
        result = state.backups.run(NOW + timedelta(days=i))
        assert result.ok, result.message
    files = sorted(p.name for p in (share / "netmover").iterdir())
    assert "fremd.txt" in files
    backups = [f for f in files if f.endswith(".nmbak")]
    assert len(backups) == 3
    assert not [f for f in files if f.endswith(".part")]
    newest = share / "netmover" / backups[-1]
    assert open_archive(newest.read_bytes(), PASS).manifest["version"] == __version__
    st = load_state(state.session_factory)
    assert st.last_success == NOW + timedelta(days=4)
    assert st.last_file == backups[-1] and st.last_size == newest.stat().st_size
    check = state.health().find("backup")
    assert check is not None and check.ok


def test_failure_is_recorded_notified_once_and_becomes_problem(
    settings: Settings, backend: FakeMountBackend
) -> None:
    sent: list[tuple[str, str, str]] = []
    state = NetMover(settings, backend=backend)
    mid, _share = _nas(state, backend)
    _configure(state, mid)
    state.backups._notify = lambda *a: sent.append(a)
    backend.vanish(state.mounts.mountpoint_for("backup"))
    backend.fail_next_mount(times=10)
    for i in range(3):
        result = state.backups.run(NOW + timedelta(hours=i))
        assert not result.ok
    st = load_state(state.session_factory)
    assert st.failures == 3 and "eingehängt" in st.last_error
    assert [k for k, _t, _m in sent] == ["backup.failed"]
    # Überfällig erst nach STALE_AFTER – dann rot in /healthz und Kuma
    state.started_at = datetime.now(UTC) - backup.STALE_AFTER * 2
    check = state.health().find("backup")
    assert check is not None and not check.ok and "überfällig" in check.message


def test_tick_runs_only_when_due(state: NetMover, backend: FakeMountBackend) -> None:
    mid, _ = _nas(state, backend)
    _configure(state, mid)
    assert state.backups.tick(NOW) is not None  # noch nie gesichert
    assert state.backups.tick(NOW + timedelta(minutes=5)) is None


def test_missing_passphrase_refuses(state: NetMover, backend: FakeMountBackend) -> None:
    mid, _ = _nas(state, backend)
    _configure(state, mid, backup_passphrase_enc="")
    result = state.backups.run(NOW)
    assert not result.ok and "Passphrase" in result.message


# -- Wiederherstellen ------------------------------------------------------------------------


def _mount_names(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("select name from mounts order by name")]
    finally:
        con.close()


def test_cli_backup_and_restore_roundtrip(
    state: NetMover, backend: FakeMountBackend, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = state.settings
    mid, _ = _nas(state, backend)
    _configure(state, mid)
    out = tmp_path / "out"
    out.mkdir()
    assert cmd_backup(settings, out, encrypted=True) == 0
    (file,) = out.iterdir()
    assert file.suffix == ".nmbak" and file.stat().st_mode & 0o077 == 0
    old_key = settings.secret_key_path.read_bytes()
    state.stop()

    # Nach der Sicherung geht etwas schief: Anbindung gelöscht, Schlüssel weg
    con = sqlite3.connect(settings.db_path)
    con.execute("delete from mounts")
    con.commit()
    con.close()
    settings.secret_key_path.write_bytes(b"kaputt")
    phrase = tmp_path / "phrase"
    phrase.write_text(PASS + "\n")

    assert cmd_restore(settings, file, phrase, assume_yes=True, active=lambda: True) == 1
    assert "Dienst läuft" in capsys.readouterr().out
    assert cmd_restore(settings, file, phrase, assume_yes=True, active=lambda: False) == 0

    assert _mount_names(settings.db_path) == ["Backup"]
    assert settings.secret_key_path.read_bytes() == old_key
    assert settings.secret_key_path.stat().st_mode & 0o077 == 0
    (kept,) = settings.data_dir.glob("vor-wiederherstellung-*")
    assert (kept / "secret.key").read_bytes() == b"kaputt"
    assert _mount_names(kept / "netmover.db") == []
    # Gespeicherte Zugangsdaten sind mit dem zurückgeholten Schlüssel wieder lesbar
    restarted = NetMover(settings, backend=backend)
    cfg = load_settings(restarted.session_factory)
    assert restarted.backups.passphrase(cfg) == PASS
    restarted.stop()


def test_restore_refuses_newer_version(state: NetMover, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = open_archive(make_backup(state.settings, None)[0], None)
    archive.manifest["version"] = "99.0.0"
    with pytest.raises(BackupError, match="erst aktualisieren"):
        backup.restore(state.settings, archive)


# -- Oberfläche ------------------------------------------------------------------------------


def test_backup_page_save_run_and_health(
    client: TestClient, state: NetMover, backend: FakeMountBackend
) -> None:
    mid, share = _nas(state, backend)
    page = client.get("/backup")
    assert page.status_code == 200 and 'href="/backup"' in page.text
    form = {
        "backup_enabled": "on",
        "backup_mount_id": str(mid),
        "backup_path": "netmover",
        "backup_time": "03:30",
        "backup_keep": "14",
    }
    r = post(client, "/backup", form)
    assert r.status_code == 400 and "Passphrase festlegen" in r.text
    r = post(client, "/backup", {**form, "passphrase": PASS, "passphrase_confirm": "anders"})
    assert r.status_code == 400 and "stimmen nicht" in r.text
    r = post(client, "/backup", {**form, "backup_path": "../raus", "passphrase": PASS})
    assert r.status_code == 400
    r = post(client, "/backup", {**form, "passphrase": PASS, "passphrase_confirm": PASS})
    assert r.status_code == 303
    with state.session_factory() as s:
        from netmover.models import Setting

        raw = " ".join(v for (v,) in s.execute(select(Setting.value)))
    assert PASS not in raw  # Passphrase nur verschlüsselt gespeichert
    # Passphrase leer lassen = unverändert
    assert post(client, "/backup", {**form, "backup_keep": "7"}).status_code == 303
    assert state.backups.passphrase(load_settings(state.session_factory)) == PASS

    assert post(client, "/backup/run").status_code == 303
    assert "geschrieben" in client.get("/backup").text
    assert len(list((share / "netmover").glob("*.nmbak"))) == 1
    r = client.get("/healthz/backup")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert "/healthz/backup" in client.get("/settings").text
    assert any(c["kind"] == "backup" for c in client.get("/healthz").json()["checks"])
