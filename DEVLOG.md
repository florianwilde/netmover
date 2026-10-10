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
| 2026-10-10 | 22:45 | 23:00 | 0:15  | –       | Echtes NAS/Paperless anbinden          |
| 2026-10-10 | 23:31 | 23:58 | 0:27  | –       | Schlüsselverwaltung, Update-Skript, 0.2.x |
| 2026-10-11 | 00:13 | 00:32 | 0:19  | –       | Menü, Anleitung, Roadmap, Uptime Kuma (0.3.0) |
| 2026-10-11 | 00:31 | 00:50 | 0:19  | –       | Pausieren, Wartungsmodus, E-Mail (0.4.0) |
| 2026-10-11 | 00:54 | 01:16 | 0:22  | –       | SFTP-Quelle, Zugangsdaten, Anmeldung (0.5.0) |
| **Summe**  |       |       | **3:17** |      |                                        |

---

## 2026-10-11 · 00:54–01:16 · SFTP-Quelle, Zugangsdaten, Anmeldung abschaltbar (0.5.0)

**Erledigt**
- `mover/sources.py`: `Source` mit `LocalSource`/`SftpSource`; Engine greift nur noch
  darüber auf die Quelle zu (Scan, frischer Zustand, Lesen, Löschen, Quarantäne).
  Ziele bekommen statt `Path` ein „Readable“ (Adapter) – Destinations fast unverändert.
- `credentials.py` (vorher `keys.py`): gespeicherte Passwörter; Seite „Zugangsdaten“.
- Anmeldung: Modus immer / vertraute Netze / nie, zentral im `guard`.
- 26 neue Tests (325 gesamt); alle bisherigen Mover-Tests bestanden den Umbau
  unverändert (nur ein Monkeypatch-Ziel verschoben).

**Entscheidungen**
- SFTP-Quelle: Stabilität über Größe + sekundengenaue mtime, Mindestfenster 2 s;
  Verbindungsabbruch → `ENOTCONN` → Lauf abgebrochen (Anbindung gestört).
- Gespeicherte Passwörter tragen den Benutzer selbst (anders als Schlüssel, deren
  Benutzer zur Anbindung gehört).
- „Passworteingabe deaktivieren“ als Login-Modus umgesetzt, mit Bestätigung durch
  das Passwort, Warnhinweis und Notausgang über `reset-password`. Ein Passwort bleibt
  Pflicht (Ersteinrichtung), sonst ließe sich die Anmeldung nie wieder einschalten.

**Stolpersteine**
- paramiko `prefetch()` braucht `fstat`; der Test-Server konnte es nicht – jetzt
  kann er es, und die Quelle übergibt die bekannte Größe (spart einen Umlauf).
- Zwei TestClients mit eigenem Lebenszyklus starteten den Scheduler doppelt →
  zweiter Client ohne Lebenszyklus auf derselben App.

## 2026-10-11 · 00:31–00:50 · Pausieren, Wartungsmodus, E-Mail (0.4.0)

**Erledigt**
- Wartezustand (`rules.waiting_since/_reason`, Migration 0003), Wartungsmodus,
  E-Mail (`mail.py`, SMTP), eine Meldung pro Anbindung mit betroffenen Regeln.
- 0.4.0/0.5.0 aufgeteilt: kleinere Updates, schneller Nutzen.
- 21 neue Tests (299 gesamt), u. a. SMTP gegen echten Server (aiosmtpd) mit
  STARTTLS, SSL, falschem Passwort und nicht vertrauenswürdigem Zertifikat.

**Entscheidungen**
- Warten statt Deaktivieren; kein Fehlerzähler und nur ein Protokolleintrag beim
  Eintritt; Ende des Wartens automatisch beim nächsten durchkommenden Lauf.
- Benachrichtigung pro Anbindung (nicht pro Regel) – sonst bei drei Regeln am NAS
  drei gleiche Meldungen. Lücke geschlossen: Aushängen von außen meldete nichts.
- Wartungsmodus unterdrückt nur Regel-Alarme, Anbindungen werden weiter geprüft.
- SMTP prüft Zertifikate immer; für Tests ist nur `mail.tls_context()` ersetzbar.

**Stolpersteine (alle im Testaufbau)**
- Globales Monkeypatchen von `ssl.create_default_context` traf auch den
  Testserver → eigene Funktion `tls_context()`.
- aiosmtpd: `AuthResult(success=False)` ohne `handled=False` schickt keine Antwort →
  Client hängt bis zum Zeitlimit.

**Migrationen** – 0003 baut `rules` neu; getestet von 0001 und von 0002 mit allen
verknüpften Tabellen sowie an der befüllten Demo-Datenbank; vor dem Release an einer
Kopie der Live-Datenbank auf der VM.

## 2026-10-11 · 00:13–00:32 · Menü, Anleitung, Roadmap, Uptime Kuma (0.3.0)

**Erledigt**
- Seitenleiste mit Gruppen, Symbolen (Inline-SVG), Zählern und Statusampel;
  Handy-Menü per Knopf (ohne JS sichtbar).
- `docs/ANLEITUNG.md` (für Anwender) mit Sprungmarken; Hilfe-Seite rendert sie per
  Python-Markdown, das Paket liefert `docs/` mit (hatch force-include).
- `ROADMAP.md`; Nutzerwünsche eingeplant: SFTP als Quelle, gespeicherte Zugangsdaten,
  automatisches Pausieren/Fortsetzen, E-Mail (0.4.0).
- Uptime Kuma: ausführliches `/healthz`, Einzel-Endpunkte, Push-Job.
- 13 neue Tests (278 gesamt).

**Wie geprüft**
- Erstmals echte Screenshots mit Playwright (Chromium, Scratchpad-venv) gegen eine
  Demo-Instanz (Fake-Backend), Desktop 1366 px und Handy 390 px, hell und dunkel.
  Daraus gefunden und behoben: überflüssiges Passwortfeld/SMB-Hinweise bei SFTP,
  sperriges Umbenennen auf der Schlüsselseite, Tabellen in der Hilfe ragten auf
  dem Handy über den Rand. Dieselben Screenshots sind jetzt in der Anleitung
  (128-Farben-PNG, zusammen ~150 KB; Demo-Pfad per DOM durch `/mnt/netmover` ersetzt).
- Ein echter Fehler, den erst der Blick auf das HTML zeigte: `{{ 'attr="x"' if … }}`
  wird von Jinja maskiert → `aria-current` und `disabled title` wirkten nicht.

**Entscheidungen**
- Automatisches Pausieren: kein Deaktivieren (müsste nach jedem NAS-Neustart von
  Hand rückgängig gemacht werden), sondern „wartet auf Anbindung“ mit
  automatischem Fortsetzen – für 0.4.0 vorgeschlagen.
- Regel mit 3 Fehlschlägen in Folge zählt jetzt als Problem in `/healthz`.

## 2026-10-10 · 23:31–23:58 · Schlüsselverwaltung, Update-Skript, Releases 0.2.0/0.2.1

**Erledigt**
- Schlüsselverwaltung (`keys.py`, Seite „Schlüssel“, Auswahl in SFTP-Anbindungen),
  Migration 0002 (`credentials.managed`, `mounts.username`).
- `deploy/update.sh` → `netmover-update`: Tags holen, Sicherung (SQLite-Online-
  Backup + Schlüssel + env), Checkout, `install.sh`, Healthcheck, Rückweg-Anleitung.
- VM auf Git-Klon `/opt/netmover/src` umgestellt (Repo ist öffentlich lesbar).

**Zwischenfall beim Update der VM auf 0.2.0**
- Migration 0002 scheiterte: `FOREIGN KEY constraint failed` bei `DROP TABLE
  credentials` (Alembic-Batch baut die Tabelle neu; `foreign_keys=ON`, und die
  Anbindungen verweisen darauf). Danach crashte jeder Neustart an der
  übriggebliebenen `_alembic_tmp_credentials` (DDL lief ohne Transaktion).
- `netmover-update` hat den Fehler erkannt und die Rückweg-Befehle genannt.
  Sicherung zurückgespielt, mit `netmover-update v0.1.1` zurückgestuft – in
  ~1 Minute wieder lauffähig, NAS-Mount blieb durchgehend eingehängt.
- Ursache, warum die Tests es nicht fanden: Migrationen wurden nur auf **leeren**
  Datenbanken getestet. Neu: `tests/test_migrations.py` mit verknüpften Daten und
  mit Abbruch mitten in der Migration (Datenbank muss unverändert bleiben).
- Fix: eigene Migrationsverbindung (FK aus, `BEGIN IMMEDIATE`, `transactional_ddl`,
  `foreign_key_check` vor dem Commit). Vor dem Release an einer Kopie der echten
  Datenbank auf der VM geprüft.
- Tag v0.2.0 bleibt (veröffentlicht), im Changelog als „nicht verwenden“ markiert.

**Lehre**
- Jede künftige Migration bekommt einen Test auf einer befüllten Datenbank.

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
