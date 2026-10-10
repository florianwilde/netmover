# Roadmap

Was NetMover kann, woran gearbeitet wird und was geplant ist. Erledigte Punkte
stehen mit Version im [CHANGELOG](CHANGELOG.md); Details zu Entscheidungen im
[DEVLOG](DEVLOG.md).

Legende: ✅ erledigt · 🔨 in Arbeit · 📋 geplant · 💡 Idee (noch nicht entschieden)

## In Arbeit

Gerade nichts – als Nächstes kommt 0.4.0.

## Als Nächstes (0.4.0)

| Status | Thema | Warum |
|---|---|---|
| 📋 | **SFTP als Quelle**: Scan, Stabilitätsprüfung und Löschen der Quelle über SFTP, mit denselben Garantien wie bei SMB/NFS | Quellen, die nur per SSH erreichbar sind (Wunsch aus dem Praxisbetrieb) |
| 📋 | **Gespeicherte Zugangsdaten** für SMB und SFTP-Passwörter, zusammen mit den SSH-Schlüsseln auf einer Seite „Zugangsdaten“ | Mehrere Freigaben mit demselben NAS-Benutzer an einer Stelle pflegen |
| 📋 | **Automatisch pausieren und fortsetzen**: Ist Quelle oder Ziel nicht erreichbar, wechselt die Regel in „wartet auf Anbindung X“ – ein Protokolleintrag und eine Benachrichtigung statt eines Fehlers alle 30 s; fortsetzen von selbst, sobald die Anbindung zurück ist. Dazu manuelles Pausieren (Wartungsmodus) | Weniger Rauschen im Protokoll und in Benachrichtigungen, kein Handgriff nach NAS-Neustarts |
| 📋 | **E-Mail-Benachrichtigung** (SMTP mit STARTTLS/SSL, verschlüsseltes Passwort, mehrere Empfänger) zusätzlich zum Webhook | Benachrichtigung ohne eigenen Webhook-Dienst |

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
| 0.3.0 | Neues Menü (gruppierte Seitenleiste, Statusampel, Handy-Menü); Anleitung im Repo und als Hilfe-Seite; Roadmap; Uptime Kuma (Einzel-Endpunkte, Push-Monitor) |
| 0.2.1 | Migrationen in einer Transaktion und ohne Fremdschlüsselfehler auf befüllten Datenbanken |
| 0.2.0 | Schlüsselverwaltung für SFTP; `netmover-update` mit Sicherung; Installation als Git-Klon |
| 0.1.1 | Mounts im System-Namespace sichtbar; frische Dateiattribute für die Stabilitätsprüfung; minütlicher Neuversuch nach NAS-Ausfall |
| 0.1.0 | Erste Version: SMB/NFS/SFTP, Regeln mit sicherem Verschieben, Scheduler, Protokoll, Weboberfläche, systemd/Docker |

## Wünsche einbringen

Neue Punkte bitte als Issue in Gitea anlegen oder direkt hier eintragen. Bei
Umsetzung wandert ein Punkt nach „In Arbeit“ und mit dem Release nach „Erledigt“.
