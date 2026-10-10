#!/bin/sh
# NetMover auf Debian 13 installieren bzw. aktualisieren (als root ausführen).
#
#   ./deploy/install.sh                 # aus diesem Verzeichnis installieren
#   ./deploy/install.sh 'git+https://git.wilde-server.de/florian.wilde/netmover.git@v0.1.0'
set -eu

SOURCE="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
PREFIX=/opt/netmover

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte als root ausführen." >&2
    exit 1
fi

echo "==> Pakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv cifs-utils nfs-common >/dev/null

echo "==> Virtuelle Umgebung in $PREFIX/venv"
install -d -m 0755 "$PREFIX"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q --upgrade "$SOURCE"

echo "==> Konfiguration"
install -d -m 0755 /etc/netmover
if [ ! -f /etc/netmover/netmover.env ]; then
    install -m 0640 "$(dirname "$0")/netmover.env" /etc/netmover/netmover.env
fi
install -d -m 0755 /mnt/netmover

echo "==> systemd"
install -m 0644 "$(dirname "$0")/netmover.service" /etc/systemd/system/netmover.service
systemctl daemon-reload
systemctl enable netmover >/dev/null 2>&1
systemctl restart netmover

sleep 2
systemctl --no-pager --lines=0 status netmover || true
echo
if journalctl -u netmover -n 200 --no-pager | grep -q "setup.required"; then
    echo "Ersteinrichtung: Einrichtungscode aus dem Log:"
    journalctl -u netmover -n 200 --no-pager | grep "setup.required" | tail -1
fi
echo "Fertig. Weboberfläche: http://127.0.0.1:8080 (README: SSH-Tunnel / Reverse Proxy)"
