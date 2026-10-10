"""Kandidaten im Quellordner finden (Fallstrick 1, erster Teil).

Übersprungen werden immer:
* versteckte Dateien und Ordner (führender Punkt) – darunter auch unsere eigenen
  ``.netmover-*.part``-Temporärdateien,
* typische Endungen unfertiger Übertragungen,
* Symlinks und alles, was keine reguläre Datei ist,
* Synology-Verwaltungsordner (``@eaDir``, ``#recycle``, ``#snapshot``).
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from netmover.models import PatternType

PARTIAL_SUFFIXES = (".part", ".tmp", ".crdownload", ".filepart", ".!ut")
IGNORED_DIRS = frozenset({"@eaDir", "#recycle", "#snapshot", "@tmp", "@sharebin"})


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Candidate:
    relpath: str  # POSIX, relativ zum Quellordner
    path: Path
    state: FileState


def is_ignored_name(name: str) -> bool:
    return name.startswith(".") or name.lower().endswith(PARTIAL_SUFFIXES)


class Matcher:
    """Glob: gegen den Dateinamen, ohne Groß-/Kleinschreibung, mehrere Muster mit
    „;“ getrennt (``*.pdf;*.jpg``). Regex: ``re.search`` gegen den relativen Pfad."""

    def __init__(self, pattern: str, pattern_type: PatternType) -> None:
        self.pattern_type = pattern_type
        if pattern_type is PatternType.REGEX:
            self._regex = re.compile(pattern)
            self._globs: list[str] = []
        else:
            self._globs = [p.strip().lower() for p in pattern.split(";") if p.strip()] or ["*"]

    def __call__(self, relpath: str) -> bool:
        if self.pattern_type is PatternType.REGEX:
            return self._regex.search(relpath) is not None
        name = relpath.rsplit("/", 1)[-1].lower()
        return any(fnmatch.fnmatchcase(name, g) for g in self._globs)


def validate_pattern(pattern: str, pattern_type: PatternType) -> None:
    if pattern_type is PatternType.REGEX:
        re.compile(pattern)


def scan(
    root: Path,
    matcher: Matcher,
    recursive: bool,
    exclude: frozenset[Path] = frozenset(),
) -> Iterator[Candidate]:
    """Liefert passende reguläre Dateien. ``exclude``: Verzeichnisse (realpath), die
    nicht betreten werden – z. B. Quarantäne oder ein Ziel innerhalb der Quelle."""

    def walk(directory: Path, prefix: str) -> Iterator[Candidate]:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            rel = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                if (
                    recursive
                    and entry.name not in IGNORED_DIRS
                    and Path(entry.path).resolve() not in exclude
                ):
                    yield from walk(Path(entry.path), f"{rel}/")
                continue
            if is_ignored_name(entry.name):
                continue
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                continue
            if matcher(rel):
                yield Candidate(rel, Path(entry.path), FileState(st.st_size, st.st_mtime_ns))

    yield from walk(root, "")


def current_state(path: Path) -> FileState | None:
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return FileState(st.st_size, st.st_mtime_ns)
