"""Datensicherung: Datenbank und Schlüssel täglich auf eine Anbindung (z. B. das NAS).

Ohne ``secret.key`` sind alle gespeicherten Zugangsdaten verloren – die Sicherung
enthält deshalb Datenbank **und** Schlüssel. Damit die Sicherung auf dem NAS nicht
selbst zum Schlüsselbund für alle Freigaben wird, verschlüsselt NetMover sie mit
einer Passphrase (scrypt + Fernet). Die Passphrase liegt verschlüsselt in der
Datenbank, damit die tägliche Sicherung ohne Eingabe läuft; zum Wiederherstellen
braucht man sie aus dem Passwortmanager.

Ablauf einer Sicherung:

1. Momentaufnahme der Datenbank über die SQLite-Backup-API (konsistent, auch
   während Regeln laufen) und ``PRAGMA integrity_check``.
2. tar.gz mit ``manifest.json``, Datenbank, Schlüsseln und ``netmover.env``.
3. Verschlüsseln und sofort probeweise wieder öffnen – eine Sicherung, die sich
   nicht lesen lässt, wird gar nicht erst hochgeladen.
4. Hochladen wie jede andere Datei (``.part`` → prüfen → atomar umbenennen).
5. Älteste Sicherungen über ``backup_keep`` hinaus löschen – erst nach Erfolg.

Format verschlüsselt (``.nmbak``): ``MAGIC`` + 16 Byte Salz + Fernet-Token.
Unverschlüsselt: ein gewöhnliches ``.tar.gz``.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import socket
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy.orm import Session, sessionmaker

from netmover import __version__
from netmover.config import Settings
from netmover.crypto import SecretBox
from netmover.logging_setup import event, get_logger
from netmover.models import CollisionPolicy, utcnow
from netmover.mover.destinations import MetaOptions
from netmover.settings_store import AppSettings, get_value, load_settings, set_value
from netmover.timeouts import CancelToken, run_with_watchdog

log = get_logger(__name__)

MAGIC = b"NETMOVER-BACKUP-1\n"
SALT_BYTES = 16
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE = 12

SUFFIX_ENC = ".nmbak"
SUFFIX_PLAIN = ".tar.gz"
NAME_RE = re.compile(r"^netmover-[A-Za-z0-9._-]+-\d{8}-\d{6}(\.nmbak|\.tar\.gz)$")

DB_NAME = "netmover.db"
KEY_FILES = ("secret.key", "session.key")
ENV_NAME = "netmover.env"

# Wann eine Sicherung als überfällig gilt (für /healthz und Kuma): zwei Tage plus
# Puffer – ein einzelner Ausfall des NAS über Nacht ist noch kein Alarm.
STALE_AFTER = timedelta(hours=50)
RETRY_AFTER = timedelta(hours=1)
IDLE_TIMEOUT = 120.0

STATE_PREFIX = "backup."


class BackupError(RuntimeError):
    pass


# -- Verschlüsselung -----------------------------------------------------------------------


def _derive(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_BYTES)
    return MAGIC + salt + Fernet(_derive(passphrase, salt)).encrypt(data)


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if not is_encrypted(blob):
        raise BackupError("Keine verschlüsselte NetMover-Sicherung")
    salt = blob[len(MAGIC) : len(MAGIC) + SALT_BYTES]
    token = blob[len(MAGIC) + SALT_BYTES :]
    try:
        return Fernet(_derive(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise BackupError("Passphrase falsch oder Sicherung beschädigt") from None


def passphrase_problem(passphrase: str, confirm: str) -> str | None:
    if len(passphrase) < MIN_PASSPHRASE:
        return f"Passphrase: mindestens {MIN_PASSPHRASE} Zeichen."
    if passphrase != confirm:
        return "Passphrasen stimmen nicht überein."
    return None


# -- Archiv bauen und lesen ------------------------------------------------------------------


@dataclass
class Archive:
    manifest: dict[str, Any]
    files: dict[str, bytes]


def snapshot_db(db_path: Path, target: Path) -> str:
    """Konsistente Kopie der laufenden Datenbank; liefert die Alembic-Revision."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        status = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise BackupError(f"Datenbank beschädigt: {status}")
        row = dst.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        dst.close()
        src.close()
    return str(row[0]) if row else ""


def _add(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(mtime)
    tar.addfile(info, io.BytesIO(data))


def build_archive(settings: Settings, now: datetime | None = None) -> tuple[bytes, dict[str, Any]]:
    """tar.gz mit Datenbank, Schlüsseln und Umgebungsdatei (unverschlüsselt)."""
    now = now or utcnow()
    with tempfile.TemporaryDirectory(dir=settings.data_dir, prefix=".backup-") as tmp:
        snap = Path(tmp) / DB_NAME
        revision = snapshot_db(settings.db_path, snap)
        files: dict[str, bytes] = {DB_NAME: snap.read_bytes()}
    for name in KEY_FILES:
        path = settings.data_dir / name
        if path.is_file():
            files[name] = path.read_bytes()
    if settings.config_file.is_file():
        files[ENV_NAME] = settings.config_file.read_bytes()
    manifest: dict[str, Any] = {
        "format": 1,
        "version": __version__,
        "revision": revision,
        "created": now.isoformat(),
        "host": socket.gethostname(),
        "files": sorted(files),
    }
    buf = io.BytesIO()
    stamp = time.time()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "manifest.json", json.dumps(manifest, indent=2).encode(), stamp)
        for name, data in files.items():
            _add(tar, name, data, stamp)
    return buf.getvalue(), manifest


def open_archive(blob: bytes, passphrase: str | None) -> Archive:
    """Sicherung lesen und prüfen (Manifest, Datenbank-Integrität)."""
    if is_encrypted(blob):
        if not passphrase:
            raise BackupError("Sicherung ist verschlüsselt – Passphrase angeben")
        blob = decrypt(blob, passphrase)
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or "/" in member.name or member.name.startswith("."):
                    raise BackupError(f"Unerwarteter Eintrag in der Sicherung: {member.name}")
                fh = tar.extractfile(member)
                if fh is not None:
                    files[member.name] = fh.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"Keine lesbare Sicherung: {exc}") from None
    try:
        manifest = json.loads(files.pop("manifest.json"))
    except (KeyError, ValueError):
        raise BackupError("Sicherung ohne gültiges manifest.json") from None
    if manifest.get("format") != 1:
        raise BackupError(f"Unbekanntes Sicherungsformat {manifest.get('format')!r}")
    if DB_NAME not in files or "secret.key" not in files:
        raise BackupError("Sicherung unvollständig: Datenbank oder secret.key fehlt")
    _check_db_bytes(files[DB_NAME])
    return Archive(manifest, files)


def _check_db_bytes(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / DB_NAME
        path.write_bytes(data)
        try:
            con = sqlite3.connect(path)
            try:
                status = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"Datenbank in der Sicherung unlesbar: {exc}") from None
    if status != "ok":
        raise BackupError(f"Datenbank in der Sicherung beschädigt: {status}")


def backup_name(now: datetime, encrypted: bool, host: str | None = None) -> str:
    host = re.sub(r"[^A-Za-z0-9._-]", "-", host or socket.gethostname()) or "host"
    local = now.astimezone()
    return f"netmover-{host}-{local:%Y%m%d-%H%M%S}{SUFFIX_ENC if encrypted else SUFFIX_PLAIN}"


def make_backup(
    settings: Settings, passphrase: str | None, now: datetime | None = None
) -> tuple[bytes, dict[str, Any]]:
    """Fertige Sicherung (verschlüsselt, falls Passphrase) – bereits probeweise geöffnet."""
    raw, manifest = build_archive(settings, now)
    blob = encrypt(raw, passphrase) if passphrase else raw
    open_archive(blob, passphrase)
    return blob, manifest


def prune_names(names: list[str], keep: int) -> list[str]:
    """Die zu löschenden Sicherungen: alles über die ``keep`` neuesten hinaus.
    Nur Dateien mit NetMover-Namensschema – sonst wird nichts angefasst."""
    ours = sorted((n for n in names if NAME_RE.match(n)), key=_stamp_of, reverse=True)
    return ours[max(keep, 1) :]


def _stamp_of(name: str) -> str:
    m = re.search(r"-(\d{8}-\d{6})\.", name)
    return m.group(1) if m else ""


# -- Wiederherstellen ----------------------------------------------------------------------


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def restore(settings: Settings, archive: Archive, now: datetime | None = None) -> Path:
    """Spielt eine Sicherung ein. Der Dienst muss gestoppt sein. Vorhandene Dateien
    wandern vorher nach ``data_dir/vor-wiederherstellung-<zeit>/`` – nichts geht
    verloren. Liefert diesen Ordner."""
    version = str(archive.manifest.get("version", "0"))
    if _version_tuple(version) > _version_tuple(__version__):
        raise BackupError(
            f"Sicherung stammt von NetMover {version}, installiert ist {__version__} – "
            "erst aktualisieren, dann wiederherstellen."
        )
    now = now or utcnow()
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    keep = data_dir / f"vor-wiederherstellung-{now.astimezone():%Y%m%d-%H%M%S}"
    keep.mkdir(mode=0o700)
    for name in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", *KEY_FILES):
        path = data_dir / name
        if path.exists():
            path.rename(keep / name)
    for name, data in archive.files.items():
        if name == ENV_NAME:
            continue
        _write_private(data_dir / name, data)
    if ENV_NAME in archive.files:
        env = settings.config_file
        if env.is_file() and env.read_bytes() != archive.files[ENV_NAME]:
            _write_private(keep / ENV_NAME, env.read_bytes())
        if env.parent.is_dir():
            _write_private(env, archive.files[ENV_NAME])
    event(log, "backup.restored", version=version, kept=str(keep))
    return keep


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.restore")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# -- Dienst: tägliche Sicherung ------------------------------------------------------------


@dataclass
class BackupState:
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str = ""
    last_file: str = ""
    last_size: int = 0
    failures: int = 0


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = json.loads(raw)
    return datetime.fromisoformat(value) if value else None


def load_state(factory: sessionmaker[Session]) -> BackupState:
    def get(key: str) -> str | None:
        return get_value(factory, STATE_PREFIX + key)

    return BackupState(
        last_attempt=_dt(get("last_attempt")),
        last_success=_dt(get("last_success")),
        last_error=json.loads(get("last_error") or '""'),
        last_file=json.loads(get("last_file") or '""'),
        last_size=json.loads(get("last_size") or "0"),
        failures=json.loads(get("failures") or "0"),
    )


def save_state(factory: sessionmaker[Session], st: BackupState) -> None:
    values: dict[str, Any] = {
        "last_attempt": st.last_attempt.isoformat() if st.last_attempt else None,
        "last_success": st.last_success.isoformat() if st.last_success else None,
        "last_error": st.last_error,
        "last_file": st.last_file,
        "last_size": st.last_size,
        "failures": st.failures,
    }
    for key, value in values.items():
        set_value(factory, STATE_PREFIX + key, json.dumps(value))


def parse_time(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not m:
        raise ValueError("Uhrzeit als HH:MM, z. B. 03:30")
    return int(m.group(1)), int(m.group(2))


def due_slot(cfg: AppSettings, now: datetime) -> datetime:
    """Der letzte Sicherungstermin (Ortszeit des Servers) vor ``now``."""
    hour, minute = parse_time(cfg.backup_time)
    local = now.astimezone()
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return slot


def is_due(cfg: AppSettings, st: BackupState, now: datetime) -> bool:
    if not cfg.backup_enabled or not cfg.backup_mount_id:
        return False
    slot = due_slot(cfg, now)
    if st.last_success is not None and st.last_success >= slot:
        return False
    # Nach einem Fehlschlag stündlich erneut, nicht bei jedem Takt
    return st.last_attempt is None or now - st.last_attempt >= RETRY_AFTER


@dataclass
class BackupResult:
    ok: bool
    message: str
    file: str = ""
    size: int = 0
    deleted: int = 0


Notify = Callable[[str, str, str], None]  # (event, title, message)


class BackupService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        destination_for: Callable[[int, str], Callable[[], Any]],
        secrets: SecretBox,
        notify: Notify | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._destination_for = destination_for
        self.secrets = secrets
        self._notify = notify
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def passphrase(self, cfg: AppSettings) -> str | None:
        if not cfg.backup_passphrase_enc:
            return None
        return self.secrets.decrypt(cfg.backup_passphrase_enc)

    def tick(self, now: datetime | None = None) -> BackupResult | None:
        """Scheduler-Job: sichert, wenn ein Termin fällig ist."""
        now = now or utcnow()
        cfg = load_settings(self.session_factory)
        if not is_due(cfg, load_state(self.session_factory), now):
            return None
        return self.run(now)

    def run(self, now: datetime | None = None) -> BackupResult:
        if self._running:
            return BackupResult(False, "Eine Sicherung läuft bereits.")
        self._running = True
        try:
            return self._run(now or utcnow())
        finally:
            self._running = False

    def _run(self, now: datetime) -> BackupResult:
        cfg = load_settings(self.session_factory)
        st = load_state(self.session_factory)
        st.last_attempt = now
        try:
            if not cfg.backup_mount_id:
                raise BackupError("Kein Sicherungsziel eingestellt")
            if not cfg.backup_passphrase_enc:
                raise BackupError("Keine Passphrase gesetzt")
            result = self._write(cfg, now)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            st.failures += 1
            st.last_error = message
            save_state(self.session_factory, st)
            event(log, "backup.failed", level=40, error=message, failures=st.failures)
            if st.failures == 1 and self._notify:
                self._notify(
                    "backup.failed",
                    "NetMover: Sicherung fehlgeschlagen",
                    f"{message}\nNächster Versuch in einer Stunde.",
                )
            return BackupResult(False, message)
        recovered = st.failures > 0
        st.failures = 0
        st.last_error = ""
        st.last_success = now
        st.last_file = result.file
        st.last_size = result.size
        save_state(self.session_factory, st)
        event(log, "backup.done", file=result.file, size=result.size, deleted=result.deleted)
        if recovered and self._notify:
            self._notify(
                "backup.recovered",
                "NetMover: Sicherung läuft wieder",
                f"Sicherung {result.file} geschrieben.",
            )
        return result

    def _write(self, cfg: AppSettings, now: datetime) -> BackupResult:
        passphrase = self.passphrase(cfg)
        blob, _manifest = make_backup(self.settings, passphrase, now)
        name = backup_name(now, passphrase is not None)
        make_dest = self._destination_for(cfg.backup_mount_id, cfg.backup_path)

        def work(token: CancelToken) -> BackupResult:
            dest = make_dest()
            try:
                dest.prepare()
                with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
                    local = Path(tmp) / name
                    local.write_bytes(blob)
                    ns = time.time_ns()
                    result = dest.commit(
                        local,
                        len(blob),
                        name,
                        CollisionPolicy.RENAME,
                        MetaOptions(ns, ns, 0o600, None),
                        lambda: None,
                        token,
                    )
                deleted = 0
                for old in prune_names(dest.list_names(), cfg.backup_keep):
                    token.check()
                    with contextlib.suppress(OSError):
                        dest.remove(old)
                        deleted += 1
                return BackupResult(
                    True,
                    f"Sicherung {dest.describe(result.final_rel)} geschrieben",
                    result.final_rel,
                    len(blob),
                    deleted,
                )
            finally:
                dest.close()

        return run_with_watchdog(work, IDLE_TIMEOUT, "Sicherung")
