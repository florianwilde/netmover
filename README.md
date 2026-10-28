# NetMover

Selbst gehosteter Dienst mit Weboberfläche, der **SMB/CIFS- und NFS-Freigaben
verwaltet** und **Dateien zeitgesteuert verschiebt** – „alle N Sekunden Ordner 1
prüfen, passende Dateien nach Ordner 2 bringen“. Quelle und Ziel sind
unabhängig voneinander per SMB, NFS, SFTP oder (als Ziel) lokal angebunden.

Typischer Einsatz: Ein Synology-NAS nimmt Scans entgegen, NetMover schiebt sie alle
30 Sekunden in den Eingangsordner von Paperless-ngx.

![Statusseite](docs/img/status.png)

| Dokument | Inhalt |
|---|---|
| **[Anleitung](docs/ANLEITUNG.md)** | Bedienung: Anbindungen, Schlüssel, Regeln, Status, Protokoll, Uptime Kuma, Beispiele, Fragen – auch in der Oberfläche unter *Hilfe* |
| **[Roadmap](ROADMAP.md)** | Was in Arbeit und geplant ist |
| [Changelog](CHANGELOG.md) | Änderungen je Version |
| dieses README | Installation, Update, Betrieb, Fehlersuche, Sicherheit |

- [Wie NetMover Datenverlust verhindert](#wie-netmover-datenverlust-verhindert)
- [Installation](#installation) · [Erster Start](#erster-start)
- [Beispiel: Synology → Paperless](#beispiel-synology--paperless)
- [Docker](#alternative-docker) · [Reverse Proxy](#reverse-proxy-mit-tls)
- [Konfiguration](#konfiguration) · [Fehlersuche](#fehlersuche) · [Sicherheit](#sicherheit)
- [Entwicklung](#entwicklung)

## Wie NetMover Datenverlust verhindert

Quelle und Ziel liegen fast immer auf verschiedenen Dateisystemen. „Verschieben“
heißt deshalb immer **kopieren, prüfen, dann erst die Quelle löschen**:

| Gefahr | Was NetMover tut |
|---|---|
| Halb geschriebene Scans | Datei wird nur angefasst, wenn Größe **und** Änderungszeit über das Stabilitätsfenster (Standard 5 s) gleich bleiben. `.part`, `.tmp`, `.crdownload`, `.filepart`, `.!ut` und versteckte Dateien werden ignoriert. |
| Freigabe nicht eingehängt | Vor jedem Lauf prüft NetMover über `/proc/self/mountinfo`, ob Quelle und Ziel wirklich eingehängt sind – nie daran, ob Dateien da sind. Sonst: Lauf abbrechen, nichts anfassen. |
| Wachsende Datei im Zielordner | Geschrieben wird in `.netmover-<uuid>.part`, dann `fsync`, Größenprüfung, erneute Prüfung der Quelle und atomares Umbenennen ohne Überschreiben. Erst danach wird die Quelle gelöscht. |
| `scan001.pdf` existiert schon | `scan001-1.pdf`, `scan001-2.pdf`, … – jedes Mal neu gegen das Dateisystem geprüft. Überschreiben nur, wenn ausdrücklich eingestellt. |
| Kopiermodus kopiert endlos | Übertragene Dateiversionen (Pfad, Größe, mtime) werden gemerkt. |
| Hängender NFS-Mount, „Stale file handle“ | Jeder Lauf arbeitet in einem eigenen Worker mit Zeitlimit; gestörte Mounts werden per `umount -l` neu eingehängt (max. 3 Versuche). |
| Ziel voll | Vor jeder Kopie: freier Platz ≥ Dateigröße + 5 %. |
| Stiller Ausfall | Statusseite und `/healthz` werden rot, wenn eine Regel länger als das Dreifache ihres Intervalls keinen erfolgreichen Lauf hatte. |

## Installation

### Wo installieren?

| Variante | Empfehlung |
|---|---|
| **VM mit Debian 13** | ✅ Empfohlen. Einhängen funktioniert ohne Sonderkonfiguration. |
| Proxmox-Host direkt | Funktioniert, aber zusätzliche Software auf dem Hypervisor ist meist unerwünscht. |
| **Privilegierter** LXC | Möglich: `pct set <id> -features mount=cifs;nfs`. |
| Unprivilegierter LXC | ❌ Der Kernel erlaubt dort kein CIFS/NFS-Mount. |
| Docker | Möglich, siehe [Alternative: Docker](#alternative-docker). |

### Proxmox: ein Befehl auf dem Host

Nach dem Vorbild der Proxmox-VE-Helper-Scripts legt
[`deploy/pve-install.sh`](deploy/pve-install.sh) auf einem Proxmox-VE-Host (ab 8.0)
alles in einem Durchgang an. Es fragt am Anfang **VM oder LXC**:

- **VM (Vorgabe, empfohlen):** Debian-13-Cloud-Image (Prüfsumme wird geprüft),
  cloud-init installiert den QEMU-Gast-Agent. Dafür muss ein Speicher den Inhalt
  „snippets“ haben; fehlt er, bietet das Skript an, ihn auf `local` einzuschalten.
- **LXC:** privilegiert, Debian-13-Vorlage, `features: mount=cifs;nfs,nesting=1`.

Danach installiert es NetMover im Gast mit `install.sh` und gibt Adresse und
Einrichtungscode aus. Auf dem Proxmox-Host als root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/florianwilde/netmover/main/deploy/pve-install.sh)"
```

Am Terminal führt es wie die Helper-Scripts mit Dialogen (`whiptail`) durch:
Bestätigung, **Standard** (alle Vorgaben) oder **Erweitert** (Typ, Nummer, Hostname,
Ressourcen, Speicher, Bridge, VLAN, IP, Oberfläche, Version), Auswahlliste für den
Speicher, sobald es mehrere gibt, und eine Zusammenfassung vor dem Anlegen. Mit `-y`,
vorbelegten `NM_*`-Variablen oder `NM_UI=plain` fragt es stattdessen zeilenweise.

Wer das Skript vorher lesen will, lädt es mit `curl -fsSLO …/pve-install.sh` und
startet es mit `bash pve-install.sh`. Für eine SSH-Quelle (`NM_SOURCE=git@…`, z. B.
einen privaten Fork) erzeugt das Skript einen Deploy Key, zeigt ihn an und wartet,
bis er eingetragen ist; der Hostschlüssel von GitHub ist fest hinterlegt.

**Log:** Jeder Lauf schreibt nach `/var/log/netmover-pve/` (Modus 0700, `NM_LOG_DIR`)
eine Datei `<Zeitstempel>-install.log` mit allen Einstellungen, Schritten samt Uhrzeit
und der Ausgabe aus dem Gast – den Deploy Key nie. Dazu holt das Skript bei Erfolg
und nach einem Fehler die Logs aus dem Gast: `…-gast-install.log` (Ausgabe von
`install.sh`), `…-gast-journal.log` (Dienst) und bei der VM `…-gast-cloud-init.log`.
Bricht das Skript ab, nennt es den Pfad. Logs älter als ein Jahr werden gelöscht.

Jede Frage lässt sich über eine Variable vorbelegen, `-y` übernimmt den Rest
(Liste: `bash pve-install.sh --help`):

```bash
NM_TYPE=vm NM_IP=192.168.1.50/24 NM_GW=192.168.1.1 NM_VLAN=20 bash pve-install.sh -y
```

### systemd-Dienst (empfohlen)

```bash
apt install -y git
git clone https://github.com/florianwilde/netmover.git /opt/netmover/src
cd /opt/netmover/src && git checkout "$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
./deploy/install.sh
```

Das Repository ist öffentlich: Klonen und Updates brauchen keine Anmeldung und
keinen Schlüssel.

Das Skript installiert `python3-venv`, `cifs-utils`, `nfs-common` und `git`. Es
richtet NetMover in `/opt/netmover/venv` ein, installiert
[`deploy/netmover.service`](deploy/netmover.service) und das Update-Skript
`netmover-update` und startet den Dienst.

### Update

```bash
netmover-update            # auf die neueste Version
netmover-update --list     # verfügbare Versionen
netmover-update v0.2.0     # bestimmte Version
```

`netmover-update` geht so vor:
1. Neue Versions-Tags holen (von GitHub oder dem Gitea-Spiegel, je nach Klon) und
   die Änderungen laut Changelog anzeigen.
2. **Datenbank, `secret.key` und Einstellungen nach `/var/backups/netmover/` sichern**
   (die letzten 10 bleiben).
3. Die Version auschecken, installieren und den Dienst neu starten. Neue
   Datenbank-Migrationen laufen dabei automatisch.
4. `/healthz` prüfen.

**Installationen aus Gitea** (geklont aus `git.wilde-server.de`) einmalig auf GitHub
umstellen – ohne Schlüssel, weil das Repository öffentlich ist:

```bash
git -C /opt/netmover/src remote set-url origin https://github.com/florianwilde/netmover.git
netmover-update --list
```

Wer Gitea weiter als Quelle nutzen will, richtet den [Gitea-Spiegel](#gitea-spiegel) ein.

Die Mounts bleiben die ganze Zeit eingehängt. Geht etwas schief, nennt das Skript
die Befehle, um die Sicherung zurückzuspielen und zur vorherigen Version zu wechseln.
**Zurückstufen** auf eine ältere Version geht nur mit der Datenbank-Sicherung von
vor dem Update, wenn das Update eine Migration enthielt; das Skript fragt deshalb nach.

Was wo liegt:

| Pfad | Inhalt |
|---|---|
| `/var/lib/netmover/netmover.db` | Datenbank (SQLite) |
| `/var/lib/netmover/secret.key` | Schlüssel für die Zugangsdaten (0600) – **mitsichern!** |
| `/etc/netmover/netmover.env` | Einstellungen (Port, Logs, …) |
| `/mnt/netmover/<name>` | Mountpoints – nur NetMover legt hier etwas an |
| `/run/netmover` | kurzlebige Credentials-Dateien für `mount.cifs` |

**Warum als root?** Ein- und Aushängen braucht `CAP_SYS_ADMIN`. Außerdem soll
NetMover Dateien auf beliebigen Freigaben lesen, löschen und mit den richtigen
Eigentümern anlegen können.

**Warum so wenig systemd-Härtung?** Optionen wie `ProtectSystem=`, `PrivateTmp=`
oder `ProtectHome=` geben dem Dienst einen eigenen Mount-Namespace. Die Mounts
landeten dann *nur dort*: Das System und Paperless sähen sie nicht, und jeder
Neustart des Dienstes würde sie verwerfen. `MountFlags=shared` hilft dagegen nicht.
Auf der Test-VM nachgewiesen. Die Unit nutzt deshalb nur Härtungen ohne eigenen
Namespace (`NoNewPrivileges`, `RestrictSUIDSGID`, `LockPersonality`,
`RestrictRealtime`); Datenbank und Schlüssel sind nur für root lesbar. Prüfen:

```bash
findmnt -l | grep /mnt/netmover    # die eingehängten Freigaben müssen hier stehen
```

**Beim Beenden bleiben die Mounts eingehängt**, weil andere Programme sie benutzen
könnten. Beim Start werden alle Anbindungen mit „automatisch einhängen“
wiederhergestellt. Erst danach startet der Scheduler.

## Erster Start

NetMover lauscht standardmäßig nur auf `127.0.0.1:8080`. Vom Arbeitsplatz aus
erreichst du die Oberfläche per SSH-Tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 root@netmover-vm
# dann im Browser: http://localhost:8080
```

Beim ersten Aufruf musst du ein Administratorpasswort setzen (mindestens 10 Zeichen).
Dafür brauchst du den **Einrichtungscode** aus dem Log. Damit kann nicht irgendwer
im Netz, der die Seite zuerst öffnet, das Passwort setzen:

```bash
journalctl -u netmover | grep setup.required
# … event=setup.required code=4f2a-91c0-7d3e …
```

Passwort vergessen? `sudo /opt/netmover/venv/bin/netmover reset-password`, dann
`systemctl restart netmover` und die Einrichtung neu durchlaufen.

## Beispiel: Synology → Paperless

Ziel: Scans landen auf dem NAS in `scans`. NetMover schiebt PDFs alle 30 Sekunden
in den Consume-Ordner von Paperless, das in einem LXC per SSH erreichbar ist.

### 1. Synology vorbereiten

1. **Systemsteuerung → Benutzer** → Benutzer `netmover` anlegen, Lese-/Schreibrecht
   auf den gemeinsamen Ordner `scans`.
2. **Systemsteuerung → Dateidienste → SMB**: aktiviert. Unter *Erweitert* als
   minimales Protokoll SMB2 oder höher.

### 2. Anbindung „Synology Scans“ anlegen

*Anbindungen → Neue Anbindung*:

| Feld | Wert |
|---|---|
| Name | `Synology Scans` |
| Typ | SMB/CIFS |
| Server | `192.168.1.10` (IP des NAS) |
| Freigabe | `scans` |
| Benutzername / Passwort | `netmover` / … |
| Zusatzoptionen | `vers=3.0` |
| Beim Start automatisch einhängen | ✓ |

Klicke auf **Testen**. Alle drei Schritte (Einhängen, Lesen, Schreiben) sollten ✓
zeigen, und die Rolle sollte „Quelle mit Löschen“ lauten. Danach speichern und
**Einhängen**.

### 3. Paperless-LXC vorbereiten (SFTP-Ziel)

Lege in NetMover die *Anbindung → Neue Anbindung* an:

| Feld | Wert |
|---|---|
| Name | `Paperless` |
| Typ | SFTP |
| Server / SSH-Port | IP des LXC / `22` |
| Freigabe / Pfad | `/opt/paperless_data/consume` |
| Anmeldeart | SSH-Schlüssel |
| Schlüssel | einen gespeicherten Schlüssel wählen (siehe unten) oder „neuer Schlüssel nur für diese Anbindung“ |
| Benutzername | `paperless` (der Benutzer, unter dem Paperless läuft) |

**Schlüsselverwaltung:** Unter *Schlüssel* erzeugst du Ed25519-Schlüssel oder
importierst vorhandene (Ed25519, ECDSA, RSA ≥ 2048 Bit; OpenSSH- oder PEM-Format,
auch mit Passphrase). Ein Schlüssel kann von mehreren Anbindungen genutzt werden; den
Benutzernamen legt jede Anbindung selbst fest. Private Schlüssel liegen verschlüsselt
in der Datenbank und werden nie angezeigt. Die Seite zeigt den Fingerabdruck und eine
fertige `authorized_keys`-Zeile zum Kopieren.

Trage den öffentlichen Schlüssel im LXC so ein, dass er **nur SFTP** darf:

```bash
# im Paperless-LXC als root
install -d -m 700 -o paperless -g paperless ~paperless/.ssh
echo 'restrict,command="/usr/lib/openssh/sftp-server" ssh-ed25519 AAAA… netmover@paperless' \
  >> ~paperless/.ssh/authorized_keys
chown paperless:paperless ~paperless/.ssh/authorized_keys
chmod 600 ~paperless/.ssh/authorized_keys
```

Dann in NetMover bei der Anbindung **Testen**. Prüfe den angezeigten Fingerabdruck
des Hostschlüssels. Ermitteln kannst du ihn im LXC mit
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. NetMover hält ihn beim ersten
Test fest und verweigert später jede Verbindung, wenn er sich ändert.

> Weil die Dateien als Benutzer `paperless` ankommen, darf Paperless sie nach dem
> Einlesen auch löschen. Paperless-ngx (aktuelle Versionen mit `watchfiles`)
> bemerkt neue Dateien sofort. Ältere Versionen, die nur auf
> `IN_CLOSE_WRITE`/`IN_MOVED_TO` lauschen, siehe [Fehlersuche](#fehlersuche).

### 4. Regel anlegen

*Regeln → Neue Regel*:

| Feld | Wert |
|---|---|
| Name | `Scans → Paperless` |
| Intervall | `30` |
| Quelle | `Synology Scans`, Unterordner leer |
| Filter | Glob `*.pdf` |
| Ziel | Anbindung `Paperless`, Zielordner leer |
| Aktion | verschieben |
| Wenn Datei existiert | umbenennen |
| Stabilitätsfenster | `5` |
| Quarantäneordner | `_quarantaene` (optional) |

Klicke auf **Probelauf**. Die Tabelle zeigt, welche Datei wohin ginge, ohne etwas
zu verändern. Danach aktivieren und auf der Statusseite verfolgen.

**Getrennt nach Unterordnern?** Scannt z. B. jede Person in einen eigenen
Unterordner (`scans/max`, `scans/erika`), legst du pro Unterordner eine Regel mit
eigenem Quell-Unterordner und eigenem Ziel an.

**Paperless lokal statt per SSH?** Läuft Paperless auf demselben Host, wähle als Ziel
„Lokaler Pfad“ (z. B. `/opt/paperless_data/consume`). Der Ordner muss existieren und
darf nicht unter `/mnt/netmover` liegen. Mit „Eigentümer setzen“ (z. B. `1000:1000`)
gehören die Dateien danach dem Paperless-Benutzer.

### NFS statt SMB

Am NAS: *Dateidienste → NFS* aktivieren. Dann unter *Gemeinsamer Ordner →
Bearbeiten → NFS-Berechtigungen* eine Regel mit der IP des NetMover-Hosts anlegen.
In NetMover Typ „NFS 4“, Freigabe `/volume1/scans`.

**Root-Squash:** NetMover läuft als root. Synology bildet root standardmäßig auf
`nobody` ab, dann scheitert das Schreiben oder `chown`. Der **Testen**-Knopf erkennt
das und sagt es dir. Abhilfe in der NFS-Regel: *Squash → „Keine Zuordnung“* (No
mapping) oder „Alle Benutzer zu admin zuordnen“.

## Alternative: Docker

```bash
mkdir -p /mnt/netmover
docker compose up -d --build
docker compose logs | grep setup.required
```

[`compose.yml`](compose.yml) gibt dem Container die nötigen Rechte:
`cap_add: [SYS_ADMIN, DAC_READ_SEARCH]` und `security_opt: [apparmor:unconfined]`.

> **Wichtig:** `/mnt/netmover` muss als `bind` mit **`propagation: rshared`**
> eingebunden sein. Ohne das bleiben die Mounts im Container gefangen: NetMover
> selbst sieht sie, aber weder der Host noch andere Container (z. B. Paperless).
> Getestet: Mit `rshared` erscheint ein im Container eingehängtes Dateisystem
> sofort unter `/mnt/netmover` auf dem Host, ohne `rshared` nicht.

Lokale Ziele musst du zusätzlich als Volume einbinden. Für NFSv3 im Container die
Option `nolock` setzen oder besser NFSv4 nehmen; NFSv3-Locking braucht
`rpc.statd`, und den gibt es im Container nicht.

**Unterschied zum systemd-Dienst:** Der Dienst direkt auf dem Host braucht keine
Propagation-Tricks, keine AppArmor-Ausnahme und keine zusätzlichen Volumes. Docker
kapselt die Software, hat aber trotz Container `CAP_SYS_ADMIN` und ist damit kaum
weniger mächtig als root auf dem Host.

## Reverse Proxy mit TLS

NetMover gehört **nicht ins Internet**. Es hat root-Rechte, hängt beliebige
Freigaben ein und speichert Zugangsdaten zu deinem NAS. Ein Fehler oder ein
schwaches Passwort wäre ein direkter Weg in dein Netz. Für den Zugriff im LAN gehört
ein Reverse Proxy mit TLS davor; für unterwegs nimm VPN (z. B. WireGuard).

Beispiel Caddy auf demselben Host:

```caddyfile
netmover.lan {
    tls internal
    reverse_proxy 127.0.0.1:8080
}
```

Beispiel nginx:

```nginx
server {
    listen 443 ssl;
    server_name netmover.lan;
    ssl_certificate     /etc/ssl/netmover.crt;
    ssl_certificate_key /etc/ssl/netmover.key;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # Mount-Tests können dauern
    }
}
```

Dann in `/etc/netmover/netmover.env`:

```bash
NETMOVER_COOKIE_SECURE=true          # Session-Cookie nur noch per HTTPS
NETMOVER_FORWARDED_ALLOW_IPS=127.0.0.1
```

## Konfiguration

Umgebungsvariablen (in `/etc/netmover/netmover.env`); alles Übrige stellst du in
der Oberfläche unter *Einstellungen* ein:

| Variable | Standard | Bedeutung |
|---|---|---|
| `NETMOVER_HOST` | `127.0.0.1` | Adresse, auf der NetMover lauscht |
| `NETMOVER_PORT` | `8080` | Port |
| `NETMOVER_DATA_DIR` | `/var/lib/netmover` | Datenbank und Schlüssel |
| `NETMOVER_MOUNT_BASE` | `/mnt/netmover` | Basisverzeichnis der Mountpoints |
| `NETMOVER_RUN_DIR` | `/run/netmover` | kurzlebige Credentials-Dateien |
| `NETMOVER_COOKIE_SECURE` | `false` | Cookie nur per HTTPS (hinter TLS-Proxy: `true`) |
| `NETMOVER_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy-IPs, deren `X-Forwarded-*` gilt |
| `NETMOVER_SESSION_HOURS` | `12` | Gültigkeit der Anmeldung |
| `NETMOVER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `NETMOVER_LOG_FORMAT` | `logfmt` | `logfmt` oder `json` (ein Ereignis pro Zeile) |

In der Oberfläche unter *Einstellungen* stellst du ein:
- Aufbewahrung der Protokolle (Standard 30 Tage).
- Webhook-Benachrichtigung: „bei jedem Fehler“ oder „nach N Fehlern in Folge“,
  jeweils mit Entwarnung.
- Export und Import der Konfiguration, **ohne** Zugangsdaten und Webhook-URL.

Webhook-Vorlagen (Platzhalter `$event $title $message $rule $time $host`):

| Dienst | URL | Content-Type | Vorlage |
|---|---|---|---|
| ntfy | `https://ntfy.sh/<topic>` | `text/plain` | `$title: $message` |
| Gotify | `https://gotify.lan/message?token=…` | `application/json` | `{"title": "$title", "message": "$message", "priority": 5}` |
| eigener Empfänger | beliebig | `application/json` | Standardvorlage |

**Überwachung:** `GET /healthz` liefert ohne Anmeldung `200` oder `503` mit allen
Einzelprüfungen; `/healthz/mount/<name>` und `/healthz/rule/<nummer>` prüfen eine
einzelne Anbindung oder Regel. Alternativ meldet NetMover jede Minute an einen
Uptime-Kuma-Push-Monitor (*Einstellungen → Überwachung*). Details:
[Anleitung → Uptime Kuma](docs/ANLEITUNG.md#uptime-kuma).

## Kompatibilität

Ab Version 1.0 gilt: **Ein Update innerhalb von 1.x verlangt nie Handarbeit.**
Brechende Änderungen gibt es erst mit 2.0, angekündigt im Changelog.

| Was | Zusage | Abgesichert durch |
|---|---|---|
| Update | Von **jeder** 0.x- und 1.x-Version direkt auf die neueste 1.x, ohne Zwischenschritte; die Datenbank wird beim Start migriert, die Sicherung vorher macht `netmover-update`. | `tests/test_migrations.py`: jede Migration auf einer befüllten Datenbank hoch und wieder herunter |
| Einstellungen | Variablen in `netmover.env` behalten Name und Bedeutung; neue bekommen einen Standard, der das bisherige Verhalten beibehält. | Review |
| Export | Jeder Export ab 0.10.0 lässt sich importieren. Das Format (`"format": 1`) wird nur erweitert; ein neues Format bringt die Umwandlung des alten mit. | `tests/test_compat.py` mit echten Exporten älterer Versionen (`tests/data/`) |
| Überwachung | `/healthz` und die Einzel-Endpunkte behalten Statuscodes und Felder; neue Felder können dazukommen. | Review |
| Sicherung | Mit `netmover restore` lässt sich jede Sicherung ab 0.10.0 einspielen; die Datenbank wird danach migriert. | `tests/test_backup.py` (Sichern und Einspielen in derselben Version) |

Zurückstufen auf eine ältere Version ist nicht Teil der Zusage – dafür gibt es die
Sicherung von vor dem Update (siehe [Update](#update)).

## Fehlersuche

```bash
journalctl -u netmover -f                    # Log live
journalctl -u netmover | grep level=error
findmnt -R /mnt/netmover                     # was ist wirklich eingehängt?
```

| Meldung / Symptom | Ursache und Abhilfe |
|---|---|
| `mount error(13): Permission denied` (CIFS) | Benutzer oder Passwort falsch, oder keine Rechte auf die Freigabe. |
| `mount error(2): No such file or directory` | Freigabename falsch geschrieben (Groß/klein!). |
| `mount error(95): Operation not supported` | Keine gemeinsame SMB-Version (im Kernel-Log: „Dialect not supported by server“). Am NAS das maximale SMB-Protokoll auf SMB3 stellen oder Zusatzoption `vers=2.0`. |
| `mount error(112): Host is down` | NAS nicht erreichbar, oder SMB1 erwartet: `vers=3.0` bzw. `vers=2.1` probieren. |
| `… helper program` / `unknown filesystem type` | `apt install cifs-utils nfs-common` |
| `Operation not permitted` / `only root can do that` | Kein `CAP_SYS_ADMIN`: unprivilegierter LXC oder Docker ohne `cap_add`. |
| `access denied by server` (NFS) | NFS-Regel am NAS fehlt für die IP dieses Hosts (oder falsches Subnetz in der Regel). |
| Test: „Root-Squash“ | Siehe [NFS statt SMB](#nfs-statt-smb); `chown` wird bis dahin übersprungen. |
| „… ist nicht leer“ beim Einhängen | Im Mountpoint liegen Dateien, die geschrieben wurden, als nichts eingehängt war. Inhalt prüfen und sichern, Verzeichnis leeren, erneut einhängen. |
| „nicht eingehängt – Lauf abgebrochen“ | Die Freigabe ist weg (z. B. von Hand ausgehängt). Auf der Anbindungsseite **Einhängen**. NetMover hängt verschwundene Mounts absichtlich nicht selbst neu ein. |
| „gestört (ESTALE …)“, danach wieder OK | NAS-Neustart; NetMover hat neu eingehängt. Bleibt der Fehler: Netzwerk/NAS prüfen. |
| „weiterhin nicht verfügbar – neuer Versuch in 60 s“ | Das NAS ist (noch) weg, z. B. während eines DSM-Updates. Bei Anbindungen mit „automatisch einhängen“ versucht NetMover es jede Minute erneut und meldet per Webhook, wenn es wieder klappt. |
| „Keine Reaktion seit 120 s“ | Ein Dateizugriff hängt (meist NFS mit `hard`). Die Regel bleibt blockiert („läuft seit …“), bis der Zugriff zurückkehrt; die Anbindung wird vor dem nächsten Lauf geprüft. |
| „Hostschlüssel … hat sich geändert“ (SFTP) | Wurde der Zielserver neu aufgesetzt? Dann bei der Anbindung „Hostschlüssel zurücksetzen“ und neu testen. Sonst **nicht** verbinden. |
| SFTP: „Anmeldung abgelehnt“ | Öffentlicher Schlüssel fehlt in `authorized_keys`, oder Rechte von `~/.ssh` stimmen nicht (700/600, Eigentümer). |
| Paperless bemerkt neue Dateien nicht | Ältere Paperless-Versionen: `PAPERLESS_CONSUMER_POLLING=10` setzen. |
| Neue Dateien erscheinen erst nach bis zu 30 s | Der CIFS-Client speichert Verzeichnislisten zwischen. Gelangen Dateien nicht per SMB aufs NAS (z. B. per FTP vom Scanner), erfährt er nichts davon. Abhilfe: Zusatzoption `nohandlecache`. |
| Regel gelb „länger als 3 × Intervall“ | Seit dem Dreifachen des Intervalls kein erfolgreicher Lauf; Protokoll der Regel ansehen. |
| Einrichtungscode verloren | `systemctl restart netmover` erzeugt einen neuen. |

## Sicherheit

- **Nicht ins Internet stellen** (siehe [Reverse Proxy](#reverse-proxy-mit-tls)).
- **HTTP im internen Netz ist eine bewusste Entscheidung**, wenn das Netz
  vertrauenswürdig ist (Heimnetz, eigenes VLAN): Die Oberfläche läuft dann
  unverschlüsselt auf Port 8080, die Anmeldung kann per Netz abgeschaltet werden
  (*Einstellungen → Anmeldung*). Wer im selben Netz mitlesen kann, könnte Passwörter
  sehen, die man in Formulare eingibt. Für fremde oder gemischte Netze den
  [Reverse Proxy mit TLS](#reverse-proxy-mit-tls) davorsetzen.
- Zugangsdaten liegen nur verschlüsselt in der Datenbank (Fernet, Schlüssel
  `secret.key`, 0600). Die Oberfläche zeigt sie nie an; in der Prozessliste stehen
  sie ebenfalls nie, weil `mount.cifs` eine kurzlebige Credentials-Datei bekommt.
  Ausgaben von `mount` werden geschwärzt, bevor sie protokolliert werden.
- **Sichere `secret.key` zusammen mit der Datenbank.** Ohne den Schlüssel sind die
  gespeicherten Passwörter verloren. Das erledigt die tägliche, mit Passphrase
  verschlüsselte [Sicherung](docs/ANLEITUNG.md#sicherung) (*System → Sicherung*);
  Wiederherstellen mit `netmover restore <datei>`.
- Anmeldung:
  - Ein Administratorkonto mit Argon2-Passworthash.
  - Sperre nach 5 Fehlversuchen.
  - Session-Cookie `HttpOnly`, `SameSite=Lax`.
  - CSRF-Token bei jedem POST.
  - Strikte Content-Security-Policy ohne Inline-Skripte.
- Eingaben, die in `mount` landen, werden streng geprüft. Zusatzoptionen
  sind nur aus einer Allowlist erlaubt, `credentials`, `username`, `password` und
  `pass` sind gesperrt, `mount` wird nie über eine Shell aufgerufen.
- Mountpoints entstehen ausschließlich unter `/mnt/netmover`; NetMover ändert
  `/etc/fstab` nie.
- SFTP-Ziele: Schlüssel per `restrict,command="/usr/lib/openssh/sftp-server"` auf
  SFTP beschränken; der Hostschlüssel wird festgehalten.

## Entwicklung

```bash
python3 -m venv .venv            # oder: --without-pip + get-pip.py
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy

# Oberfläche ohne root und ohne NAS ausprobieren (simulierte Mounts):
NETMOVER_DATA_DIR=/tmp/nm NETMOVER_RUN_DIR=/tmp/nm/run NETMOVER_MOUNT_BASE=/tmp/nm/mnt \
NETMOVER_MOUNT_BACKEND=fake .venv/bin/netmover serve
# Freigaben simulieren: mkdir -p /tmp/nm/fake/remote/<server>/<freigabe>
```

Aufbau:

| Modul | Aufgabe |
|---|---|
| `mounting/` | `MountBackend` (Linux/Fake), Mount-Verwaltung, Test, Wiederherstellung, SFTP |
| `mover/` | Scan, Stabilität, atomares Schreiben, Kollisionen, Buchführung, Regellauf |
| `runs.py`, `scheduler.py`, `health.py`, `notify.py` | Protokoll, Zeitsteuerung, Gesundheit, Webhook |
| `admin.py`, `keys.py`, `transfer.py`, `web/` | Formulare, Schlüsselverwaltung, Export/Import, Oberfläche |
| `health.py`, `kuma.py` | Gesundheitsprüfung, Kennzahlen, Uptime-Kuma-Push |
| `backup.py` | tägliche verschlüsselte Sicherung, `netmover backup`/`restore` |
| `docs/` | Anleitung und Bilder; wird als Hilfe-Seite mit ausgeliefert |

Die gesamte Mover-Logik ist mit dem `FakeMountBackend` ohne root testbar. SFTP
wird gegen einen echten SFTP-Server im Testprozess getestet.

Neue Migration nach einer Modelländerung:

```bash
NETMOVER_DATA_DIR=$PWD/data .venv/bin/alembic revision --autogenerate -m "…"
```

Veröffentlichte Migrationen (ab v0.1.0) werden nie mehr geändert, nur ergänzt.
`tests/test_migrations.py` stuft **jede** Migration automatisch auf einer
Datenbank mit verknüpften Daten hoch und wieder herunter – eine neue Migration ist
damit ohne weiteres Zutun abgedeckt; ergänzt sie Spalten mit Daten, gehört ein
eigener Test dazu.

### CI

`.github/workflows/ci.yml` führt bei jedem Push auf `main`, jedem Versions-Tag und
jedem Pull Request ruff, mypy, shellcheck und alle Tests aus (Python 3.12 und 3.13,
GitHub Actions).

### Gitea-Spiegel

GitHub ist die Quelle, `git.wilde-server.de/florian.wilde/netmover` ein reiner
Lese-Spiegel. Gitea holt Zweige und Tags selbst ab; gepusht wird dorthin nie.
Nötig ist er nicht mehr – Server können direkt von GitHub holen (siehe [Update](#update)).
Einmalig einrichten:

1. In Gitea das bisherige Repository `florian.wilde/netmover` umbenennen, z. B. in
   `netmover-alt`. Ein bestehendes Repository lässt sich nicht in einen Spiegel
   umwandeln; erst nach dem Umbenennen ist der Name für den Spiegel frei, und
   bestehende Klone auf den Servern holen dann ohne Änderung vom Spiegel.
2. *Neue Migration → GitHub*: URL `https://github.com/florianwilde/netmover` (ohne
   Token, das Repository ist öffentlich), Haken bei **„Dieses Repository wird ein Mirror sein“**,
   Besitzer `florian.wilde`, Name `netmover`. Bei der Sichtbarkeit dieselbe wie beim
   bisherigen Repository wählen, damit die Server weiter ohne Anmeldung klonen können.
3. Im neuen Repository unter *Einstellungen → Mirror-Einstellungen* das Intervall auf
   `10m` setzen (Standard 8 h) und unter *Einstellungen → Erweitert* **Actions
   abschalten**. Die CI läuft auf GitHub; Gitea Actions liest sonst unter Umständen
   ebenfalls `.github/workflows` und würde die Prüfungen doppelt starten.
4. Prüfen: Auf einem Server `netmover-update --list` – die neueste Version muss
   erscheinen. Danach kann `netmover-alt` gelöscht werden.

Issues und Pull Requests gehören auf GitHub; im Spiegel angelegte gehen nicht
verloren, landen aber nie im Code.

### Arbeitsweise

- Commits nach [Conventional Commits](https://www.conventionalcommits.org/de/)
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:` …).
- Jede nutzerrelevante Änderung kommt unter `[Unreleased]` in
  [CHANGELOG.md](CHANGELOG.md). Bei einem Release:
  1. CI grün? Dann den Abschnitt `[Unreleased]` mit Versionsnummer versehen.
  2. Die Version in `src/netmover/__init__.py` anheben.
  3. Nach dem Merge auf `main`: *Actions → CI → Run workflow* mit **Version**
     (`X.Y.Z`) und **Kurztitel**. Die CI prüft, dass Version und Changelog-Abschnitt
     stimmen und es den Tag noch nicht gibt, setzt nach grünem Lauf den annotierten
     Tag `vX.Y.Z` („NetMover X.Y.Z – Kurztitel“) und legt das Release an.
  4. Alternativ von Hand: Tag setzen und pushen – das Release legt dann die CI an –
     oder `scripts/github_release.py --tag X.Y.Z "Kurztitel"` (Token in
     `~/.config/netmover/github-token`, Modus 0600). *Run workflow* ohne Version
     bzw. `--all` legt fehlende Releases für alle vorhandenen Tags an.
- Arbeitszeit und Entscheidungen stehen in [DEVLOG.md](DEVLOG.md).
- htmx 2.0.4 liegt unter `src/netmover/web/static/htmx.min.js`
  (SHA-256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`).

## Lizenz

MIT
