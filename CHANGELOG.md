# Changelog

Alle nennenswerten Änderungen an NetMover stehen hier.

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [Semantic Versioning](https://semver.org/lang/de/).
Die Version steht an genau einer Stelle: `src/netmover/__init__.py`.

## [Unreleased]

## [0.2.1] – 2026-10-11

### Behoben
- **Update von 0.1.x auf 0.2.0 scheiterte**: Migration 0002 brach auf einer
  Datenbank mit Anbindungen ab (`FOREIGN KEY constraint failed` beim Neuaufbau der
  Tabelle `credentials`). Weil SQLite-Migrationen nicht in einer Transaktion liefen,
  blieb dabei ein halber Zustand zurück. Migrationen laufen jetzt ohne
  Fremdschlüsselprüfung, aber mit Integritätsprüfung vor dem Commit und vollständig
  in einer Transaktion: Ein Abbruch hinterlässt die Datenbank unverändert.
  Geprüft mit Tests auf verknüpften Daten und an einer Kopie einer echten Datenbank.
- `netmover-update` zeigt beim Zurückstufen keinen irreführenden Changelog-Auszug mehr.

## [0.2.0] – 2026-10-11

> ⚠️ **Nicht verwenden** – das Update von 0.1.x scheitert an Migration 0002.
> Stattdessen 0.2.1 installieren. Wer 0.2.0 schon versucht hat: Die Datenbank aus der
> Sicherung von `netmover-update` zurückspielen und auf 0.2.1 aktualisieren.

### Hinzugefügt
- **Schlüsselverwaltung** (Seite „Schlüssel“): SSH-Schlüssel erzeugen (Ed25519) oder
  importieren (Ed25519, ECDSA, RSA ≥ 2048 Bit; OpenSSH/PEM, auch mit Passphrase),
  Fingerabdruck und `authorized_keys`-Zeile mit SFTP-Beschränkung zum Kopieren,
  Umbenennen, Löschen nur wenn unbenutzt. Private Schlüssel werden nie angezeigt.
- SFTP-Anbindungen können einen gespeicherten Schlüssel auswählen; der Benutzername
  gehört zur Anbindung (Migration 0002: `credentials.managed`, `mounts.username`).
- **`netmover-update`**: Update aus dem Git-Repo mit Sicherung von Datenbank,
  Schlüsseln und Einstellungen, Changelog-Auszug, Neustart und Healthcheck;
  Versionswahl, Warnung beim Zurückstufen.
- Installation als Git-Klon unter `/opt/netmover/src`; `install.sh` installiert
  `git` und das Update-Skript.

### Geändert
- Klartext-Hinweis bei `mount error(95)` (keine gemeinsame SMB-Version) mit
  Synology-Einstellung; aufgefallen bei der Anbindung des echten NAS.
- SFTP-Anbindungen gelten nach erfolgreichem Test als „erreichbar“ (vorher bis
  zum ersten Regellauf „nicht geprüft“).

## [0.1.1] – 2026-10-10

Korrekturen aus dem ersten Praxistest auf einer Debian-13-VM mit echten
SMB-, NFS- und SFTP-Servern.

### Behoben
- **systemd-Unit:** `ProtectSystem`, `PrivateTmp` und `ProtectControlGroups`
  gaben dem Dienst einen eigenen Mount-Namespace. Die Mounts waren für das System
  und andere Programme unsichtbar und verschwanden bei jedem Neustart des Dienstes
  (`MountFlags=shared` half nicht). Jetzt nur noch Härtungen ohne Namespace.
- **Stabilitätsprüfung:** CIFS lieferte per `stat()` sekundenlang eine veraltete
  Größe einer noch wachsenden Datei. Gemessen wird jetzt per `open()` + `fstat()`,
  das erzwingt frische Attribute. Gesperrte Dateien gelten als „in Benutzung“.
  (Die zweite Sicherung, die Byte-Zählung beim Kopieren, hatte den Fall bereits
  abgefangen; es kam keine unvollständige Datei an.)
- **Ausfall länger als die Wiederherstellung (z. B. NAS-Neustart, Stromausfall):**
  Von NetMover selbst ausgehängte Anbindungen mit „automatisch einhängen“ werden
  jetzt jede Minute erneut eingehängt, auch wenn das Einhängen schon beim
  Dienststart scheiterte. Entwarnung per Webhook. Von außen ausgehängte Anbindungen
  bleiben weiterhin unangetastet.
- Bei einer Zeitüberschreitung von `mount` wird die ganze Prozessgruppe beendet, damit
  `mount.nfs` nicht später doch noch einhängt.
- Hinweistexte zu `mount`-Fehlern werden nicht mehr mitgeschwärzt.
- `install.sh` ohne `debconf`-Warnungen, wenn kein Terminal angeschlossen ist.

### Dokumentation
- Fehlersuche: verzögert sichtbare Dateien auf CIFS → `nohandlecache`.
- README erklärt, warum die Unit keine Namespace-Härtung nutzt.

## [0.1.0] – 2026-10-10

Erste Version.

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
- Endname lokal per `renameat2(RENAME_NOREPLACE)`: echtes `rename` (Watcher sehen
  `IN_MOVED_TO`), trotzdem nie überschreibend; Rückfall auf `link` + `unlink`.
- Deployment: systemd-Unit (`ProtectSystem=full`, `MountFlags=shared`),
  Installationsskript für Debian 13, `Dockerfile` und `compose.yml` mit
  `rshared`-Propagation.
- README mit Installation, Synology→Paperless-Beispiel (SMB-Quelle, SFTP-Ziel),
  Docker, Reverse Proxy, Konfiguration, Fehlersuche und Sicherheitshinweisen.
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

[Unreleased]: https://git.wilde-server.de/florian.wilde/netmover/compare/v0.2.1...HEAD
[0.2.1]: https://git.wilde-server.de/florian.wilde/netmover/compare/v0.2.0...v0.2.1
[0.2.0]: https://git.wilde-server.de/florian.wilde/netmover/compare/v0.1.1...v0.2.0
[0.1.1]: https://git.wilde-server.de/florian.wilde/netmover/compare/v0.1.0...v0.1.1
[0.1.0]: https://git.wilde-server.de/florian.wilde/netmover/releases/tag/v0.1.0
