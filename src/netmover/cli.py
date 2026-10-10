"""Kommandozeile: ``netmover serve`` / ``netmover migrate`` / ``netmover version``
/ ``netmover reset-password``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from netmover import __version__
from netmover.config import get_settings
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
                s.commit()
        engine.dispose()
        print("Passwort gelöscht. Dienst neu starten; der Einrichtungscode steht im Log.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
