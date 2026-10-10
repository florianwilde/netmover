# Devlog

Arbeitsprotokoll mit Zeiterfassung. Ein Eintrag pro Arbeitsblock, neueste oben.
Zeiten in Ortszeit (Europe/Berlin).

## Zeitübersicht

| Datum      | Von   | Bis   | Dauer | Schritt | Thema                                  |
|------------|-------|-------|-------|---------|----------------------------------------|
| 2026-10-10 | 17:21 | 17:27 | 0:06  | 1       | Gerüst, Datenmodell, Migrationen       |
| 2026-10-10 | 18:35 | 18:48 | 0:13  | 2       | Mount-Schicht, Validierung, SFTP       |
| 2026-10-10 | 18:53 | 19:07 | 0:14  | 3       | Mover-Logik                            |
| 2026-10-10 | 19:10 | 19:17 | 0:07  | 4       | Scheduler, Protokoll, Benachrichtigung |
| 2026-10-10 | 19:18 | 19:34 | 0:16  | 5       | Weboberfläche und Anmeldung            |
| 2026-10-10 | 19:40 | 19:50 | 0:10  | 6       | Deployment, README, Release 0.1.0      |
| 2026-10-10 | 21:36 | 22:05 | 0:29  | –       | Praxistest auf VM, Release 0.1.1       |
| **Summe**  |       |       | **1:35** |      |                                        |

---

## 2026-10-10 · 21:36–22:05 · Praxistest auf der Debian-13-VM, Release 0.1.1

**Umgebung**
- KVM-VM `srv-rz-netmover` (10.0.10.223), Debian 13, Python 3.13, 2 GB RAM.
- Installation per `deploy/install.sh` aus einem `git archive` (VM ohne Gitea-Zugang).
- Vorübergehende Testserver auf der VM: Samba (nur `lo`), NFS-Exporte für
  `127.0.0.1` (mit/ohne Root-Squash), SFTP-Benutzer `paperless` mit Schlüssel
  `restrict,command="/usr/lib/openssh/sftp-server"`.
- Auf Wunsch `NETMOVER_HOST=0.0.0.0`, damit die Oberfläche im LAN erreichbar ist.

**Bestanden**
- Ersteinrichtung mit Code aus dem Log, Cookie-Flags.
- CIFS (Passwort mit Komma/Ausrufezeichen, Credentials-Datei danach weg), NFS 4,
  NFS 3, Root-Squash-Erkennung mit Synology-Hinweis, SFTP mit TOFU (Fingerabdruck
  = `ssh-keygen -lf`), echtes OpenSSH liefert `statvfs`.
- Probelauf, Kollision `scan001-1.pdf`, `.txt`/`.part` ignoriert, keine `.part`-Reste,
  Eigentümer `paperless` im SFTP-Ziel.
- Langsam wachsende Datei (20 × 50 KB/s): erst nach Ende verschoben, Prüfsumme gleich.
- Kopiermodus rekursiv mit Eigentümer/Modus; zweiter Lauf „2 bereits übertragen“.
- Samba gestoppt: Lebenszeichen 15 s → gestört → 3 Versuche → Fehler, `/healthz` 503,
  nichts angefasst. NFS-Server gestoppt (hängender `hard`-Mount): Oberfläche und
  andere Regel liefen weiter, Rückkehr automatisch.
- Mounts überstehen Stopp/Start des Dienstes (gleiche Mount-IDs).
- Neustart der VM: NFS sofort, SMB per Neuversuch nach ~1 min, Regel läuft weiter.

**Gefunden und behoben (v0.1.1)**
1. **systemd-Unit mit eigenem Mount-Namespace** (ProtectSystem/PrivateTmp/…):
   Mounts nur im Dienst sichtbar, `shared,slave`. Härtung ohne Namespace.
2. **CIFS-Attribut-Cache**: `stat()` meldete 3 s lang eine veraltete Größe einer
   wachsenden Datei; die Byte-Zählung beim Kopieren fing das ab. Jetzt
   `open()` + `fstat()` für beide Messungen.
3. **Nach drei gescheiterten Versuchen endgültig aufgegeben** – zu kurz für einen
   NAS-Neustart, und beim Booten vor dem NAS kam die Freigabe nie zurück.
   Jetzt minütlicher Neuversuch (nur für von NetMover selbst ausgehängte
   Anbindungen mit `auto_mount`) plus Scheduler-Job.
4. `mount`-Zeitlimit beendete nur `mount`, nicht `mount.nfs` → später doch
   eingehängt. Jetzt ganze Prozessgruppe.
5. Hinweistext wurde mitgeschwärzt; `debconf`-Warnungen in `install.sh`.

**Nicht nachstellbar**
- Einmalig 46 s Verzögerung direkt nach dem Booten (CIFS-Verzeichnis-Cache bei
  lokal statt per SMB angelegter Datei). Messungen danach 9–11 s, mit und ohne
  `nohandlecache`. Als Hinweis in die Fehlersuche aufgenommen.

## 2026-10-10 · 19:40–19:50 · Schritt 6: Deployment, README, Release 0.1.0

**Erledigt**
- `deploy/netmover.service`, `deploy/netmover.env`, `deploy/install.sh`,
  `Dockerfile` (python:3.12-slim-trixie, mehrstufig), `compose.yml`, `.dockerignore`.
- README vollständig; `LICENSE` (MIT, wie seit Schritt 1 in `pyproject.toml`).
- Version 0.1.0, Changelog-Abschnitt, Tag `v0.1.0`.

**Geprüft**
- Wheel enthält Templates, statische Dateien und Migrationen.
- Docker-Image gebaut; Container startet, `/healthz` = 200, `mount.cifs` 7.4 und
  `mount.nfs` vorhanden. **Propagation getestet:** tmpfs im Container unter
  `/mnt/netmover` eingehängt → mit `rshared` auf dem Host sichtbar, ohne nicht.
- `systemd-analyze verify` der Unit ohne Beanstandung. Ein echter Start der Unit
  als root (insb. `MountFlags=shared` mit `ProtectSystem`) war hier nicht möglich →
  beim ersten Einsatz mit `findmnt -R /mnt/netmover` prüfen.

**Entscheidungen**
- Paperless-ngx-Consumer geprüft: nutzt `watchfiles` (`Change.added`) plus
  5-Minuten-Rescan – erkennt also auch per `link()` erzeugte Dateien. Für Watcher,
  die nur `IN_MOVED_TO`/`IN_CLOSE_WRITE` kennen, trotzdem auf
  `renameat2(RENAME_NOREPLACE)` umgestellt (entspricht der Vorgabe `os.replace`,
  ohne die Garantie „nie überschreiben“ aufzugeben).
- Härtung der Unit bewusst zurückhaltend (keine `CapabilityBoundingSet`, kein
  `ProtectKernelModules`): ungetestete Einschränkungen könnten Mounten brechen
  (z. B. NFS mit privilegiertem Quellport, Modul-Autoload für cifs).
- SFTP-Schlüssel im README per `restrict,command="…/sftp-server"` auf SFTP beschränkt.

**Offen für später**
- Erster Praxistest auf der Ziel-VM mit echtem Synology-NAS und Paperless-LXC.

## 2026-10-10 · 19:18–19:34 · Schritt 5: Weboberfläche und Anmeldung

**Erledigt**
- `admin.py` (Formulare prüfen/speichern, frameworkunabhängig), `transfer.py`
  (Export/Import), `web/auth.py`, `web/app.py`, 14 Templates, `app.css`, `app.js`.
- htmx 2.0.4 lokal eingebunden (SHA-256
  `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`).
- 25 Web-Tests (232 gesamt); Rauchtest mit echtem `netmover serve`.

**Entscheidungen**
- Einmaliger Einrichtungscode im Log, damit nicht irgendwer im LAN beim ersten
  Aufruf das Admin-Passwort setzt.
- Strikte CSP (`script-src 'self'; style-src 'self'`) – deshalb keine
  Inline-Styles; Füllstand als `<meter>`; htmx-Indikator-Styles abgeschaltet.
- Jede Anbindung pflegt eigene Zugangsdaten (Datenmodell erlaubt Teilen, die
  Oberfläche bleibt dadurch einfacher).
- Verbindungsdaten nur im ausgehängten Zustand änderbar; SFTP-Hostschlüssel wird
  bei Server-/Portwechsel verworfen.
- „Jetzt ausführen“ startet im Hintergrund (Ergebnis im Protokoll); „Probelauf“
  wartet und zeigt die Tabelle.
- Webhook-URL wird nur gekürzt angezeigt; unverändert zurückgeschickte Maske = behalten.
- Import: vorhandene Namen überspringen, importierte Regeln deaktiviert,
  CIFS-Anbindungen ohne `auto_mount` (Zugangsdaten fehlen).
- APScheduler-Logs auf WARNING (sonst alle 30 s eine Zeile pro Job).

**Stolpersteine**
- Starlette-Session erkennt `setdefault(...).append()` nicht als Änderung →
  zweite Flash-Meldung ging verloren. Jetzt Neuzuweisung.
- `request.form()` liefert `starlette.datastructures.UploadFile`, nicht die
  FastAPI-Unterklasse → Import erkannte nie eine Datei.
- Settings-Import öffnete eine zweite DB-Verbindung in offener Schreib-
  transaktion (SQLite: „database is locked“) → Einstellungen nach dem Commit.

## 2026-10-10 · 19:10–19:17 · Schritt 4: Scheduler und Protokollierung

**Erledigt**
- `settings_store.py` (Laufzeit-Einstellungen), `notify.py` (Webhook),
  `runs.py` (Lauf ausführen + protokollieren, Zähler, Aufräumen),
  `health.py`, `scheduler.py`, `app_state.py` (Verdrahtung, Start/Stop).
- 24 neue Tests (207 gesamt). Mutationsprobe: 6/6 erkannt.

**Entscheidungen**
- Erfolg = `ok` oder `partial` (Werkzeug arbeitet); Fehlschlag = `error` oder
  `partial` (für Benachrichtigungen). Übersprungen zählt nicht; manueller
  Probelauf ändert keine Zähler.
- Bezug für „zu lange kein Erfolg“: spätester von letztem Erfolg, Anlage der
  Regel und Dienststart – sonst wäre nach einem Neustart sofort alles rot.
- „Nach N Fehlern“ meldet genau einmal beim Erreichen der Schwelle, danach
  Entwarnung beim ersten Erfolg (abschaltbar).
- `max_instances=3` im Scheduler, damit überlappende Termine beim eigenen
  Regel-Lock ankommen und protokolliert werden, statt dass APScheduler sie still verwirft.
- Leere Läufe (alle 30 s) loggen nur auf DEBUG; in `runs` stehen sie trotzdem
  (die Oberfläche filtert standardmäßig auf Läufe mit Aktivität).
- Webhook-URL wird nie geloggt (enthält oft ein Token).

**Stolpersteine**
- Zwei Testfehler lagen in den Tests selbst (Fake-Symlink bei `resolve()`,
  Jitter in der Zeitschranke vergessen).

## 2026-10-10 · 18:53–19:07 · Schritt 3: Mover-Logik

**Erledigt**
- Remote `origin` (Gitea) war eingerichtet → `main` gepusht.
- `mover/scan.py`, `naming.py`, `ledger.py`, `destinations.py` (lokal/Kernel-Mount
  und SFTP), `engine.py` (Ablauf pro Lauf), `runner.py` (Lock, Lebenszeichen,
  Watchdog, Ziel-Aufbau).
- `timeouts.run_with_watchdog`: Inaktivitäts-Zeitlimit mit `CancelToken`.
- 60 neue Tests (183 gesamt), u. a. Abbruch in jeder Phase, Wettlauf um
  Dateinamen, hängender Lauf, SFTP-Ziel mit Kollision gegen echten SFTP-Server.
- Mutationsprobe: 9 eingebaute Fehler (keine Stabilitätsprüfung, kein fsync,
  keine Quellprüfung, Wettlauf, direkt unter Endnamen schreiben, keine
  Buchführung, keine Platzprüfung, Lock-Freigabe, kein Watchdog). Zwei überlebten
  zunächst (fsync, enges Wettlauf-Fenster) → zwei Tests ergänzt; jetzt alle erkannt.

**Entscheidungen**
- Zeitlimit als **Inaktivitäts**-Watchdog statt Gesamtlaufzeit: große Dateien
  über langsame Leitungen werden nicht abgebrochen, hängende Zugriffe schon.
- Regel-Lock gibt erst der Worker selbst frei. Nach einer Zeitüberschreitung
  bleibt die Regel blockiert, bis der hängende Worker endet – so kopieren nie
  zwei Läufe dieselbe Datei gleichzeitig. Statusseite zeigt „läuft seit …“.
- Stabilität innerhalb eines Laufs (zweite Messung nach dem Fenster) plus
  Quellprüfung direkt vor dem Umbenennen.
- Umbenennen ohne Überschreiben per `link()` (atomar, EEXIST bei Kollision);
  ohne Hardlink-Unterstützung (oft CIFS) Rückfall auf „prüfen + rename“.
- Bei Rekursion wird die Ordnerstruktur im Ziel beibehalten; Trennung nach
  Quell-Unterordnern geht über mehrere Regeln mit eigenem Unterpfad.
- Quarantänepfad ist relativ zur Quell-Anbindung (gleiches Dateisystem → rename).
- `copied_files` wird nach jedem *vollständigen* Scan bereinigt (statt nur beim
  Protokoll-Aufräumen) – zeitnäher, und nie nach einem abgebrochenen Scan.
- ESTALE/EIO/ENOTCONN mitten im Lauf → Lauf abbrechen (nicht jede Datei einzeln
  scheitern lassen, nicht gegen den Quarantäne-Zähler zählen).

**Stolpersteine**
- `tmp_path` liegt auf einem Gerät → Tests liefen erst über den Schnellweg
  (`link`) statt über den Kopierweg; Fixture `cross_device` erzwingt ihn.
- Prüfung „lokales Ziel nicht unter Mount-Basis“ nutzte nur `resolve()` – ein
  Symlink umgeht das. Jetzt wörtlich **und** aufgelöst.
- Lock-Übergabe an den Worker: doppelte Freigabe bei sehr schnellem Lauf
  (beim Gegenlesen gefunden, vor dem ersten Testlauf behoben).

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
