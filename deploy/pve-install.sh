#!/usr/bin/env bash
# NetMover auf Proxmox VE ausrollen – auf dem Proxmox-Host als root ausführen.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/florianwilde/netmover/main/deploy/pve-install.sh)"
#   bash pve-install.sh                      # fragt alles ab
#   NM_TYPE=lxc NM_IP=192.168.1.50/24 NM_GW=192.168.1.1 bash pve-install.sh -y
#
# Legt eine VM (Vorgabe; Debian-13-Cloud-Image, cloud-init, QEMU-Gast-Agent) oder
# einen privilegierten LXC (Debian 13, mount=cifs;nfs) an und installiert darin
# NetMover mit deploy/install.sh. Jeder Lauf schreibt ein Log nach
# /var/log/netmover-pve/ (NM_LOG_DIR), dazu bei Erfolg oder Fehler die Logs aus dem Gast. Jede Frage lässt sich über ihre NM_*-Variable
# vorbelegen (Liste: --help); -y nimmt für alles Übrige die Vorgabe.
#
# Zugang zum Code: Vorgabe ist das öffentliche GitHub-Repository per HTTPS, ohne
# Schlüssel. Bei einer SSH-Quelle (z. B. einem privaten Fork) erzeugt das Skript einen
# Deploy Key, zeigt ihn an und wartet, bis er auf GitHub eingetragen ist.
set -euo pipefail

DEFAULT_SOURCE="https://github.com/florianwilde/netmover.git"
# Veröffentlichter Hostschlüssel von GitHub (docs.github.com, 'GitHub's SSH key
# fingerprints', SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU)
GITHUB_KNOWN_HOST="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
CLOUD_IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest"
CLOUD_IMAGE="debian-13-genericcloud-amd64.qcow2"
IMAGE_CACHE="${NM_IMAGE_CACHE:-/var/lib/vz/template/cache}"

# Farben nur, wenn ein Terminal da ist (im Log stehen sie dann auch: less -R)
UI_TTY="${NM_UI_TTY:-/dev/tty}"
if { : <"$UI_TTY"; } 2>/dev/null && [ -z "${NO_COLOR:-}" ]; then
    BL=$'\e[36m' GN=$'\e[1;92m' RD=$'\e[01;31m' BD=$'\e[1m' CL=$'\e[m'
else
    BL="" GN="" RD="" BD="" CL=""
fi
info() { printf '\n%s==>%s [%s] %s%s%s\n' "$BL" "$CL" "$(date +%H:%M:%S)" "$BD" "$*" "$CL"; }
ok() { printf '    %s✔%s %s\n' "$GN" "$CL" "$*"; }
die() { printf '\n%s✖ %s%s\n' "$RD" "$*" "$CL" >&2; exit 1; }

usage() {
    cat <<'EOF'
NetMover auf Proxmox VE ausrollen (auf dem Proxmox-Host als root).

  bash pve-install.sh [-y]
  bash pve-install.sh --weiter <Nummer>

  -y, --yes          Vorgaben ohne Rückfrage übernehmen (gesetzte NM_*-Variablen gelten immer)
  --weiter <Nummer>  Nichts anlegen: NetMover in eine schon vorhandene VM bzw. einen
                     Container installieren, z. B. nach einem Abbruch beim Warten

Variablen (alle optional, sonst wird gefragt):
  NM_TYPE       vm | lxc                         (Vorgabe: vm)
  NM_ID         VM-/CT-Nummer                    (nächste freie)
  NM_HOSTNAME   Hostname                         (netmover)
  NM_CORES      CPU-Kerne                        (2)
  NM_RAM        Arbeitsspeicher in MiB           (2048)
  NM_DISK       Plattengröße in GB               (16)
  NM_STORAGE    Speicher für die Platte          (erster passende aktive)
  NM_BRIDGE     Netzwerk-Bridge                  (vmbr0)
  NM_VLAN       VLAN-Tag, leer = keins
  NM_IP         dhcp oder Adresse/Präfix, z. B. 192.168.1.50/24   (dhcp)
  NM_GW         Gateway (nur bei fester Adresse)
  NM_LAN        ja = Oberfläche im LAN auf Port 8080, nein = nur lokal (ja)
  NM_SOURCE     Git-Quelle                       (https://github.com/florianwilde/netmover.git)
  NM_VERSION    Tag, z. B. v0.12.0, oder latest  (latest)
  NM_DEPLOY_KEY Nur SSH-Quelle: vorhandener privater Deploy Key statt eines neuen
  NM_LOG_DIR    Verzeichnis für die Logs         (/var/log/netmover-pve)
  NM_AGENT_TIMEOUT  Nur VM: Sekunden Warten auf den Gast-Agent (1800)
  NM_SPEED_TEST Schreibtest auf dem Speicher vor dem Anlegen, ja/nein (ja)
  NM_MIN_MBPS   Darunter warnt der Schreibtest, MB/s (20)
  NM_SSHKEYS    Nur VM: Datei mit öffentlichen SSH-Schlüsseln für den Benutzer 'debian'
                (Vorgabe: /root/.ssh/authorized_keys des Hosts, falls vorhanden)
EOF
}

ASSUME_YES=0
RESUME_ID=""
while [ $# -gt 0 ]; do
    case "$1" in
        -y | --yes) ASSUME_YES=1 ;;
        --weiter)
            [ $# -ge 2 ] || die "--weiter braucht die Nummer der VM bzw. des Containers."
            RESUME_ID=$2
            shift
            ;;
        -h | --help) usage; exit 0 ;;
        *) die "Unbekannte Option: $1 (Hilfe: --help)" ;;
    esac
    shift
done

# ask VAR "Frage" "Vorgabe" – eine gesetzte Variable gilt, sonst wird gefragt
ask() {
    local var=$1 prompt=$2 default=$3 value=""
    if [ -n "${!var:-}" ]; then
        return
    fi
    if [ "$ASSUME_YES" = 1 ]; then
        printf -v "$var" '%s' "$default"
        return
    fi
    read -r -p "    $prompt [$default]: " value </dev/tty || true
    printf -v "$var" '%s' "${value:-$default}"
}

# first_storage INHALT – erster aktiver Speicher, der diesen Inhalt aufnimmt
first_storage() {
    pvesm status -content "$1" 2>/dev/null | awk 'NR > 1 && $3 == "active" { print $1; exit }'
}

is_ssh_source() {
    case "$NM_SOURCE" in git@* | ssh://*) return 0 ;; *) return 1 ;; esac
}

# -- Prüfen ---------------------------------------------------------------------------

[ "$(id -u)" = 0 ] || die "Bitte als root auf dem Proxmox-Host ausführen."
command -v pveversion >/dev/null || die "Kein Proxmox VE gefunden (pveversion fehlt)."
for cmd in pvesh pvesm pct qm; do
    command -v "$cmd" >/dev/null || die "$cmd fehlt – ist das ein Proxmox-VE-Host?"
done

WORK="$(mktemp -d)"
GUEST_CREATED=0

# Alles, was auf dem Bildschirm erscheint, landet auch im Log (ohne Deploy Key:
# der wird nie ausgegeben). Je Lauf eine Datei, dazu die Logs aus dem Gast.
LOG_DIR="${NM_LOG_DIR:-/var/log/netmover-pve}"
STAMP="$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$LOG_DIR"
LOG="$LOG_DIR/$STAMP-install.log"
find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime +365 -delete 2>/dev/null || true
exec > >(tee -a "$LOG") 2> >(tee -a "$LOG" >&2)

SPEED_VOL=""
finish() {
    local rc=$?
    [ -z "$SPEED_VOL" ] || pvesm free "$SPEED_VOL" >/dev/null 2>&1 || true
    if [ "$rc" -ne 0 ]; then
        [ "$GUEST_CREATED" != 1 ] || collect_guest_logs
        printf '\n!! Abgebrochen (Code %s). Log: %s\n' "$rc" "$LOG" >&2
    fi
    rm -rf "$WORK"
}
trap finish EXIT

header() {
    printf '%s' "$GN"
    cat <<'EOF'
    _   __     __  __  ___
   / | / /__  / /_/  |/  /___ _   _____  _____
  /  |/ / _ \/ __/ /|_/ / __ \ | / / _ \/ ___/
 / /|  /  __/ /_/ /  / / /_/ / |/ /  __/ /
/_/ |_/\___/\__/_/  /_/\____/|___/\___/_/
EOF
    printf '%s    SMB/NFS/SFTP-Freigaben verwalten, Dateien zeitgesteuert verschieben\n\n' "$CL"
}

# -- Menüführung wie bei den Proxmox-VE-Helper-Scripts (whiptail) ---------------------
# Ohne Terminal, mit -y oder NM_UI=plain wird zeilenweise gefragt (ask).

UI=plain
if [ "$ASSUME_YES" = 0 ] && [ "${NM_UI:-auto}" != plain ] && command -v whiptail >/dev/null \
    && { : <"$UI_TTY"; } 2>/dev/null; then
    UI=whiptail
fi

# wt ARGS – whiptail-Dialog; die Oberfläche geht ans Terminal, die Antwort nach stdout
wt() {
    whiptail --backtitle "NetMover · Proxmox VE" "$@" 3>&1 1>"$UI_TTY" 2>&3
}

# wt_input VAR "Titel" "Frage" "Vorgabe" – nur wenn VAR noch nicht gesetzt ist
wt_input() {
    local var=$1 value
    [ -z "${!var:-}" ] || return 0
    value="$(wt --title "$2" --inputbox "$3" 10 64 "$4")" || die "Abgebrochen."
    printf -v "$var" '%s' "$value"
}

# wt_choose VAR "Titel" "Frage" "Vorgabe" OPTION BESCHREIBUNG … – Auswahlliste
wt_choose() {
    local var=$1 title=$2 text=$3 default=$4 items=() value
    shift 4
    [ -z "${!var:-}" ] || return 0
    while [ $# -gt 0 ]; do
        items+=("$1" "$2" "$([ "$1" = "$default" ] && echo ON || echo OFF)")
        shift 2
    done
    value="$(wt --title "$title" --radiolist "$text" 16 70 6 "${items[@]}")" || die "Abgebrochen."
    printf -v "$var" '%s' "$value"
}

# storage_items INHALT – "Name" "Typ, frei X GB" je aktivem Speicher
storage_items() {
    pvesm status -content "$1" 2>/dev/null | awk 'NR > 1 && $3 == "active" {
        printf "%s\n%s, frei %.0f GB\n", $1, $2, $6 / 1024 / 1024 }'
}

bridge_names() {
    ip -o link show type bridge 2>/dev/null | awk -F': ' '{ print $2 }' | cut -d@ -f1
}

ui_collect() {
    wt --title "NetMover" --yesno "Dieses Skript legt auf $(hostname) einen Gast an und installiert NetMover darin.\n\nEs schreibt ein Log nach $LOG_DIR.\n\nFortfahren?" 13 66 \
        || die "Abgebrochen."
    local mode
    mode="$(wt --title "Einstellungen" --menu "Vorgaben übernehmen oder selbst festlegen?" 14 72 2 \
        "Standard" "VM, nächste Nummer, 2 Kerne, 2 GB RAM, 16 GB, vmbr0, DHCP" \
        "Erweitert" "Typ, Nummer, Ressourcen, Speicher, Netz selbst festlegen")" || die "Abgebrochen."

    if [ "$mode" = Erweitert ]; then
        wt_choose NM_TYPE "Typ" "Was soll angelegt werden?" vm \
            vm "VM mit Debian 13 (empfohlen)" lxc "Privilegierter LXC mit Debian 13"
        wt_input NM_ID "Nummer" "VM-/CT-Nummer:" "$(pvesh get /cluster/nextid)"
        wt_input NM_HOSTNAME "Hostname" "Hostname des Gasts:" "netmover"
        wt_input NM_CORES "CPU" "CPU-Kerne:" "2"
        wt_input NM_RAM "Arbeitsspeicher" "Arbeitsspeicher in MiB:" "2048"
        wt_input NM_DISK "Platte" "Plattengröße in GB:" "16"
    fi
    NM_TYPE="${NM_TYPE:-vm}"

    # Speicher: Auswahl, sobald es mehr als einen gibt (auch bei Standard)
    local content=images items=()
    [ "$NM_TYPE" = vm ] || content=rootdir
    mapfile -t items < <(storage_items "$content")
    if [ -z "${NM_STORAGE:-}" ] && [ "${#items[@]}" -gt 2 ]; then
        wt_choose NM_STORAGE "Speicher" "Speicher für die Platte:" "${items[0]}" "${items[@]}"
    fi

    if [ "$mode" = Erweitert ]; then
        local bridges=() b
        mapfile -t bridges < <(bridge_names)
        if [ "${#bridges[@]}" -gt 1 ]; then
            items=()
            for b in "${bridges[@]}"; do items+=("$b" "Bridge"); done
            wt_choose NM_BRIDGE "Netzwerk" "Bridge:" "vmbr0" "${items[@]}"
        fi
        if [ -z "${NM_VLAN+x}" ]; then
            NM_VLAN="$(wt --title "Netzwerk" --inputbox "VLAN-Tag (leer = keins):" 10 64 "")" || die "Abgebrochen."
        fi
        wt_input NM_IP "Netzwerk" "IP-Adresse: dhcp oder fest mit Präfix, z. B. 192.168.1.50/24" "dhcp"
        # shellcheck disable=SC2153  # NM_IP setzt wt_input
        if [ "$NM_IP" != dhcp ]; then
            wt_input NM_GW "Netzwerk" "Gateway:" ""
        fi
        if [ -z "${NM_LAN:-}" ]; then
            if wt --title "Oberfläche" --yesno "Oberfläche im LAN erreichbar machen (http://<IP>:8080, ohne TLS)?\n\nNein = nur lokal, Zugriff per SSH-Tunnel." 11 66; then
                NM_LAN=ja
            else
                NM_LAN=nein
            fi
        fi
        wt_input NM_VERSION "Version" "NetMover-Version (Tag wie v0.13.0 oder latest):" "latest"
    fi
    ASSUME_YES=1 # alles Übrige: Vorgaben
}

ui_confirm() {
    local vlan="${NM_VLAN:-keins}" ip="$NM_IP"
    [ "$NM_IP" = dhcp ] || ip="$NM_IP, Gateway $NM_GW"
    wt --title "Zusammenfassung" --yesno "$(printf '%-14s %s\n' \
        Typ "$( [ "$NM_TYPE" = vm ] && echo 'VM (Debian 13)' || echo 'privilegierter LXC (Debian 13)')" \
        Nummer "$NM_ID" Hostname "$NM_HOSTNAME" \
        Ressourcen "$NM_CORES Kerne, $NM_RAM MiB, $NM_DISK GB auf $NM_STORAGE" \
        Netzwerk "$NM_BRIDGE, VLAN $vlan, $ip" \
        Oberfläche "$( [ "$NM_LAN" = ja ] && echo 'im LAN auf Port 8080' || echo 'nur lokal')" \
        Version "$NM_VERSION")\n\nJetzt anlegen und installieren?" 18 74 || die "Abgebrochen."
}

# --weiter: Typ und Hostname aus dem vorhandenen Gast, alles Übrige nach Vorgabe
resume_params() {
    [[ $RESUME_ID =~ ^[0-9]+$ ]] || die "Ungültige Nummer: $RESUME_ID"
    NM_ID=$RESUME_ID
    if qm status "$NM_ID" >/dev/null 2>&1; then
        NM_TYPE=vm
        NM_HOSTNAME="$(qm config "$NM_ID" | awk -F': ' '$1 == "name" { print $2 }')"
    elif pct status "$NM_ID" >/dev/null 2>&1; then
        NM_TYPE=lxc
        NM_HOSTNAME="$(pct config "$NM_ID" | awk -F': ' '$1 == "hostname" { print $2 }')"
    else
        die "Es gibt keine VM und keinen Container mit der Nummer $NM_ID."
    fi
    NM_HOSTNAME="${NM_HOSTNAME:-netmover}"
    if [ "$UI" != plain ]; then
        wt --title "Weitermachen" --yesno "NetMover in $(guest_kind) $NM_ID ($NM_HOSTNAME) installieren?\n\nDer Gast bleibt, wie er ist; es wird nur NetMover installiert." 11 70 \
            || die "Abgebrochen."
    fi
    ASSUME_YES=1
}

guest_kind() {
    [ "$NM_TYPE" = vm ] && echo VM || echo Container
}

[ "$UI" = plain ] || clear >"$UI_TTY" 2>/dev/null || true
header
info "NetMover auf $(hostname) ausrollen ($(pveversion | head -n 1))"
echo "    Log: $LOG"
if [ -n "$RESUME_ID" ]; then
    resume_params
elif [ "$UI" != plain ]; then
    ui_collect
fi

if [ -z "$RESUME_ID" ]; then
ask NM_TYPE "VM oder LXC (privilegiert)? vm/lxc" "vm"
[[ $NM_TYPE =~ ^(lxc|vm)$ ]] || die "NM_TYPE muss lxc oder vm sein, nicht '$NM_TYPE'."
content=images
[ "$NM_TYPE" = vm ] || content=rootdir

ask NM_ID "Nummer" "$(pvesh get /cluster/nextid)"
[[ $NM_ID =~ ^[0-9]+$ ]] && [ "$NM_ID" -ge 100 ] || die "Ungültige Nummer: $NM_ID"
pvesh get /cluster/nextid --vmid "$NM_ID" >/dev/null 2>&1 || die "Nummer $NM_ID ist schon vergeben."

ask NM_HOSTNAME "Hostname" "netmover"
[[ $NM_HOSTNAME =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "Ungültiger Hostname: $NM_HOSTNAME"
ask NM_CORES "CPU-Kerne" "2"
ask NM_RAM "Arbeitsspeicher in MiB" "2048"
ask NM_DISK "Plattengröße in GB" "16"
for v in NM_CORES NM_RAM NM_DISK; do
    [[ ${!v} =~ ^[1-9][0-9]*$ ]] || die "$v muss eine Zahl sein, nicht '${!v}'."
done
ask NM_STORAGE "Speicher für die Platte" "$(first_storage "$content")"
[ -n "$NM_STORAGE" ] || die "Kein aktiver Speicher für '$content' gefunden – NM_STORAGE setzen."
ask NM_BRIDGE "Netzwerk-Bridge" "vmbr0"
[[ $NM_BRIDGE =~ ^[A-Za-z0-9_.-]+$ ]] || die "Ungültige Bridge: $NM_BRIDGE"
ask NM_VLAN "VLAN-Tag (leer = keins)" ""
[ -z "$NM_VLAN" ] || { [[ $NM_VLAN =~ ^[0-9]+$ ]] && [ "$NM_VLAN" -ge 1 ] && [ "$NM_VLAN" -le 4094 ]; } \
    || die "Ungültiges VLAN: $NM_VLAN"
ask NM_IP "IP-Adresse (dhcp oder z. B. 192.168.1.50/24)" "dhcp"
if [ "$NM_IP" != dhcp ]; then
    [[ $NM_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || die "Ungültige Adresse: $NM_IP (Format 192.168.1.50/24)"
    ask NM_GW "Gateway" ""
    [[ $NM_GW =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "Bei fester Adresse ist ein Gateway nötig (NM_GW)."
fi
fi # Ende: nur beim Anlegen
ask NM_LAN "Oberfläche im LAN erreichbar (Port 8080, ohne TLS)? ja/nein" "ja"
# shellcheck disable=SC2153  # NM_LAN setzt ask()
[[ $NM_LAN =~ ^(ja|nein)$ ]] || die "NM_LAN muss ja oder nein sein."
ask NM_SOURCE "Git-Quelle" "$DEFAULT_SOURCE"
ask NM_VERSION "Version (Tag oder latest)" "latest"
[[ $NM_VERSION =~ ^(latest|v[0-9]+\.[0-9]+\.[0-9]+)$ ]] || die "Ungültige Version: $NM_VERSION"

[[ ${NM_AGENT_TIMEOUT:-1800} =~ ^[1-9][0-9]*$ ]] || die "NM_AGENT_TIMEOUT muss eine Zahl (Sekunden) sein."
[ -n "$RESUME_ID" ] || [ "$UI" = plain ] || ui_confirm

info "Einstellungen"
for v in NM_TYPE NM_ID NM_HOSTNAME NM_CORES NM_RAM NM_DISK NM_STORAGE NM_BRIDGE NM_VLAN \
    NM_IP NM_GW NM_LAN NM_SOURCE NM_VERSION; do
    printf '    %-12s %s\n' "$v" "${!v:-}"
done

# -- Zugang zum Code -----------------------------------------------------------------

KEY_B64=""
if is_ssh_source; then
    if [ -n "${NM_DEPLOY_KEY:-}" ]; then
        [ -r "$NM_DEPLOY_KEY" ] || die "Deploy Key $NM_DEPLOY_KEY nicht lesbar."
        cp "$NM_DEPLOY_KEY" "$WORK/deploy"
    else
        info "Deploy Key für $NM_HOSTNAME"
        ssh-keygen -q -t ed25519 -N "" -C "netmover@$NM_HOSTNAME" -f "$WORK/deploy"
        echo "    Auf GitHub eintragen: Repository → Settings → Deploy keys → Add deploy key"
        echo "    Titel: Kunde/Host, 'Allow write access' NICHT ankreuzen. Schlüssel:"
        echo
        cat "$WORK/deploy.pub"
        echo
        read -r -p "    Eingetragen? Weiter mit Enter … " _ </dev/tty || true
    fi
    KEY_B64="$(base64 -w0 <"$WORK/deploy")"
fi

# -- Skript, das im Gast läuft ---------------------------------------------------------

guest_script() {
    printf '#!/usr/bin/env bash\n'
    printf 'NM_SOURCE=%q\nNM_VERSION=%q\nNM_LAN=%q\nNM_KEY_B64=%q\nGITHUB_KNOWN_HOST=%q\n' \
        "$NM_SOURCE" "$NM_VERSION" "$NM_LAN" "$KEY_B64" "$GITHUB_KNOWN_HOST"
    cat <<'GUEST'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
log() { printf '    [gast] %s\n' "$*"; }

log "Warte auf Netzwerk"
for _ in $(seq 1 60); do
    getent hosts deb.debian.org >/dev/null && break
    sleep 2
done
getent hosts deb.debian.org >/dev/null || { log "Kein Netz oder DNS im Gast"; exit 10; }

log "Pakete installieren"
apt-get update -qq
apt-get install -y -qq git openssh-client ca-certificates curl >/dev/null

if [ -n "$NM_KEY_B64" ]; then
    install -d -m 0700 /root/.ssh
    printf '%s' "$NM_KEY_B64" | base64 -d >/root/.ssh/netmover_deploy
    chmod 0600 /root/.ssh/netmover_deploy
    touch /root/.ssh/known_hosts
    grep -qxF "$GITHUB_KNOWN_HOST" /root/.ssh/known_hosts || echo "$GITHUB_KNOWN_HOST" >>/root/.ssh/known_hosts
    if ! grep -q netmover_deploy /root/.ssh/config 2>/dev/null; then
        printf 'Host github.com\n    IdentityFile /root/.ssh/netmover_deploy\n    IdentitiesOnly yes\n' >>/root/.ssh/config
    fi
fi

log "Zugriff auf $NM_SOURCE prüfen"
reachable=0
for _ in $(seq 1 6); do
    if git ls-remote --tags "$NM_SOURCE" >/dev/null 2>&1; then
        reachable=1
        break
    fi
    sleep 5
done
if [ "$reachable" != 1 ]; then
    log "Kein Zugriff auf $NM_SOURCE – Deploy Key eingetragen? Netz nach außen offen?"
    exit 11
fi

if [ -d /opt/netmover/src/.git ]; then
    git -C /opt/netmover/src fetch -q --tags origin
else
    git clone -q "$NM_SOURCE" /opt/netmover/src
fi
cd /opt/netmover/src
tag="$NM_VERSION"
[ "$tag" != latest ] || tag="$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
git -c advice.detachedHead=false checkout -q "$tag"

log "NetMover $tag installieren (Log: /var/log/netmover-install.log)"
if ! ./deploy/install.sh >/var/log/netmover-install.log 2>&1; then
    tail -n 30 /var/log/netmover-install.log
    exit 12
fi

if [ "$NM_LAN" = ja ]; then
    sed -i 's/^NETMOVER_HOST=.*/NETMOVER_HOST=0.0.0.0/' /etc/netmover/netmover.env
    systemctl restart netmover
fi

for _ in $(seq 1 30); do
    curl -s -o /dev/null http://127.0.0.1:8080/healthz && break
    sleep 1
done
systemctl is-active --quiet netmover || { log "Dienst läuft nicht: journalctl -u netmover"; exit 13; }

code="$(journalctl -u netmover -n 500 --no-pager | grep setup.required | grep -o 'code=[A-Za-z0-9-]*' | tail -n 1 | cut -d= -f2 || true)"
echo "NETMOVER_RESULT version=$(/opt/netmover/venv/bin/netmover version) code=${code:-}"
GUEST
}

# Logs aus dem Gast neben das Host-Log legen – bei Erfolg und nach einem Fehler
collect_guest_logs() {
    local base="$LOG_DIR/$STAMP"
    if [ "$NM_TYPE" = lxc ]; then
        pct pull "$NM_ID" /var/log/netmover-install.log "$base-gast-install.log" 2>/dev/null || true
        pct exec "$NM_ID" -- journalctl -u netmover -n 500 --no-pager >"$base-gast-journal.log" 2>/dev/null || true
    else
        vm_output cat /var/log/netmover-install.log >"$base-gast-install.log" 2>/dev/null || true
        vm_output journalctl -u netmover -n 500 --no-pager >"$base-gast-journal.log" 2>/dev/null || true
        vm_output cloud-init status --long >"$base-gast-cloud-init.log" 2>/dev/null || true
    fi
    find "$LOG_DIR" -maxdepth 1 -name "$STAMP-gast-*" -empty -delete 2>/dev/null || true
    echo "    Logs aus dem Gast: $base-gast-*.log"
}

# Befehl in der VM über den Gast-Agent, Ausgabe auf stdout
vm_output() {
    qm guest exec "$NM_ID" --timeout 60 -- "$@" \
        | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin).get("out-data", ""))'
}

# Schreibt 64 MB in ein Testvolume auf dem gewählten Speicher und warnt, wenn das
# langsam geht – dann dauert der erste Start der VM (cloud-init) entsprechend lange.
storage_speed_check() {
    [ "${NM_SPEED_TEST:-ja}" = ja ] || return 0
    local type name path start end mbps=0 min="${NM_MIN_MBPS:-20}"
    type="$(pvesm status 2>/dev/null | awk -v s="$NM_STORAGE" '$1 == s { print $2 }')"
    name="vm-$NM_ID-nmspeed"
    case "$type" in dir | nfs | cifs | glusterfs | btrfs | cephfs) name="$name.raw" ;; esac
    info "Schreibtest auf $NM_STORAGE (${type:-?})"
    SPEED_VOL="$(pvesm alloc "$NM_STORAGE" "$NM_ID" "$name" 256M 2>/dev/null | sed -n "s/.*'\(.*\)'.*/\1/p")"
    path="$([ -z "$SPEED_VOL" ] || pvesm path "$SPEED_VOL" 2>/dev/null || true)"
    if [ "${path:0:1}" != / ]; then
        echo "    (Schreibtest auf diesem Speicher nicht möglich – übersprungen)"
    else
        head -c 64M /dev/urandom >"$WORK/speed.bin"
        start="$(date +%s%N)"
        if timeout 120 dd if="$WORK/speed.bin" of="$path" bs=4M oflag=direct conv=fsync,notrunc status=none 2>/dev/null \
            || timeout 120 dd if="$WORK/speed.bin" of="$path" bs=4M conv=fsync,notrunc status=none 2>/dev/null; then
            end="$(date +%s%N)"
            mbps=$((64 * 1000000000 / (end - start + 1)))
        fi
        rm -f "$WORK/speed.bin"
        if [ "$mbps" -ge "$min" ]; then
            ok "$mbps MB/s"
        else
            printf '    %s! Nur %s MB/s auf %s.%s Der erste Start (cloud-init) kann dann 20 Minuten\n' "$RD" "$mbps" "$NM_STORAGE" "$CL"
            echo "      und länger dauern. Besser einen schnelleren Speicher wählen (NM_STORAGE)."
            if [ "$UI" != plain ]; then
                wt --title "Langsamer Speicher" --yesno "Schreibtest auf $NM_STORAGE: nur $mbps MB/s (erwartet mindestens $min).\n\nDer erste Start kann dann 20 Minuten und länger dauern.\n\nTrotzdem fortfahren?" 13 70 \
                    || die "Abgebrochen – bitte mit einem schnelleren Speicher neu starten."
            elif [ "$ASSUME_YES" != 1 ]; then
                ask NM_SLOW_OK "Trotzdem fortfahren? ja/nein" "nein"
                [ "$NM_SLOW_OK" = ja ] || die "Abgebrochen – bitte mit einem schnelleren Speicher neu starten."
            fi
        fi
    fi
    if [ -n "$SPEED_VOL" ]; then
        pvesm free "$SPEED_VOL" >/dev/null 2>&1 || echo "    Testvolume $SPEED_VOL bitte von Hand löschen."
        SPEED_VOL=""
    fi
}

# Bei --weiter: gestoppten Gast starten
ensure_running() {
    local status
    if [ "$NM_TYPE" = vm ]; then
        status="$(qm status "$NM_ID" | awk '{ print $2 }')"
        [ "$status" = running ] || { qm start "$NM_ID"; ok "VM $NM_ID gestartet"; }
    else
        status="$(pct status "$NM_ID" | awk '{ print $2 }')"
        [ "$status" = running ] || { pct start "$NM_ID"; ok "Container $NM_ID gestartet"; }
    fi
}

wait_for_agent() {
    local limit="${NM_AGENT_TIMEOUT:-1800}" waited=0
    info "Warte auf den Gast-Agent (höchstens $((limit / 60)) min; beim ersten Start installiert ihn cloud-init)"
    until qm guest ping "$NM_ID" >/dev/null 2>&1; do
        if [ "$waited" -ge "$limit" ]; then
            die "Gast-Agent meldet sich nach $((waited / 60)) min nicht. Konsole der VM $NM_ID prüfen; ist sie fertig gestartet, weiter mit: bash pve-install.sh --weiter $NM_ID"
        fi
        sleep 5
        waited=$((waited + 5))
        if [ $((waited % 30)) -eq 0 ]; then
            printf '    … %d min %02d s, VM %s\n' $((waited / 60)) $((waited % 60)) \
                "$(qm status "$NM_ID" 2>/dev/null | awk '{ print $2 }')"
        fi
    done
    ok "Gast-Agent erreichbar (nach $((waited / 60)) min $((waited % 60)) s)"
}

net_ip() {
    if [ "$NM_IP" = dhcp ]; then
        echo "ip=dhcp"
    else
        echo "ip=$NM_IP,gw=$NM_GW"
    fi
}

# -- LXC ------------------------------------------------------------------------------

create_lxc() {
    local tstore tmpl net
    tstore="$(first_storage vztmpl)"
    [ -n "$tstore" ] || die "Kein Speicher für Container-Vorlagen (vztmpl) gefunden."
    info "Vorlage Debian 13 ($tstore)"
    pveam update >/dev/null 2>&1 || echo "    (pveam update fehlgeschlagen – nutze bekannte Liste)"
    tmpl="$(pveam available --section system | awk '$2 ~ /^debian-13-standard_.*_amd64/ { print $2 }' | sort -V | tail -n 1)"
    [ -n "$tmpl" ] || die "Keine Vorlage debian-13-standard gefunden (pveam available)."
    if pveam list "$tstore" | grep -q "$tmpl"; then
        ok "$tmpl vorhanden"
    else
        pveam download "$tstore" "$tmpl" >/dev/null
        ok "$tmpl geladen"
    fi

    net="name=eth0,bridge=$NM_BRIDGE,$(net_ip)"
    [ -z "$NM_VLAN" ] || net="$net,tag=$NM_VLAN"
    info "Container $NM_ID anlegen (privilegiert, mount=cifs;nfs)"
    pct create "$NM_ID" "$tstore:vztmpl/$tmpl" \
        --hostname "$NM_HOSTNAME" --cores "$NM_CORES" --memory "$NM_RAM" --swap 512 \
        --rootfs "$NM_STORAGE:$NM_DISK" --net0 "$net" \
        --unprivileged 0 --features "mount=cifs;nfs,nesting=1" \
        --ostype debian --timezone host --onboot 1 --start 1 >/dev/null
    GUEST_CREATED=1
    ok "Container läuft"
}

install_lxc() {
    info "NetMover im Container installieren"
    guest_script >"$WORK/setup.sh"
    pct push "$NM_ID" "$WORK/setup.sh" /root/netmover-setup.sh --perms 0700
    # shellcheck disable=SC2016  # $? und $rc wertet die Shell im Container aus
    pct exec "$NM_ID" -- bash -c 'bash /root/netmover-setup.sh; rc=$?; rm -f /root/netmover-setup.sh; exit $rc' \
        | tee "$WORK/out.txt" || die "Installation im Container fehlgeschlagen – Container $NM_ID bleibt zur Fehlersuche (pct enter $NM_ID; danach erneut: bash pve-install.sh --weiter $NM_ID)."
    GUEST_IP="$(pct exec "$NM_ID" -- hostname -I | awk '{ print $1 }')"
}

# -- VM -------------------------------------------------------------------------------

fetch_cloud_image() {
    local img="$IMAGE_CACHE/$CLOUD_IMAGE" want
    mkdir -p "$IMAGE_CACHE"
    curl -fsSL "$CLOUD_IMAGE_URL/SHA512SUMS" -o "$WORK/SHA512SUMS"
    want="$(awk -v f="$CLOUD_IMAGE" '$2 == f { print $1 }' "$WORK/SHA512SUMS")"
    [ -n "$want" ] || die "$CLOUD_IMAGE nicht in SHA512SUMS gefunden."
    if [ -f "$img" ] && [ "$(sha512sum <"$img" | cut -d' ' -f1)" = "$want" ]; then
        ok "Cloud-Image aktuell"
    else
        curl -fL --progress-bar "$CLOUD_IMAGE_URL/$CLOUD_IMAGE" -o "$img.part"
        [ "$(sha512sum <"$img.part" | cut -d' ' -f1)" = "$want" ] || { rm -f "$img.part"; die "Prüfsumme des Cloud-Images stimmt nicht."; }
        mv "$img.part" "$img"
        ok "Cloud-Image geladen und geprüft"
    fi
    IMAGE="$img"
}

snippet_storage() {
    local store current
    store="$(first_storage snippets)"
    if [ -z "$store" ]; then
        current="$(pvesh get /storage/local --output-format json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content", ""))')"
        echo "    Für cloud-init braucht die VM einen Speicher mit Inhalt 'snippets'; keiner ist eingeschaltet."
        ask NM_ENABLE_SNIPPETS "'snippets' auf 'local' zusätzlich einschalten (bisher: $current)? ja/nein" "ja"
        [ "$NM_ENABLE_SNIPPETS" = ja ] || die "Ohne Snippets-Speicher keine VM – dann NM_TYPE=lxc nehmen."
        pvesm set local --content "$current,snippets"
        ok "'snippets' auf 'local' eingeschaltet"
        store=local
    fi
    SNIPPETS="$store"
}

create_vm() {
    local vendor_name vendor_path tz net sshkeys
    info "Debian-13-Cloud-Image"
    fetch_cloud_image
    snippet_storage

    tz="$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Berlin)"
    vendor_name="netmover-$NM_ID-vendor.yaml"
    vendor_path="$(pvesm path "$SNIPPETS:snippets/$vendor_name")"
    mkdir -p "$(dirname "$vendor_path")"
    cat >"$vendor_path" <<EOF
#cloud-config
timezone: $tz
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
EOF

    net="virtio,bridge=$NM_BRIDGE"
    [ -z "$NM_VLAN" ] || net="$net,tag=$NM_VLAN"
    info "VM $NM_ID anlegen"
    qm create "$NM_ID" --name "$NM_HOSTNAME" --ostype l26 --machine q35 --cpu host \
        --cores "$NM_CORES" --memory "$NM_RAM" --scsihw virtio-scsi-single \
        --net0 "$net" --agent enabled=1 --serial0 socket --vga serial0 --onboot 1 >/dev/null
    GUEST_CREATED=1
    qm set "$NM_ID" --scsi0 "$NM_STORAGE:0,import-from=$IMAGE,discard=on,ssd=1" >/dev/null
    qm resize "$NM_ID" scsi0 "${NM_DISK}G" >/dev/null
    qm set "$NM_ID" --ide2 "$NM_STORAGE:cloudinit" --boot order=scsi0 \
        --ipconfig0 "$(net_ip)" --cicustom "vendor=$SNIPPETS:snippets/$vendor_name" >/dev/null
    sshkeys="${NM_SSHKEYS:-/root/.ssh/authorized_keys}"
    if [ -s "$sshkeys" ]; then
        qm set "$NM_ID" --sshkeys "$sshkeys" >/dev/null
        ok "SSH-Schlüssel aus $sshkeys für den Benutzer 'debian'"
    fi
    qm start "$NM_ID"
    ok "VM gestartet"
}

install_vm() {
    local exec_json
    wait_for_agent
    info "NetMover in der VM installieren"
    exec_json="$WORK/exec.json"
    guest_script | qm guest exec "$NM_ID" --pass-stdin 1 --timeout 1800 -- bash -s >"$exec_json" \
        || die "Ausführen in der VM fehlgeschlagen (qm guest exec)."
    python3 - "$exec_json" "$WORK/out.txt" <<'PY' || die "Installation in der VM fehlgeschlagen – VM bleibt zur Fehlersuche (danach erneut: bash pve-install.sh --weiter $NM_ID)."
import json, sys
data = json.load(open(sys.argv[1]))
out = data.get("out-data", "") + data.get("err-data", "")
open(sys.argv[2], "w").write(out)
sys.stdout.write(out)
sys.exit(int(data.get("exitcode", 1)))
PY
    GUEST_IP="$(qm guest cmd "$NM_ID" network-get-interfaces | python3 -c '
import json, sys
for nic in json.load(sys.stdin):
    if nic.get("name") == "lo":
        continue
    for a in nic.get("ip-addresses", []):
        if a.get("ip-address-type") == "ipv4":
            print(a["ip-address"]); sys.exit()
')"
}

# -- Los ------------------------------------------------------------------------------

GUEST_IP=""
if [ -n "$RESUME_ID" ]; then
    GUEST_CREATED=1
    info "Weiter mit $(guest_kind) $NM_ID ($NM_HOSTNAME)"
    ensure_running
else
    storage_speed_check
fi
if [ "$NM_TYPE" = lxc ]; then
    [ -n "$RESUME_ID" ] || create_lxc
    install_lxc
else
    [ -n "$RESUME_ID" ] || create_vm
    install_vm
fi

result="$(grep '^NETMOVER_RESULT' "$WORK/out.txt" | tail -n 1 || true)"
[ -n "$result" ] || die "Installation lieferte kein Ergebnis – Ausgabe oben prüfen."
version="$(sed -n 's/.*version=\([^ ]*\).*/\1/p' <<<"$result")"
code="$(sed -n 's/.*code=\([^ ]*\).*/\1/p' <<<"$result")"

info "Fertig: NetMover $version läuft in $([ "$NM_TYPE" = lxc ] && echo Container || echo VM) $NM_ID ($NM_HOSTNAME)"
if [ "$NM_LAN" = ja ]; then
    echo "    Oberfläche:       http://${GUEST_IP:-<IP>}:8080"
else
    echo "    Oberfläche:       ssh -L 8080:127.0.0.1:8080 <Gast>, dann http://localhost:8080"
fi
if [ -n "$code" ]; then
    echo "    Einrichtungscode: $code  (beim ersten Aufruf mit einem neuen Administratorpasswort)"
fi
echo "    Updates im Gast:  netmover-update"
collect_guest_logs
echo "    Log:              $LOG"
