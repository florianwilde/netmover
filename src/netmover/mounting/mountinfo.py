"""``/proc/self/mountinfo`` lesen (Fallstrick 2).

Ob ein Verzeichnis eingehängt ist, entscheidet ausschließlich der Kernel – nie
das Vorhandensein von Dateien. Ein leerer Mountpoint sieht sonst genauso aus wie
eine leere Freigabe.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")

_OCTAL_RE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    # Der Kernel kodiert Leerzeichen, Tab, Zeilenumbruch und Backslash oktal.
    return _OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), field)


@dataclass(frozen=True)
class MountEntry:
    mountpoint: str
    fstype: str
    source: str
    options: str


def parse_mountinfo(text: str) -> list[MountEntry]:
    entries: list[MountEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lf = left.split(" ")
        rf = right.split(" ")
        if len(lf) < 6 or len(rf) < 3:
            continue
        entries.append(
            MountEntry(
                mountpoint=_unescape(lf[4]),
                fstype=rf[0],
                source=_unescape(rf[1]),
                options=rf[2],
            )
        )
    return entries


def read_mountinfo(path: Path = MOUNTINFO) -> list[MountEntry]:
    return parse_mountinfo(path.read_text(encoding="utf-8", errors="surrogateescape"))


def find_mount(mountpoint: Path, entries: list[MountEntry]) -> MountEntry | None:
    """Liefert den obersten Eintrag für genau diesen Mountpoint (bei Übereinander-
    Mounts gewinnt der zuletzt eingehängte)."""
    target = os.path.normpath(str(mountpoint))
    found: MountEntry | None = None
    for entry in entries:
        if entry.mountpoint == target:
            found = entry
    return found
