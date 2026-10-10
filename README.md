# NetMover

Selbst gehosteter Dienst mit Web-Oberfläche, der SMB/CIFS- und NFS-Freigaben
verwaltet und Dateien zeitgesteuert zwischen ihnen verschiebt.

> Status: in Entwicklung (Schritt 3 von 6). Vollständige Doku folgt mit Schritt 6.

## Entwicklung

```bash
python3 -m venv .venv            # oder: --without-pip + get-pip.py
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
NETMOVER_DATA_DIR=$PWD/data .venv/bin/netmover migrate
```

Neue Migration nach Modelländerung:

```bash
NETMOVER_DATA_DIR=$PWD/data .venv/bin/alembic revision --autogenerate -m "…"
```

Veröffentlichte Migrationen (ab v0.1.0) werden nie mehr geändert, nur ergänzt.

## Arbeitsweise

- Commits nach [Conventional Commits](https://www.conventionalcommits.org/de/)
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:` …).
- Jede nutzerrelevante Änderung bekommt einen Eintrag unter `[Unreleased]` in
  [CHANGELOG.md](CHANGELOG.md). Beim Release wird der Abschnitt versioniert,
  die Version in `src/netmover/__init__.py` angehoben und ein Tag `vX.Y.Z` gesetzt.
- Arbeitszeit und Entscheidungen: [DEVLOG.md](DEVLOG.md).
