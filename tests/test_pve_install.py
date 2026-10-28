"""deploy/pve-install.sh gegen nachgebaute Proxmox-Befehle (pct, qm, pvesm, pveam, pvesh).

Geprüft wird, was das Skript auf dem Host aufruft und welches Skript es in den Gast
gibt – nicht der Gast selbst (dafür gibt es install.sh und die Praxis).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/pve-install.sh"
BASH = shutil.which("bash") or "/bin/bash"
IMAGE = b"fake qcow2"

STUBS = {
    "id": "echo 0",
    "pveversion": 'echo "pve-manager/8.4.1/2a5fa54a8503f96d (running kernel: 6.8.12-9-pve)"',
    "timedatectl": "echo Europe/Berlin",
    "pvesh": r"""
case "$*" in
  "get /cluster/nextid") echo 105 ;;
  "get /cluster/nextid --vmid "*) [ "$4" != "${STUB_TAKEN:-}" ] || exit 1; echo "$4" ;;
  "get /storage/local --output-format json") echo '{"content": "iso,vztmpl,backup"}' ;;
esac""",
    "pvesm": r"""
case "$1 ${3:-}" in
  "status rootdir"|"status images") printf 'Name Type Status Total Used Available\n'
     echo "local-lvm lvmthin active 1 1 52428800"
     [ -z "${STUB_TWO_STORAGES:-}" ] || echo "ceph rbd active 1 1 104857600" ;;
  "status vztmpl") printf 'Name Type Status\nlocal dir active\n' ;;
  "status snippets") printf 'Name Type Status\n' ;;
esac
case "$1" in
  alloc) echo "successfully created '$2:$4'" ;;
  path) case "$2" in *nmspeed*) echo "$STUB_ROOT/speed.img" ;;
        *) echo "$STUB_ROOT/snippets/${2##*/}" ;; esac ;;
esac""",
    "pveam": r"""
case "$1" in
  available) echo "system  debian-12-standard_12.7-1_amd64.tar.zst"
             echo "system  debian-13-standard_13.1-2_amd64.tar.zst" ;;
esac""",
    "pct": r"""
case "$1" in
  push) cp "$3" "$STUB_ROOT/guest.sh" ;;
  pull) echo "install.sh-Ausgabe aus dem Container" >"$4" ;;
  exec) case "$*" in *hostname*) echo "10.0.0.5" ;;
        *) echo "NETMOVER_RESULT version=0.12.0 code=ab12-cd34" ;; esac ;;
  status) [ "$2" = "${STUB_EXISTING_CT:-}" ] || exit 2; echo "status: running" ;;
  config) echo "hostname: ct-kunde" ;;
esac""",
    "qm": r"""
case "$1 ${2:-}" in
  "guest exec") case "$*" in
       *--pass-stdin*) cat >"$STUB_ROOT/guest.sh"
          echo '{"exitcode": 0, "exited": 1,
                 "out-data": "NETMOVER_RESULT version=0.12.0 code=ef56-7890\n"}' ;;
       *) echo '{"exitcode": 0, "exited": 1, "out-data": "Ausgabe aus der VM\n"}' ;;
     esac ;;
  "guest ping") [ -z "${STUB_AGENT_DOWN:-}" ] ;;
  status*) [ "$2" = "${STUB_EXISTING_VM:-105}" ] || exit 2
           echo "status: ${STUB_VM_STATE:-running}" ;;
  config*) echo "name: srv-kunde" ;;
  "guest cmd") echo '[
     {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
     {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.6"}]}]' ;;
esac""",
    # Antwortet wie ein Mensch, der die Vorgaben nimmt; Abweichungen über STUB_WT_*.
    # Wie das echte whiptail: Oberfläche nach stdout, Antwort nach stderr.
    "whiptail": r"""
title=""; kind=""; args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --title) title="${args[i + 1]}" ;;
    --yesno|--menu|--radiolist|--inputbox) kind="${args[i]}" ;;
  esac
done
echo "[$kind $title]"
case "$kind" in
  --yesno) [ "$title" != "${STUB_WT_NO:-}" ] ;;
  --menu) echo "${STUB_WT_MODE:-Standard}" >&2 ;;
  --radiolist)
    case "$title" in
      Typ) echo "${STUB_WT_TYPE:-vm}" >&2 ;;
      Netzwerk) echo "${STUB_WT_BRIDGE:-vmbr0}" >&2 ;;
      Speicher) echo "${STUB_WT_STORAGE:-local-lvm}" >&2 ;;
    esac ;;
  --inputbox) echo "${args[${#args[@]} - 1]}" >&2 ;;
esac""",
    "ip": 'echo "4: vmbr0: <UP> mtu 1500"; echo "5: vmbr1: <UP> mtu 1500"',
    "clear": ":",
    "sleep": ":",  # Warten auf den Gast-Agent ohne echte Pausen
    "curl": r"""
out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac; shift; done
case "$url" in
  */SHA512SUMS) echo "$STUB_IMAGE_SHA  debian-13-genericcloud-amd64.qcow2" >"$out" ;;
  *) printf 'fake qcow2' >"$out" ;;
esac""",
}


@pytest.fixture
def stubs(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        exe = bin_dir / name
        exe.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >>"$STUB_ROOT/calls.log"\n{body}\n')
        exe.chmod(0o755)
    key = tmp_path / "deploy_key"
    key.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "S3CR3T-NUR-IM-TEST\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_ROOT": str(tmp_path),
        "STUB_IMAGE_SHA": hashlib.sha512(IMAGE).hexdigest(),
        "NM_IMAGE_CACHE": str(tmp_path / "cache"),
        "NM_DEPLOY_KEY": str(key),
        "NM_SSHKEYS": str(tmp_path / "keine-schluessel"),
        "NM_LOG_DIR": str(tmp_path / "logs"),
        "HOME": str(tmp_path),
    }


def _run(env: dict[str, str], *args: str, **nm: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(SCRIPT), *(args or ("-y",))],
        env={**env, **nm},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return (Path(env["STUB_ROOT"]) / "calls.log").read_text().splitlines()


def _logs(env: dict[str, str]) -> dict[str, str]:
    """Dateiendung hinter dem Zeitstempel → Inhalt, z. B. ``install`` oder ``gast-journal``."""
    return {
        f.name.split("-", 2)[2].removesuffix(".log"): f.read_text()
        for f in Path(env["NM_LOG_DIR"]).glob("*.log")
    }


def test_lxc_is_privileged_with_mounts_and_static_ip(stubs: dict[str, str]) -> None:
    r = _run(
        stubs,
        NM_TYPE="lxc",
        NM_IP="192.168.1.50/24",
        NM_GW="192.168.1.1",
        NM_VLAN="20",
        NM_SOURCE="git@github.com:florianwilde/netmover.git",  # SSH-Quelle → Deploy Key
    )
    assert r.returncode == 0, r.stderr + r.stdout
    [create] = [c for c in _calls(stubs) if c.startswith("pct create")]
    assert create.startswith("pct create 105 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst ")
    assert "--unprivileged 0 --features mount=cifs;nfs,nesting=1" in create
    assert "--net0 name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1,tag=20" in create
    assert "--rootfs local-lvm:16" in create
    assert "pveam download local debian-13-standard_13.1-2_amd64.tar.zst" in _calls(stubs)
    assert "http://10.0.0.5:8080" in r.stdout
    assert "Einrichtungscode: ab12-cd34" in r.stdout

    guest = Path(stubs["STUB_ROOT"]) / "guest.sh"
    text = guest.read_text()
    assert "NM_SOURCE=git@github.com:florianwilde/netmover.git" in text
    assert "NM_LAN=ja" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" in text
    assert "NM_KEY_B64=''" not in text  # Deploy Key ist dabei
    assert subprocess.run([BASH, "-n", str(guest)], check=False).returncode == 0


def test_vm_from_cloud_image_with_cloud_init(stubs: dict[str, str]) -> None:
    r = _run(
        stubs,
        NM_TYPE="vm",
        NM_VERSION="v0.12.0",
        NM_LAN="nein",
    )
    assert r.returncode == 0, r.stderr + r.stdout
    calls = _calls(stubs)
    image = Path(stubs["NM_IMAGE_CACHE"]) / "debian-13-genericcloud-amd64.qcow2"
    assert image.read_bytes() == IMAGE  # geladen und Prüfsumme stimmt
    assert "pvesm set local --content iso,vztmpl,backup,snippets" in calls
    assert f"qm set 105 --scsi0 local-lvm:0,import-from={image},discard=on,ssd=1" in calls
    assert "qm resize 105 scsi0 16G" in calls
    assert any("--cicustom vendor=local:snippets/netmover-105-vendor.yaml" in c for c in calls)
    assert not any("--sshkeys" in c for c in calls)  # keine Schlüsseldatei vorhanden
    vendor = Path(stubs["STUB_ROOT"]) / "snippets/netmover-105-vendor.yaml"
    assert "qemu-guest-agent" in vendor.read_text()
    assert "timezone: Europe/Berlin" in vendor.read_text()

    guest = (Path(stubs["STUB_ROOT"]) / "guest.sh").read_text()
    # Vorgabe: öffentliches Repository per HTTPS, kein Schlüssel
    assert "NM_SOURCE=https://github.com/florianwilde/netmover.git" in guest
    assert "NM_KEY_B64=''" in guest
    assert "NM_VERSION=v0.12.0" in guest
    assert "Einrichtungscode: ef56-7890" in r.stdout
    assert "ssh -L 8080" in r.stdout


def test_wrong_checksum_stops_before_creating_vm(stubs: dict[str, str]) -> None:
    r = _run({**stubs, "STUB_IMAGE_SHA": "0" * 128}, NM_TYPE="vm")
    assert r.returncode != 0
    assert "Prüfsumme" in r.stderr
    assert not any(c.startswith("qm create") for c in _calls(stubs))


@pytest.mark.parametrize(
    ("nm", "message"),
    [
        ({"NM_IP": "192.168.1.500"}, "Ungültige Adresse"),
        ({"NM_IP": "192.168.1.50/24"}, "Gateway"),
        ({"NM_ID": "105", "STUB_TAKEN": "105"}, "schon vergeben"),
        ({"NM_TYPE": "docker"}, "lxc oder vm"),
        ({"NM_HOSTNAME": "Net_Mover"}, "Hostname"),
        ({"NM_VERSION": "main"}, "Version"),
    ],
)
def test_invalid_input_creates_nothing(
    stubs: dict[str, str], nm: dict[str, str], message: str
) -> None:
    r = _run(stubs, **nm)
    assert r.returncode != 0
    assert message in r.stderr
    assert not any(c.startswith(("pct create", "qm create")) for c in _calls(stubs))


def test_vm_is_default_and_run_is_logged(stubs: dict[str, str]) -> None:
    r = _run(stubs, NM_SOURCE="git@github.com:florianwilde/netmover.git")
    assert r.returncode == 0, r.stderr + r.stdout
    assert any(c.startswith("qm create 105") for c in _calls(stubs))
    assert not any(c.startswith("pct create") for c in _calls(stubs))

    logs = _logs(stubs)
    assert set(logs) == {"install", "gast-install", "gast-journal", "gast-cloud-init"}
    log = logs["install"]
    assert "NM_TYPE      vm" in log
    assert "NETMOVER_RESULT version=0.12.0" in log  # Ausgabe aus dem Gast
    assert "Einrichtungscode: ef56-7890" in log
    assert "S3CR3T-NUR-IM-TEST" not in log  # der Deploy Key steht nie im Log
    assert "Ausgabe aus der VM" in logs["gast-install"]
    assert oct(Path(stubs["NM_LOG_DIR"]).stat().st_mode & 0o777) == "0o700"


def test_failure_keeps_log_and_names_it(stubs: dict[str, str]) -> None:
    r = _run({**stubs, "STUB_IMAGE_SHA": "0" * 128})
    assert r.returncode != 0
    log = _logs(stubs)["install"]
    assert "Prüfsumme" in log
    assert "Abgebrochen (Code 1). Log:" in r.stderr
    assert str(Path(stubs["NM_LOG_DIR"])) in r.stderr


def test_lxc_collects_container_logs(stubs: dict[str, str]) -> None:
    r = _run(stubs, NM_TYPE="lxc")
    assert r.returncode == 0, r.stderr + r.stdout
    logs = _logs(stubs)
    assert "install.sh-Ausgabe aus dem Container" in logs["gast-install"]
    assert any(c.startswith("pct exec 105 -- journalctl") for c in _calls(stubs))


@pytest.fixture
def tty(stubs: dict[str, str]) -> dict[str, str]:
    """Wie am Terminal: Dialoge statt Zeilenabfrage (Bildschirm = Datei)."""
    screen = Path(stubs["STUB_ROOT"]) / "screen"
    screen.touch()
    return {**stubs, "NM_UI_TTY": str(screen), "NO_COLOR": "1"}


def _dialogs(env: dict[str, str]) -> list[str]:
    return [c for c in _calls(env) if c.startswith("whiptail")]


def test_menu_standard_creates_vm_after_summary(tty: dict[str, str]) -> None:
    r = subprocess.run([BASH, str(SCRIPT)], env=tty, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    titles = [c.split("--title ", 1)[1].split(" --", 1)[0] for c in _dialogs(tty)]
    assert titles == ["NetMover", "Einstellungen", "Zusammenfassung"]
    assert any(c.startswith("qm create 105 --name netmover") for c in _calls(tty))
    screen = (Path(tty["STUB_ROOT"]) / "screen").read_text()
    assert "[--yesno Zusammenfassung]" in screen  # Dialoge gehen ans Terminal, nicht ins Log
    assert "[--yesno" not in _logs(tty)["install"]


def test_menu_advanced_lxc_with_second_bridge_and_storage(tty: dict[str, str]) -> None:
    env = {
        **tty,
        "STUB_WT_MODE": "Erweitert",
        "STUB_WT_TYPE": "lxc",
        "STUB_WT_BRIDGE": "vmbr1",
        "STUB_WT_STORAGE": "ceph",
        "STUB_TWO_STORAGES": "1",
    }
    r = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    [create] = [c for c in _calls(env) if c.startswith("pct create")]
    assert "--net0 name=eth0,bridge=vmbr1,ip=dhcp" in create
    assert "--rootfs ceph:16" in create
    dialogs = " ".join(_dialogs(env))
    for title in ("Typ", "Nummer", "Hostname", "Speicher", "Netzwerk", "Oberfläche", "Version"):
        assert f"--title {title}" in dialogs


def test_menu_cancel_creates_nothing(tty: dict[str, str]) -> None:
    env = {**tty, "STUB_WT_NO": "Zusammenfassung"}
    r = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "Abgebrochen." in r.stderr
    assert not any(c.startswith(("qm create", "pct create")) for c in _calls(env))


def test_speed_check_measures_and_frees_test_volume(stubs: dict[str, str]) -> None:
    r = _run(stubs)
    assert r.returncode == 0, r.stderr + r.stdout
    calls = _calls(stubs)
    assert "pvesm alloc local-lvm 105 vm-105-nmspeed 256M" in calls
    assert "pvesm free local-lvm:vm-105-nmspeed" in calls
    assert calls.index("pvesm free local-lvm:vm-105-nmspeed") < next(
        i for i, c in enumerate(calls) if c.startswith("qm create")
    )
    assert "MB/s" in _logs(stubs)["install"]


def test_slow_storage_warns_and_continues_with_y(stubs: dict[str, str]) -> None:
    r = _run(stubs, NM_MIN_MBPS="100000000")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "Nur " in r.stdout and "schnelleren Speicher" in r.stdout
    assert any(c.startswith("qm create") for c in _calls(stubs))


def test_slow_storage_cancel_in_menu_creates_nothing(tty: dict[str, str]) -> None:
    env = {**tty, "NM_MIN_MBPS": "100000000", "STUB_WT_NO": "Langsamer Speicher"}
    r = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    calls = _calls(env)
    assert not any(c.startswith("qm create") for c in calls)
    assert "pvesm free local-lvm:vm-105-nmspeed" in calls  # Testvolume wird trotzdem gelöscht


def test_agent_timeout_shows_progress_and_names_weiter(stubs: dict[str, str]) -> None:
    r = _run(stubs, STUB_AGENT_DOWN="1", NM_AGENT_TIMEOUT="60")
    assert r.returncode != 0
    assert "… 0 min 30 s, VM running" in r.stdout
    assert "bash pve-install.sh --weiter 105" in r.stderr
    assert "gast-cloud-init" in _logs(stubs)  # Logs aus der VM werden trotzdem geholt


def test_weiter_installs_into_existing_vm_and_starts_it(stubs: dict[str, str]) -> None:
    env = {**stubs, "NM_UI": "plain", "STUB_EXISTING_VM": "124", "STUB_VM_STATE": "stopped"}
    r = _run(env, "--weiter", "124")
    assert r.returncode == 0, r.stderr + r.stdout
    calls = _calls(env)
    assert not any(c.startswith(("qm create", "pct create", "pvesm alloc")) for c in calls)
    assert "qm start 124" in calls
    assert any(c.startswith("qm guest exec 124 --pass-stdin 1") for c in calls)
    assert "NetMover 0.12.0 läuft in VM 124 (srv-kunde)" in r.stdout


def test_weiter_installs_into_existing_container(stubs: dict[str, str]) -> None:
    env = {**stubs, "NM_UI": "plain", "STUB_EXISTING_VM": "", "STUB_EXISTING_CT": "130"}
    r = _run(env, "--weiter", "130")
    assert r.returncode == 0, r.stderr + r.stdout
    calls = _calls(env)
    assert not any(c.startswith("pct create") for c in calls)
    assert any(c.startswith("pct push 130") for c in calls)
    assert "Container 130 (ct-kunde)" in r.stdout


def test_weiter_unknown_guest(stubs: dict[str, str]) -> None:
    r = _run({**stubs, "NM_UI": "plain", "STUB_EXISTING_VM": ""}, "--weiter", "999")
    assert r.returncode != 0
    assert "keine VM und keinen Container mit der Nummer 999" in r.stderr
