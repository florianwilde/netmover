"""Kommandozeile: ``netmover migrate`` / ``netmover version``.

``netmover serve`` folgt mit dem Web-Teil (Schritt 5).
"""

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
    return 2


if __name__ == "__main__":
    sys.exit(main())
