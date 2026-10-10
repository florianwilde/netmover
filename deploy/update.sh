#!/bin/sh
# NetMover aktualisieren (als root). Wird von install.sh als
# /usr/local/sbin/netmover-update installiert.
#
#   netmover-update            # auf die neueste Version (neuester Tag vX.Y.Z)
#   netmover-update v0.2.0     # auf eine bestimmte Version (auch zurück)
#   netmover-update --list     # verfügbare Versionen anzeigen
#
# Ablauf: Tags holen → Datenbank und Schlüssel sichern → Version auschecken →
# install.sh (installiert, migriert beim Start, startet den Dienst neu) →
# Gesundheit prüfen. Die Mounts bleiben dabei eingehängt.
set -eu

SRC="${NETMOVER_SRC:-/opt/netmover/src}"
VENV=/opt/netmover/venv
ENV_FILE=/etc/netmover/netmover.env
BACKUP_ROOT=/var/backups/netmover
KEEP_BACKUPS=10

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte als root ausführen." >&2
    exit 1
fi
if [ ! -d "$SRC/.git" ]; then
    echo "Kein Git-Klon unter $SRC. Einmalig einrichten:" >&2
    echo "  git clone https://git.wilde-server.de/florian.wilde/netmover.git $SRC" >&2
    exit 1
fi

# Einstellungen des Dienstes (Port, Datenverzeichnis)
NETMOVER_PORT=8080
NETMOVER_DATA_DIR=/var/lib/netmover
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

cd "$SRC"
git fetch --tags --quiet origin

if [ "${1:-}" = "--list" ]; then
    git tag -l 'v*' --sort=-v:refname
    exit 0
fi

TARGET="${1:-$(git tag -l 'v*' --sort=-v:refname | head -n 1)}"
if ! git rev-parse -q --verify "refs/tags/$TARGET" >/dev/null; then
    echo "Version $TARGET gibt es nicht. Verfügbar:" >&2
    git tag -l 'v*' --sort=-v:refname | head -n 10 >&2
    exit 1
fi
CURRENT="v$("$VENV/bin/netmover" version 2>/dev/null || echo "?")"
echo "Installiert: $CURRENT"
echo "Ziel:        $TARGET"
if [ "$CURRENT" = "$TARGET" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "Bereits auf dem gewünschten Stand (FORCE=1 erzwingt eine Neuinstallation)."
    exit 0
fi

NEWEST="$(printf '%s\n%s\n' "$CURRENT" "$TARGET" | sort -V | tail -n 1)"
if [ "$NEWEST" = "$CURRENT" ] && [ "$CURRENT" != "$TARGET" ]; then
    echo
    echo "!! Zurückstufen auf eine ältere Version: Wurde die Datenbank inzwischen migriert,"
    echo "   startet die alte Version nicht. Dann vorher die Sicherung von *vor* dem Update"
    echo "   zurückspielen (siehe $BACKUP_ROOT, Anleitung im README)."
    printf "   Trotzdem fortfahren? [j/N] "
    read -r answer
    [ "$answer" = "j" ] || [ "$answer" = "J" ] || exit 1
fi

echo
echo "==> Änderungen laut Changelog"
git show "$TARGET:CHANGELOG.md" | awk -v cur="${CURRENT#v}" '
    /^## \[/ { if (index($0, "[" cur "]")) exit; show = ($0 !~ /Unreleased/) }
    show' | head -n 60

echo
echo "==> Sicherung"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_ROOT/$STAMP-$CURRENT"
install -d -m 0700 "$BACKUP_ROOT" "$BACKUP"
# Konsistente Kopie auch bei laufendem Dienst (SQLite-Online-Backup)
"$VENV/bin/python" - "$NETMOVER_DATA_DIR/netmover.db" "$BACKUP/netmover.db" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close(); dst.close()
EOF
for f in secret.key session.key; do
    [ -f "$NETMOVER_DATA_DIR/$f" ] && cp -p "$NETMOVER_DATA_DIR/$f" "$BACKUP/"
done
[ -f "$ENV_FILE" ] && cp -p "$ENV_FILE" "$BACKUP/"
chmod 0600 "$BACKUP"/*
echo "    $BACKUP"
# alte Sicherungen aufräumen
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -print | sort \
    | head -n "-$KEEP_BACKUPS" | xargs -r rm -rf --

echo
echo "==> Version $TARGET auschecken und installieren"
git -c advice.detachedHead=false checkout --quiet "$TARGET"
"$SRC/deploy/install.sh" "$SRC" >/tmp/netmover-update.log 2>&1 || {
    echo "install.sh ist fehlgeschlagen – Ausgabe: /tmp/netmover-update.log" >&2
    tail -n 20 /tmp/netmover-update.log >&2
    FAILED=1
}

echo
echo "==> Prüfung"
OK=0
if [ "${FAILED:-0}" != "1" ]; then
    i=0
    while [ $i -lt 30 ]; do
        if "$VENV/bin/python" - "$NETMOVER_PORT" <<'EOF'
import json, sys, urllib.error, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}/healthz"
try:
    body = urllib.request.urlopen(url, timeout=3).read()
except urllib.error.HTTPError as exc:  # 503 = läuft, meldet aber Probleme
    body = exc.read()
except OSError:
    sys.exit(1)
data = json.loads(body)
print(f"    Gesundheit: {data['status']}")
for p in data.get("problems", []):
    print(f"    – {p}")
EOF
        then
            OK=1
            break
        fi
        i=$((i + 1))
        sleep 1
    done
fi

if [ "$OK" = "1" ] && systemctl is-active --quiet netmover; then
    echo "    Dienst läuft: $("$VENV/bin/netmover" version)"
    echo
    echo "Fertig. Sicherung: $BACKUP"
    exit 0
fi

echo
echo "!! Der Dienst läuft nach dem Update nicht richtig." >&2
echo "   Log:    journalctl -u netmover -n 50" >&2
echo "   Zurück auf $CURRENT (Datenbank aus der Sicherung, weil sie migriert sein kann):" >&2
echo "     systemctl stop netmover" >&2
echo "     cp $BACKUP/netmover.db $NETMOVER_DATA_DIR/netmover.db" >&2
echo "     rm -f $NETMOVER_DATA_DIR/netmover.db-wal $NETMOVER_DATA_DIR/netmover.db-shm" >&2
echo "     netmover-update $CURRENT" >&2
exit 1
