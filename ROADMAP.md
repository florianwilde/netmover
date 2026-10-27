# Roadmap

Was NetMover kann, woran gearbeitet wird und was geplant ist. Erledigte Punkte
stehen mit Version im [CHANGELOG](CHANGELOG.md); Details zu Entscheidungen im
[DEVLOG](DEVLOG.md).

Legende: ✅ erledigt · 🔨 in Arbeit · 📋 geplant · 💡 Idee (noch nicht entschieden)

## Geplant

| Status | Thema | Warum |
|---|---|---|
| 📋 | **SSH-Ausfälle bei Paperless klären**, Anbindungen auf SSH-Schlüssel umstellen | 18 kurze Ausfälle seit 11.10. („No existing session“, „protocol banner“) |
| 📋 | **Statusseite schneller bei hängenden Mounts**: Füllstand zwischenspeichern | Heute bis zu 2 s Wartezeit, wenn ein NFS-Server weg ist |
| 📋 | **Benachrichtigung bei Quarantäne** einer Datei | Unbeaufsichtigter Betrieb; heute über Kuma (`metrics.quarantined_24h`) sichtbar |
| 📋 | **Kompatibilitätszusage für 1.0**: Einstellungen, Export-Format, Update aus jeder 0.x | Danach keine brechenden Änderungen ohne 2.0 |

### Weg zu 1.0

0.10.0 Sicherung + CI → 0.11.0 mehrere Ziele (Kundenanforderung; Datenformat muss vor
1.0 feststehen) → 0.12.0 restliche Punkte oben → ein bis zwei Wochen ruhiger Betrieb →
1.0.0 ohne neue Funktionen.

**Entschieden:** Kein TLS im internen Netz – HTTP auf Port 8080 ist eine bewusste
Entscheidung (README, „Sicherheit“). Der Reverse Proxy bleibt dokumentiert für
nicht vertrauenswürdige Netze.

## Ideen

| Status | Thema | Anmerkung |
|---|---|---|
| 💡 | **Eine Regel, mehrere Ziele** je Quell-Unterordner | Heute: eine Regel pro Unterordner |
| 💡 | Metriken für Prometheus (`/metrics`) | Ergänzt `/healthz`, z. B. Dateien pro Tag |
| 💡 | Deinstallationsskript | Gegenstück zu `install.sh` |
| 💡 | Prüfsumme (SHA-256) je Regel: Kopie zurücklesen und vergleichen | Zusätzlicher Nachweis, z. B. für Rechnungen (GoBD) |

## Erledigt

| Version | Thema |
|---|---|
| 0.11.0 | Mehrere Ziele je Regel (bis zu 10), unabhängig erreichbar; Archiv wird zum weiteren Ziel |
| 0.10.0 | Tägliche verschlüsselte Sicherung + `netmover restore`; Kennzahlen für Kuma; CI in Gitea; Migrationstest-Pflicht |
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

Neue Punkte bitte als Issue auf GitHub anlegen oder direkt hier eintragen. Bei
Umsetzung wandert ein Punkt nach „In Arbeit“ und mit dem Release nach „Erledigt“.
