"""Fallstrick 2: „eingehängt?“ entscheidet /proc/self/mountinfo."""

from __future__ import annotations

from pathlib import Path

from netmover.mounting.mountinfo import find_mount, parse_mountinfo

SAMPLE = """\
22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
98 22 0:52 / /mnt/netmover/scans rw,relatime shared:50 - cifs //nas/scans rw,vers=3.1.1
99 22 0:53 / /mnt/netmover/mit\\040leer rw,relatime - nfs4 nas:/volume1/a\\040b rw
100 22 0:54 / /mnt/netmover/scans rw,relatime - cifs //nas2/scans rw
"""


def test_parse_with_octal_escapes() -> None:
    entries = parse_mountinfo(SAMPLE)
    assert len(entries) == 4
    assert entries[2].mountpoint == "/mnt/netmover/mit leer"
    assert entries[2].source == "nas:/volume1/a b"
    assert entries[2].fstype == "nfs4"


def test_find_mount_uses_topmost_entry() -> None:
    entries = parse_mountinfo(SAMPLE)
    found = find_mount(Path("/mnt/netmover/scans/"), entries)
    assert found is not None
    assert found.source == "//nas2/scans"


def test_unmounted_directory_is_not_mounted() -> None:
    entries = parse_mountinfo(SAMPLE)
    assert find_mount(Path("/mnt/netmover/archiv"), entries) is None
    # Unterverzeichnis eines Mounts ist selbst kein Mountpoint
    assert find_mount(Path("/mnt/netmover/scans/eingang"), entries) is None


def test_garbage_lines_are_ignored() -> None:
    assert parse_mountinfo("kaputt\n\n1 2 3\n") == []
