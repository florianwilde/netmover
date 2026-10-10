# Devlog

Arbeitsprotokoll mit Zeiterfassung. Ein Eintrag pro Arbeitsblock, neueste oben.
Zeiten in Ortszeit (Europe/Berlin).

## Zeitübersicht

| Datum      | Von   | Bis   | Dauer | Schritt | Thema                                  |
|------------|-------|-------|-------|---------|----------------------------------------|
| 2026-10-10 | 17:21 | 17:27 | 0:06  | 1       | Gerüst, Datenmodell, Migrationen       |
| 2026-10-10 | 18:35 | 18:48 | 0:13  | 2       | Mount-Schicht, Validierung, SFTP       |
| **Summe**  |       |       | **0:19** |      |                                        |

---

## 2026-10-10 · 18:35–18:48 · Schritt 2: Mount-Schicht, Validierung, SFTP

**Anforderungsänderung (Rücksprache)**
- Ziel ist voraussichtlich ein LXC, der nur per SSH erreichbar ist → neuer
  Anbindungstyp `sftp` (nur als Ziel). Host: VM mit Debian 13 empfohlen.
- `/healthz` zählt nur aktive Anbindungen; Python ≥ 3.12 (Debian 13: 3.13).

**Erledigt**
- `validation.py`: strikte Muster für Server/Freigabe/Pfade, Options-Allowlist
  pro Typ, gesperrte Schlüssel, Werte gegen `^[A-Za-z0-9._:/@-]+$`.
- `crypto.py`: Fernet-Schlüssel (0600, O_EXCL), Ed25519-Schlüsselerzeugung,
  Schwärzung.
- `mounting/`: `MountBackend`, `LinuxMountBackend`, `FakeMountBackend`,
  `mountinfo`-Parser (Oktal-Escapes, übereinanderliegende Mounts),
  `MountManager` (einhängen, aushängen, Wiederherstellung beim Start, Test,
  Lebenszeichen, Wiederherstellung), SFTP-Anbindung mit TOFU-Hostschlüssel.
- `timeouts.py`: Zugriffe in Daemon-Threads mit Zeitlimit (kein Pool, damit
  hängende Threads nichts blockieren).
- 113 neue Tests (123 gesamt), u. a. gegen einen echten SFTP-Server im Prozess.
- Mutationsprobe: 5 absichtlich eingebaute Fehler (Credentials-Datei bleibt liegen,
  fehlende ismount-Prüfung, kein `umount -l`, nicht-leerer Mountpoint, gesperrte
  Optionen) werden alle von Tests erkannt.

**Entscheidungen**
- Migration 0001 angepasst statt 0002 – noch kein Release, keine Datenbank im
  Einsatz. Ab v0.1.0 sind veröffentlichte Migrationen unveränderlich.
- Mount verschwunden (nicht eingehängt) → Lauf abbrechen, **kein** automatisches
  Neu-Einhängen (Fallstrick 2). Gestört (ESTALE & Co.) → Neu-Einhängen (Fallstrick 13).
- Root-Squash-Auswertung als reine Funktion (`assess_write`), damit testbar ohne
  root und ohne NAS.
- `statvfs`/`fsync` per SFTP über die OpenSSH-Erweiterungen; fehlen sie, gibt es
  einen Hinweis statt eines Fehlers.
- Ruff-Regel `PTH` abgeschaltet: die bewussten `os.open`/`os.fsync`-Aufrufe
  sind hier richtig.

**Stolpersteine**
- Funktionen und Klassen mit dem Präfix `test_`/`Test` in Produktionscode würden
  von pytest beim Import gesammelt → `check_connection`, `CheckReport`.

**Offen / nächster Schritt**
- Schritt 3: Mover (Stabilität, Atomarität, Kollisionen, Kopier-Buchführung,
  Worker mit Zeitlimit, Quarantäne) mit Zielen lokal/Kernel-Mount und SFTP.

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
