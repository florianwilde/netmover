"""Kommandozeile: ``netmover serve`` / ``netmover migrate`` / ``netmover version``
/ ``netmover reset-password`` / ``netmover backup`` / ``netmover restore``."""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from netmover import __version__
from netmover.config import Settings, get_settings
from netmover.db import upgrade
from netmover.logging_setup import event, get_logger, setup_logging

log = get_logger("netmover.cli")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netmover")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="Version ausgeben")
    sub.add_parser("migrate", help="Datenbank auf den neuesten Stand bringen")
    sub.add_parser("serve", help="Dienst mit Weboberfläche starten")
    sub.add_parser(
        "reset-password",
        help="Administratorpasswort löschen – beim nächsten Aufruf startet die Einrichtung",
    )
    b = sub.add_parser("backup", help="Sicherung in eine lokale Datei oder einen Ordner schreiben")
    b.add_argument("out", type=Path, help="Zieldatei oder Ordner")
    b.add_argument(
        "--unverschluesselt",
        action="store_true",
        help="ohne Passphrase (die Datei enthält dann alle Zugangsdaten lesbar)",
    )
    r = sub.add_parser("restore", help="Sicherung einspielen (Dienst vorher stoppen)")
    r.add_argument("file", type=Path, help="Sicherungsdatei (.nmbak oder .tar.gz)")
    r.add_argument("--passphrase-file", type=Path, help="Passphrase aus Datei statt Abfrage")
    r.add_argument("--ja", action="store_true", help="ohne Rückfrage einspielen")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "migrate":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        upgrade(settings.effective_database_url)
        event(log, "db.migrated", url=settings.effective_database_url)
        return 0
    if args.cmd == "serve":
        import uvicorn

        from netmover.web.app import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=None,  # unser strukturiertes Logging
            proxy_headers=True,
            forwarded_allow_ips=settings.forwarded_allow_ips,
            server_header=False,
        )
        return 0
    if args.cmd == "reset-password":
        from netmover.db import make_engine, make_session_factory
        from netmover.models import Setting
        from netmover.web.auth import PASSWORD_KEY

        upgrade(settings.effective_database_url)
        engine = make_engine(settings.effective_database_url)
        with make_session_factory(engine)() as s:
            row = s.get(Setting, PASSWORD_KEY)
            if row is not None:
                s.delete(row)
            # Anmeldung wieder überall einschalten und Netz-Sperren aufheben
            for key, value in (
                ("app.auth_mode", '"always"'),
                ("app.trusted_networks", '""'),
                ("app.allowed_networks", '""'),
            ):
                setting = s.get(Setting, key)
                if setting is not None:
                    setting.value = value
            s.commit()
        engine.dispose()
        print("Passwort gelöscht. Dienst neu starten; der Einrichtungscode steht im Log.")
        return 0
    if args.cmd == "backup":
        return cmd_backup(settings, args.out, encrypted=not args.unverschluesselt)
    if args.cmd == "restore":
        return cmd_restore(settings, args.file, args.passphrase_file, assume_yes=args.ja)
    return 2


def service_active() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "netmover"],  # noqa: S607
        check=False,
    )
    return result.returncode == 0


def _ask_passphrase(confirm: bool) -> str:
    from netmover.backup import passphrase_problem

    first = getpass.getpass("Passphrase: ")
    if not confirm:
        return first
    problem = passphrase_problem(first, getpass.getpass("Wiederholen: "))
    if problem:
        raise SystemExit(problem)
    return first


def cmd_backup(settings: Settings, out: Path, *, encrypted: bool) -> int:
    from netmover.backup import backup_name, make_backup
    from netmover.crypto import SecretBox, load_or_create_key
    from netmover.db import make_engine, make_session_factory
    from netmover.models import utcnow
    from netmover.settings_store import load_settings

    passphrase: str | None = None
    if encrypted:
        engine = make_engine(settings.effective_database_url)
        cfg = load_settings(make_session_factory(engine))
        engine.dispose()
        if cfg.backup_passphrase_enc:
            box = SecretBox(load_or_create_key(settings.secret_key_path))
            passphrase = box.decrypt(cfg.backup_passphrase_enc)
            print("Verschlüsselt mit der Passphrase aus den Einstellungen.")
        else:
            passphrase = _ask_passphrase(confirm=True)
    now = utcnow()
    blob, manifest = make_backup(settings, passphrase, now)
    target = out / backup_name(now, encrypted) if out.is_dir() else out
    fd_path = target.with_name(f".{target.name}.part")
    fd_path.write_bytes(blob)
    fd_path.chmod(0o600)
    fd_path.replace(target)
    print(f"Sicherung geschrieben: {target} ({len(blob)} Bytes, NetMover {manifest['version']})")
    return 0


def cmd_restore(
    settings: Settings,
    file: Path,
    passphrase_file: Path | None,
    *,
    assume_yes: bool,
    active: Callable[[], bool] = service_active,
) -> int:
    from netmover.backup import BackupError, is_encrypted, open_archive, restore

    if active():
        print("Der Dienst läuft – erst `systemctl stop netmover`, dann wiederherstellen.")
        return 1
    blob = file.read_bytes()
    passphrase: str | None = None
    if is_encrypted(blob):
        passphrase = (
            passphrase_file.read_text().strip() if passphrase_file else _ask_passphrase(False)
        )
    try:
        archive = open_archive(blob, passphrase)
    except BackupError as exc:
        print(f"Fehler: {exc}")
        return 1
    m = archive.manifest
    print(
        f"Sicherung von {m.get('host')} vom {m.get('created')} (NetMover {m.get('version')}),"
        f" enthält: {', '.join(sorted(archive.files))}"
    )
    if not assume_yes and input(f"Nach {settings.data_dir} einspielen? [ja/nein] ") != "ja":
        print("Abgebrochen, nichts verändert.")
        return 1
    try:
        kept = restore(settings, archive)
    except BackupError as exc:
        print(f"Fehler: {exc}")
        return 1
    upgrade(settings.effective_database_url)
    print(f"Wiederhergestellt. Bisherige Dateien liegen in {kept}.")
    print("Jetzt `systemctl start netmover`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
