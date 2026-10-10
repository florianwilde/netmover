# Changelog

Alle nennenswerten Änderungen an NetMover stehen hier.

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [Semantic Versioning](https://semver.org/lang/de/).
Die Version steht an genau einer Stelle: `src/netmover/__init__.py`.

## [Unreleased]

### Hinzugefügt
- Projektgerüst (Python ≥ 3.12, FastAPI-Stack, src-Layout, ruff, mypy strict, pytest).
- Datenmodell mit SQLAlchemy 2.x: `credentials`, `mounts`, `rules`, `runs`,
  `run_items`, `copied_files`, `file_failures`, `settings`.
- Alembic-Migration `0001` (initiales Schema); NetMover migriert beim Start selbst.
- Zeitzonenbewusste Zeitstempel (UTC) auch auf SQLite.
- Strukturierte Logs (logfmt oder JSON, ein Ereignis pro Zeile).
- CLI `netmover migrate` / `netmover version`.

### Behoben
- Löschen von Zugangsdaten, die noch von einem Mount benutzt werden, trennt den
  Mount nicht mehr stillschweigend ab, sondern wird von der Datenbank abgelehnt.
