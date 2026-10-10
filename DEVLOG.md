# Devlog

Arbeitsprotokoll mit Zeiterfassung. Ein Eintrag pro Arbeitsblock, neueste oben.
Zeiten in Ortszeit (Europe/Berlin).

## Zeitübersicht

| Datum      | Von   | Bis   | Dauer | Schritt | Thema                                  |
|------------|-------|-------|-------|---------|----------------------------------------|
| 2026-10-10 | 17:21 | 17:27 | 0:06  | 1       | Gerüst, Datenmodell, Migrationen       |
| **Summe**  |       |       | **0:06** |      |                                        |

---

## 2026-10-10 · 17:21–17:27 · Schritt 1: Gerüst, Datenmodell, Migrationen

**Erledigt**
- Repo angelegt, venv (lokal Python 3.14; Code zielt auf ≥ 3.12, Debian 13 liefert 3.13).
  `python3-venv` fehlt auf der Entwicklungsmaschine → venv mit `--without-pip` + `get-pip`.
- `pyproject.toml` (hatchling, src-Layout), ruff + mypy strict.
- Datenmodell nach Spezifikation plus Ergänzungen (siehe Docstring `models.py`):
  `credentials.domain`, `mounts.state_since/write_ok/root_squash/last_test_*`,
  `rules.op_timeout_seconds/quarantine_after`, `runs.status/trigger/dry_run`,
  Tabelle `file_failures`, `copied_files.mtime_ns` statt Float-`mtime`.
- Alembic-Migration 0001; Migrationen liegen im Paket, damit sie mit installiert werden.
- 10 Tests: Schema = Modell (kein Drift), Down-/Upgrade, Defaults, UTC-Roundtrip,
  RESTRICT bei benutzten Mounts/Zugangsdaten, CASCADE bei Regeln, Eindeutigkeit
  in `copied_files`, CHECK-Constraints der Enums.

**Entscheidungen**
- Enums als `VARCHAR` + `CHECK` (kein natives Enum; SQLite kann das ohnehin nicht).
- `UTCDateTime` lehnt naive `datetime` ab, statt sie still als UTC zu interpretieren.
- SQLite mit WAL, `foreign_keys=ON`, `busy_timeout=5000` (Scheduler + Web + Worker-Threads).

**Stolpersteine**
- Alembic-Autogenerate erzeugt CHECK-Constraints bei `Enum(create_constraint=True)`
  dreifach → explizite `CheckConstraint`-Zeilen aus der Migration entfernt.
- Test deckte auf: ORM setzt ohne `passive_deletes` `mounts.cred_id` auf NULL,
  statt das `RESTRICT` greifen zu lassen. Behoben.

**Offen / nächster Schritt**
- Schritt 2: Mount-Schicht, Validierung, `FakeMountBackend`, Fernet-Verschlüsselung.
