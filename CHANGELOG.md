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
- SFTP als Zieltyp (`sftp`) für Ziele, die nur per SSH erreichbar sind; Anmeldung
  per selbst erzeugtem Ed25519-Schlüssel oder Passwort, Hostschlüssel wird beim
  ersten Test festgehalten und danach erzwungen.
- Eingabeprüfung für Server, Freigaben, Pfade und Mount-Optionen (Allowlist pro
  Typ; `credentials`, `username`, `password`, `pass`, `domain` gesperrt).
- Fernet-Verschlüsselung der Zugangsdaten (`secret.key`, 0600, beim ersten Start
  erzeugt) und Schwärzung bekannter Geheimnisse in `mount`-Ausgaben.
- Mount-Schicht hinter `MountBackend` mit `LinuxMountBackend` (`mount`/`umount`
  nur mit Argumentliste, CIFS-Zugangsdaten über kurzlebige Datei unter
  `/run/netmover`) und `FakeMountBackend` für Tests ohne root.
- Mount-Test in drei Schritten (einhängen, lesen, schreiben) mit Rollen
  („nur Quelle, kopieren“ / „Quelle mit Löschen“ / „Ziel“) und Root-Squash-Erkennung
  inklusive Synology-Hinweis.
- Lebenszeichen mit Zeitlimit vor jedem Lauf; gestörte Mounts (ESTALE, EIO,
  ENOTCONN, Zeitüberschreitung) werden per `umount -l` bis zu dreimal mit
  wachsendem Abstand neu eingehängt.
- Mountpoints ausschließlich unter `/mnt/netmover/<slug>`, müssen vor dem
  Einhängen leer sein und werden nach dem Aushängen entfernt.
- Mover-Engine: Stabilitätsprüfung (Größe **und** mtime nach dem
  Stabilitätsfenster), unfertige/versteckte Dateien und Synology-Ordner
  (`@eaDir`, `#recycle`) werden ignoriert, Glob (mehrere Muster mit `;`) oder Regex.
- Atomares Schreiben: `.netmover-<uuid>.part` → `fsync` → Größenprüfung →
  Metadaten → Quellprüfung → Umbenennen ohne Überschreiben (`link`, Rückfall auf
  `rename`) → erst dann Quelle löschen; gleiches Gerät ohne Kopie per `link`.
- Namenskollisionen: umbenennen (`scan-1.pdf`, `scan-2.pdf`, …, jedes Mal gegen
  das Dateisystem geprüft), überspringen oder überschreiben.
- Buchführung im Kopiermodus (`copied_files`); verhindert auch Duplikate, wenn im
  Verschiebemodus die Quelle nicht gelöscht werden kann.
- Platzprüfung vor jeder Kopie (Größe + 5 %), Übernahme von mtime und Modus,
  optional Eigentümer; Metadaten-Fehler sind Hinweise, keine Fehler; `chown` auf
  CIFS und auf NFS mit Root-Squash wird übersprungen und einmalig gemeldet.
- Quarantäneordner für Dateien, die wiederholt scheitern.
- SFTP-Ziel mit demselben atomaren Ablauf (`rename` ohne Überschreiben,
  `posix-rename` beim Überschreiben, `fsync`/`statvfs` über OpenSSH-Erweiterungen).
- Regellauf mit Lock pro Regel, Lebenszeichen der Anbindungen und Worker mit
  Inaktivitäts-Zeitlimit; ein hängender Worker blockiert nur seine eigene Regel.
- Lokale Ziele unter dem Mount-Basisverzeichnis werden abgelehnt (dort muss die
  Anbindung als Ziel gewählt werden, damit „eingehängt?“ geprüft wird).
- Scheduler (APScheduler): ein Job pro aktiver Regel mit zufälligem
  Startversatz und Jitter; Läufe in eigenem Thread-Pool; Abgleich nach Änderungen.
- Protokoll: pro Lauf eine Zeile (auch übersprungene Termine), Einzeldateien
  in `run_items` (max. 2000 pro Lauf); Regelzähler letzter Lauf, letzter Erfolg,
  Fehler in Folge.
- Aufräumen der Protokolle nach konfigurierbarer Frist (Standard 30 Tage).
- Gesundheitsprüfung: Scheduler-Herzschlag, Anbindungen im Fehlerzustand,
  Regeln ohne Erfolg seit mehr als dem Dreifachen ihres Intervalls.
- Webhook-Benachrichtigung mit frei wählbarer Vorlage (JSON-sicher), Modus
  „bei jedem Fehler“ oder „nach N Fehlern in Folge“, optional Entwarnung;
  auch für Anbindungen, deren Wiederherstellung scheitert.
- Dienststart: Migration → Schlüssel → Anbindungen wiederherstellen → Scheduler;
  beim Beenden werden Mounts nicht ausgehängt.
- Weboberfläche (FastAPI, Jinja2, HTMX ohne Build-Schritt, handyfähig, Hell/Dunkel):
  Status, Anbindungen, Regeln, Protokoll, Einstellungen.
- Ersteinrichtung mit Pflicht-Passwort (≥ 10 Zeichen) und einmaligem
  Einrichtungscode aus dem Log; Anmeldung mit Argon2, Sperre nach 5 Fehlversuchen.
- Session-Cookie `HttpOnly`, `SameSite=Lax` (optional `Secure`), CSRF-Token bei
  jedem POST, strikte Content-Security-Policy ohne Inline-Skripte/-Styles.
- Mount-Formular mit „Testen“ vor dem Speichern; Passwörter werden nie
  angezeigt (leer = unverändert); SFTP-Schlüsselpaar wird erzeugt und der
  öffentliche Schlüssel zum Kopieren angezeigt.
- Probelauf zeigt in einer Tabelle, welche Datei wohin ginge.
- Protokoll filterbar nach Regel und Status, Einzeldateien aufklappbar.
- Export/Import der Konfiguration als JSON (ohne Zugangsdaten und Webhook-URL).
- `GET /healthz`, `netmover serve`, `netmover reset-password`.

### Behoben
- Import-Upload wurde nie erkannt (falsche `UploadFile`-Klasse geprüft).
- Hinweise nach Aktionen gingen verloren, wenn schon ein Hinweis in der
  Session stand (Session erkennt nur Neuzuweisungen als Änderung).
- Löschen von Zugangsdaten, die noch von einem Mount benutzt werden, trennt den
  Mount nicht mehr stillschweigend ab, sondern wird von der Datenbank abgelehnt.
