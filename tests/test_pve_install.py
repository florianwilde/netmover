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
  "status rootdir"|"status images") printf 'Name Type Status\nlocal-lvm lvmthin active\n' ;;
  "status vztmpl") printf 'Name Type Status\nlocal dir active\n' ;;
  "status snippets") printf 'Name Type Status\n' ;;
esac
case "$1" in path) echo "$STUB_ROOT/snippets/${2##*/}" ;; esac""",
    "pveam": r"""
case "$1" in
  available) echo "system  debian-12-standard_12.7-1_amd64.tar.zst"
             echo "system  debian-13-standard_13.1-2_amd64.tar.zst" ;;
esac""",
    "pct": r"""
case "$1" in
  push) cp "$3" "$STUB_ROOT/guest.sh" ;;
  exec) case "$*" in *hostname*) echo "10.0.0.5" ;;
        *) echo "NETMOVER_RESULT version=0.12.0 code=ab12-cd34" ;; esac ;;
esac""",
    "qm": r"""
case "$1 ${2:-}" in
  "guest exec") cat >"$STUB_ROOT/guest.sh"
     echo '{"exitcode": 0, "exited": 1,
            "out-data": "NETMOVER_RESULT version=0.12.0 code=ef56-7890\n"}' ;;
  "guest cmd") echo '[
     {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
     {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.6"}]}]' ;;
esac""",
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
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----\n")
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_ROOT": str(tmp_path),
        "STUB_IMAGE_SHA": hashlib.sha512(IMAGE).hexdigest(),
        "NM_IMAGE_CACHE": str(tmp_path / "cache"),
        "NM_DEPLOY_KEY": str(key),
        "NM_SSHKEYS": str(tmp_path / "keine-schluessel"),
        "HOME": str(tmp_path),
    }


def _run(env: dict[str, str], **nm: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(SCRIPT), "-y"],
        env={**env, **nm},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return (Path(env["STUB_ROOT"]) / "calls.log").read_text().splitlines()


def test_lxc_is_privileged_with_mounts_and_static_ip(stubs: dict[str, str]) -> None:
    r = _run(stubs, NM_TYPE="lxc", NM_IP="192.168.1.50/24", NM_GW="192.168.1.1", NM_VLAN="20")
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
        NM_SOURCE="https://git.example.org/netmover.git",
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
    assert "NM_SOURCE=https://git.example.org/netmover.git" in guest
    assert "NM_KEY_B64=''" in guest  # HTTPS-Quelle braucht keinen Schlüssel
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
