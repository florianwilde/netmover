# Roadmap

Was NetMover kann, woran gearbeitet wird und was geplant ist. Erledigte Punkte
stehen mit Version im [CHANGELOG](CHANGELOG.md); Details zu Entscheidungen im
[DEVLOG](DEVLOG.md).

Legende: ✅ erledigt · 🔨 in Arbeit · 📋 geplant · 💡 Idee (noch nicht entschieden)

## Geplant

| Status | Thema | Warum |
|---|---|---|
| 📋 | **CI in Gitea** (Actions): Tests, ruff, mypy, shellcheck bei jedem Push | Fehler fallen vor dem Update auf, nicht auf dem Server |
| 📋 | **Migrationstest-Pflicht**: jede neue Migration mit Test auf befüllter Datenbank | Lehre aus dem gescheiterten Update auf 0.2.0 |
| 📋 | **Reverse Proxy mit TLS** auf der Ziel-VM (Caddy), danach Bindung an `127.0.0.1` | Anmeldung nicht unverschlüsselt im LAN |
| 📋 | **Statusseite schneller bei hängenden Mounts**: Füllstand zwischenspeichern | Heute bis zu 2 s Wartezeit, wenn ein NFS-Server weg ist |

## Ideen

| Status | Thema | Anmerkung |
|---|---|---|
| 💡 | **Eine Regel, mehrere Ziele** je Quell-Unterordner | Heute: eine Regel pro Unterordner |
| 💡 | Metriken für Prometheus (`/metrics`) | Ergänzt `/healthz`, z. B. Dateien pro Tag |
| 💡 | Deinstallationsskript | Gegenstück zu `install.sh` |
| 💡 | Benachrichtigung bei Quarantäne einer Datei | Heute nur im Protokoll sichtbar |

## Erledigt

| Version | Thema |
|---|---|
| 0.9.0 | Regeln kopieren (Kopie zunächst deaktiviert) |
| 0.8.0 | Anbindungen kopieren inklusive Zugangsdaten |
| 0.7.0 | Archiv je Regel: Kopie ins Ziel und ins Archiv, Quelle erst danach löschen |
| 0.6.0 | Anmeldung je Netz: ohne Passwort / mit Passwort / abgewiesen, Schutz vor Aussperren |
| 0.5.0 | SFTP als Quelle; gespeicherte Zugangsdaten für SMB/SFTP-Passwörter (Seite „Zugangsdaten“); Anmeldung abschaltbar (vertrauenswürdige Netze / ganz) |
| 0.4.0 | Automatisch pausieren/fortsetzen bei nicht erreichbaren Anbindungen; Wartungsmodus; E-Mail-Benachrichtigung; eine Meldung pro ausgefallener Anbindung |
| 0.3.0 | Neues Menü (gruppierte Seitenleiste, Statusampel, Handy-Menü); Anleitung im Repo und als Hilfe-Seite; Roadmap; Uptime Kuma (Einzel-Endpunkte, Push-Monitor) |
| 0.2.1 | Migrationen in einer Transaktion und ohne Fremdschlüsselfehler auf befüllten Datenbanken |
| 0.2.0 | Schlüsselverwaltung für SFTP; `netmover-update` mit Sicherung; Installation als Git-Klon |
| 0.1.1 | Mounts im System-Namespace sichtbar; frische Dateiattribute für die Stabilitätsprüfung; minütlicher Neuversuch nach NAS-Ausfall |
| 0.1.0 | Erste Version: SMB/NFS/SFTP, Regeln mit sicherem Verschieben, Scheduler, Protokoll, Weboberfläche, systemd/Docker |

## Wünsche einbringen

Neue Punkte bitte als Issue in Gitea anlegen oder direkt hier eintragen. Bei
Umsetzung wandert ein Punkt nach „In Arbeit“ und mit dem Release nach „Erledigt“.
