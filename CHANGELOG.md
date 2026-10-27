# Changelog

Alle nennenswerten Änderungen an NetMover stehen hier.

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [Semantic Versioning](https://semver.org/lang/de/).
Die Version steht an genau einer Stelle: `src/netmover/__init__.py`.

## [Unreleased]

### Hinzugefügt
- Releases mit dem Changelog-Abschnitt als Text: `scripts/github_release.py`
  (einzeln oder `--all`) und automatisch per CI bei jedem Versions-Tag.

### Geändert
- **Umzug von Gitea nach GitHub** (`github.com/florianwilde/netmover`): CI läuft als
  GitHub Actions (`.github/workflows/ci.yml`), Releases entstehen auf GitHub;
  Installations- und Update-Skript, systemd-Unit, Hilfe-Seite und Doku zeigen auf
  GitHub. Bestehende Installationen stellen die Quelle einmalig um (README,
  „Update“); für das private Repository gibt es einen Deploy Key (README,
  „Installation“).

## [0.11.0] – 2026-10-24

### Hinzugefügt
- **Mehrere Ziele je Regel**: neben dem Hauptziel bis zu 9 weitere Ziele (Anbindung
  oder lokaler Pfad, je mit Bezeichnung, Ordner und Ablage flach/nach Datum) – z. B.
  Rechnung ins DMS, in die Buchhaltung und ins Archiv. Weitere Ziele werden vor dem
  Hauptziel beliefert, jedes mit eigener Buchführung; die Quelle wird erst gelöscht,
  wenn alle Ziele die Datei haben.
- **Ein nicht erreichbares Ziel hält die anderen nicht auf**: Die erreichbaren Ziele
  (auch das Hauptziel) werden beliefert, die Quelle bleibt liegen, bis das fehlende
  Ziel zurück ist und nachbeliefert wurde. Die Regel gilt solange als wartend;
  Wiederholungen während des Ausfalls erzeugen keine Protokolleinträge.
- Probelauf zeigt für jedes Ziel den künftigen Pfad.
- Im Kopiermodus bekommt ein neu hinzugefügtes Ziel auch die schon vorhandenen Dateien.
- Anleitung: Beispiel „Rechnungseingang: DMS, Buchhaltung und Archiv“.

### Geändert
- Das Archiv ist jetzt ein weiteres Ziel. Migration 0005 übernimmt vorhandene Archive
  als Ziel „Archiv“ samt Buchführung (`rule_targets`, `target_files` statt
  `rules.archive_*`, `archived_files`). Downgrade: nur das erste weitere Ziel bleibt.
- Export enthält die Ziele (`targets`); Exporte älterer Versionen mit Archiv werden
  beim Import umgewandelt.
- Regelformular: „Ziel“ heißt jetzt „Hauptziel“, „Archiv“ wird zu „Weitere Ziele“.

## [0.10.0] – 2026-10-23

### Hinzugefügt
- **Tägliche Datensicherung** (*System → Sicherung*): Datenbank, `secret.key`,
  `session.key` und `netmover.env` als mit Passphrase verschlüsselte Datei
  (scrypt + Fernet) auf eine Anbindung (SMB, NFS oder SFTP). Momentaufnahme über die
  SQLite-Backup-API mit Integritätsprüfung; die Datei wird vor dem Hochladen
  probeweise geöffnet und atomar geschrieben; ältere NetMover-Sicherungen über die
  eingestellte Anzahl hinaus werden gelöscht, andere Dateien nie. Verpasste Termine
  werden nachgeholt, nach einem Fehler stündlich neuer Versuch, erster Fehlschlag
  per E-Mail/Webhook. Knopf „Jetzt sichern“.
- `netmover restore <datei>`: spielt eine Sicherung ein (nur bei gestopptem Dienst),
  legt die bisherigen Dateien vorher nach `vor-wiederherstellung-<zeit>/` und
  migriert die Datenbank. `netmover backup <ziel>` sichert von Hand in eine Datei.
- Befehl `netmover` im Pfad (`/usr/local/sbin/netmover`, lädt `netmover.env`).
- **Kennzahlen für Uptime Kuma**: `/healthz` und `/healthz/rule/<id>` liefern unter
  `metrics` Dateien, Fehler, Quarantäne und abgebrochene Läufe der letzten 24 Stunden
  sowie die letzte verschobene Datei – für Kuma-Monitore vom Typ „JSON-Abfrage“.
  Der Push meldet z. B. „OK · 24 h: 12 Dateien“.
- `/healthz/backup`: eigener Monitor; eine seit zwei Tagen überfällige Sicherung gilt
  als Problem (Statusampel, `/healthz`, Push).
- **CI in Gitea** (`.gitea/workflows/ci.yml`): ruff, mypy, shellcheck und Tests auf
  Python 3.12 und 3.13 bei jedem Push.
- Test-Pflicht für Migrationen: jede Migration wird automatisch auf einer befüllten
  Datenbank hoch- und heruntergestuft.

### Geändert
- README: HTTP im internen Netz als bewusste Entscheidung dokumentiert; TLS über
  Reverse Proxy bleibt der Weg für nicht vertrauenswürdige Netze.

## [0.9.0] – 2026-10-15

### Hinzugefügt
- **Regeln kopieren** (Knopf „Kopieren“): alle Einstellungen inklusive Archiv
  übernommen, Name „… (Kopie)“. Die Kopie ist zunächst **deaktiviert**, damit sie nicht
  mit dem Original um dieselben Dateien konkurriert; Protokoll und Buchführung
  beginnen neu.

## [0.8.0] – 2026-10-12

### Hinzugefügt
- **Anbindungen kopieren** (Knopf „Kopieren“): Formular vorausgefüllt, Name
  „… (Kopie)“. Eigene Zugangsdaten der Vorlage werden übernommen (eigenes Exemplar),
  solange kein neues Passwort eingegeben wird; ein eigener SSH-Schlüssel wird
  weiterverwendet; gespeicherte Zugangsdaten bleiben ausgewählt. „Testen“ im
  Kopie-Formular nutzt die übernommenen Zugangsdaten. Der SFTP-Hostschlüssel wird nur
  bei gleichem Server und Port übernommen.

## [0.7.0] – 2026-10-12

### Hinzugefügt
- **Archiv je Regel:** zusätzliche Kopie in ein Archiv (Anbindung SMB/NFS/SFTP oder
  lokaler Pfad), Ablage „wie in der Quelle“ oder „nach Datum“ (`Jahr/Monat/…`).
  Reihenfolge: erst Archiv, dann Ziel, **erst dann wird die Quelle gelöscht**.
  Scheitert das Ziel nach dem Archiv, verhindert eine Buchführung
  (`archived_files`) eine zweite Archivkopie. Im Archiv wird nie überschrieben.
  Nicht erreichbares Archiv → Regel wartet. Platzprüfung auch fürs Archiv,
  Probelauf zeigt das Archivziel. Migration 0004.
- Export/Import enthalten die Archiv-Einstellungen; eine als Archiv benutzte
  Anbindung lässt sich nicht löschen.

## [0.6.0] – 2026-10-11

### Geändert
- **Anmeldung je Netz konfigurierbar** (*Einstellungen → Anmeldung*): „Ohne Passwort
  aus diesen Netzen“ und „Mit Passwort aus diesen Netzen“ (leer = alle übrigen).
  Ist die zweite Liste ausgefüllt, werden alle anderen Netze abgewiesen (403, auch die
  Anmeldeseite); `/healthz` und statische Dateien bleiben erreichbar. Ersetzt die
  Auswahl „immer / vertraute Netze / nie“ aus 0.5.0 (`0.0.0.0/0, ::/0` = ganz aus;
  gespeicherter Modus „nie“ gilt weiter).
- Schutz vor Aussperren: Eine Einstellung, mit der die eigene Adresse keinen Zugang
  mehr hätte, wird nicht gespeichert. `netmover reset-password` hebt auch Netz-Sperren auf.

## [0.5.0] – 2026-10-11

### Hinzugefügt
- **SFTP als Quelle** mit denselben Garantien wie bei SMB/NFS: Stabilitätsprüfung,
  atomares Schreiben, Löschen der Quelle erst nach vollständiger Übertragung,
  Kopier-Buchführung, Quarantäne. Verbindungsabbrüche brechen den Lauf als gestörte
  Anbindung ab. Bei SFTP-Quellen mindestens 2 s Stabilitätsfenster (Zeiten nur
  sekundengenau).
- **Gespeicherte Zugangsdaten** für SMB und SFTP-Passwörter (Benutzer, Passwort,
  Domäne); in Anbindungen auswählbar, ein neues Passwort gilt sofort für alle.
  Die Seite „Schlüssel“ heißt jetzt **„Zugangsdaten“** (Passwörter + SSH-Schlüssel);
  `/keys` leitet weiter.
- **Anmeldung abschaltbar** (*Einstellungen → Anmeldung*, Bestätigung mit Passwort):
  immer / nicht aus vertrauenswürdigen Netzen / nie (z. B. hinter Reverse Proxy mit
  eigener Anmeldung). Deutlicher Hinweis in der Kopfzeile; CSRF-Schutz bleibt;
  `netmover reset-password` schaltet die Anmeldung wieder ein.

### Geändert
- Mover intern auf eine Quell-Schnittstelle umgestellt (`LocalSource`, `SftpSource`);
  bestehendes Verhalten unverändert (alle bisherigen Tests gelten weiter).

## [0.4.0] – 2026-10-11

### Hinzugefügt
- **Automatisch pausieren und fortsetzen:** Ist Quelle oder Ziel nicht erreichbar,
  wartet die Regel („wartet seit …“ mit Grund) – ein Protokolleintrag statt eines
  Fehlers alle 30 s, „Fehler in Folge“ zählt nicht hoch. Sobald die Anbindung zurück
  ist, läuft die Regel von selbst weiter („Fortgesetzt nach … Wartezeit“).
  Migration 0003: `rules.waiting_since`, `rules.waiting_reason`.
- **Wartungsmodus** (Knopf auf der Statusseite): alle geplanten Läufe pausiert,
  „Jetzt ausführen“ geht weiter, kein Alarm für Regeln in `/healthz`; gelber Hinweis
  in der Kopfzeile.
- **E-Mail-Benachrichtigung** per SMTP (STARTTLS, SSL/TLS oder ohne für interne
  Relays; Zertifikate werden geprüft), Passwort verschlüsselt, mehrere Empfänger,
  Test-Knopf. Webhook und E-Mail lassen sich gleichzeitig nutzen.

### Geändert
- Ausfälle von Anbindungen werden **einmal pro Anbindung** gemeldet, mit der Liste
  der betroffenen Regeln, und mit Entwarnung, wenn sie zurück sind – auch wenn eine
  Anbindung von außen ausgehängt wurde (vorher keine Meldung).
- Einstellungen neu gegliedert: Benachrichtigungen · Webhook · E-Mail · Überwachung.
- `/healthz`: Eine wartende Regel ist ein Problem (mit Grund); im Wartungsmodus nicht.

## [0.3.0] – 2026-10-11

### Hinzugefügt
- **Anleitung** (`docs/ANLEITUNG.md`) mit Screenshots, zugleich in der Oberfläche
  unter *Hilfe*; „Hilfe zu dieser Seite“ in der Kopfzeile und „?“-Links an den
  Formularabschnitten führen direkt zum passenden Abschnitt.
- **Roadmap** (`ROADMAP.md`).
- **Uptime Kuma:** `/healthz` mit Version, Scheduler-Herzschlag und Einzelprüfungen;
  Einzel-Endpunkte `/healthz/mount/<name>` und `/healthz/rule/<nummer>` (200/503/404);
  Push an einen Kuma-Push-Monitor jede Minute (*Einstellungen → Überwachung*, URL
  maskiert, Test-Knopf, fertige Adressen für HTTP-Monitore).

### Geändert
- **Menü neu aufgebaut:** Seitenleiste mit Gruppen in der Reihenfolge der
  Einrichtung (Übersicht · Verbindungen · Automatisierung · System), Symbole,
  Zähler (z. B. „2 gestört“, „1/2 aktiv“), Statusampel in der Kopfzeile; auf dem
  Handy ausklappbares Menü.
- SFTP-Formular zeigt nur noch passende Felder und Hinweise (kein Passwortfeld bei
  Schlüssel-Anmeldung, keine SMB-Hinweise); kompaktere Schlüsselseite.
- Gesundheit: Eine aktive Regel mit drei fehlgeschlagenen Läufen in Folge gilt jetzt
  ebenfalls als Problem (nicht nur „zu lange kein Erfolg“).

### Behoben
- `aria-current` im Menü und der gesperrte Löschknopf auf der Schlüsselseite
  wurden von Jinja maskiert (`=&#34;`) und wirkten nicht; Test prüft alle Seiten.

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

[Unreleased]: https://github.com/florianwilde/netmover/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/florianwilde/netmover/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/florianwilde/netmover/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/florianwilde/netmover/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/florianwilde/netmover/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/florianwilde/netmover/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/florianwilde/netmover/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/florianwilde/netmover/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/florianwilde/netmover/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/florianwilde/netmover/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/florianwilde/netmover/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/florianwilde/netmover/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/florianwilde/netmover/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/florianwilde/netmover/releases/tag/v0.1.0
