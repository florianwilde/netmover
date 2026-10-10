"""Namenskollisionen (Fallstrick 4): ``scan001.pdf`` → ``scan001-1.pdf`` → …

Die Kandidaten werden nur erzeugt; ob ein Name frei ist, entscheidet jedes Mal
das Ziel-Dateisystem (kein Vorhalten im Speicher).
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

MAX_SUFFIX = 9999


def collision_candidates(relpath: str) -> Iterator[str]:
    yield relpath
    directory, name = posixpath.split(relpath)
    stem, dot, ext = name.rpartition(".")
    if not stem:  # kein Punkt bzw. nur führender Punkt
        stem, dot, ext = name, "", ""
    for n in range(1, MAX_SUFFIX + 1):
        yield posixpath.join(directory, f"{stem}-{n}{dot}{ext}")
