#!/usr/bin/env bash
# NetMover auf Proxmox VE ausrollen – auf dem Proxmox-Host als root ausführen.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/florianwilde/netmover/main/deploy/pve-install.sh)"
#   bash pve-install.sh                      # fragt alles ab
#   NM_TYPE=lxc NM_IP=192.168.1.50/24 NM_GW=192.168.1.1 bash pve-install.sh -y
#
# Legt wahlweise einen privilegierten LXC (Debian 13, mount=cifs;nfs) oder eine VM
# (Debian-13-Cloud-Image, cloud-init, QEMU-Gast-Agent) an und installiert darin
# NetMover mit deploy/install.sh. Jede Frage lässt sich über ihre NM_*-Variable
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

info() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ✓ %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
NetMover auf Proxmox VE ausrollen (auf dem Proxmox-Host als root).

  bash pve-install.sh [-y]

  -y, --yes   Vorgaben ohne Rückfrage übernehmen (gesetzte NM_*-Variablen gelten immer)

Variablen (alle optional, sonst wird gefragt):
  NM_TYPE       lxc | vm                         (Vorgabe: lxc)
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
  NM_SSHKEYS    Nur VM: Datei mit öffentlichen SSH-Schlüsseln für den Benutzer 'debian'
                (Vorgabe: /root/.ssh/authorized_keys des Hosts, falls vorhanden)
EOF
}

ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        -y | --yes) ASSUME_YES=1 ;;
        -h | --help) usage; exit 0 ;;
        *) die "Unbekannte Option: $arg (Hilfe: --help)" ;;
    esac
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
trap 'rm -rf "$WORK"' EXIT

info "NetMover auf $(hostname) ausrollen ($(pveversion | head -n 1))"

ask NM_TYPE "LXC (privilegiert) oder VM? lxc/vm" "lxc"
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
ask NM_LAN "Oberfläche im LAN erreichbar (Port 8080, ohne TLS)? ja/nein" "ja"
# shellcheck disable=SC2153  # NM_LAN setzt ask()
[[ $NM_LAN =~ ^(ja|nein)$ ]] || die "NM_LAN muss ja oder nein sein."
ask NM_SOURCE "Git-Quelle" "$DEFAULT_SOURCE"
ask NM_VERSION "Version (Tag oder latest)" "latest"
[[ $NM_VERSION =~ ^(latest|v[0-9]+\.[0-9]+\.[0-9]+)$ ]] || die "Ungültige Version: $NM_VERSION"

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
    ok "Container läuft"

    info "NetMover im Container installieren"
    guest_script >"$WORK/setup.sh"
    pct push "$NM_ID" "$WORK/setup.sh" /root/netmover-setup.sh --perms 0700
    # shellcheck disable=SC2016  # $? und $rc wertet die Shell im Container aus
    pct exec "$NM_ID" -- bash -c 'bash /root/netmover-setup.sh; rc=$?; rm -f /root/netmover-setup.sh; exit $rc' \
        | tee "$WORK/out.txt" || die "Installation im Container fehlgeschlagen – Container $NM_ID bleibt zur Fehlersuche (pct enter $NM_ID)."
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
        store=local
    fi
    SNIPPETS="$store"
}

create_vm() {
    local vendor_name vendor_path tz net exec_json sshkeys
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

    info "Warte auf den Gast-Agent (cloud-init installiert ihn, 2–5 Minuten)"
    local waited=0
    until qm guest ping "$NM_ID" >/dev/null 2>&1; do
        [ "$waited" -lt 600 ] || die "Gast-Agent meldet sich nicht – Konsole der VM $NM_ID prüfen."
        sleep 5
        waited=$((waited + 5))
    done
    ok "Gast-Agent erreichbar"

    info "NetMover in der VM installieren"
    exec_json="$WORK/exec.json"
    guest_script | qm guest exec "$NM_ID" --pass-stdin 1 --timeout 1800 -- bash -s >"$exec_json" \
        || die "Ausführen in der VM fehlgeschlagen (qm guest exec)."
    python3 - "$exec_json" "$WORK/out.txt" <<'PY' || die "Installation in der VM fehlgeschlagen – VM bleibt zur Fehlersuche."
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
if [ "$NM_TYPE" = lxc ]; then
    create_lxc
else
    create_vm
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
