"""Quellen: eingehängtes Verzeichnis (SMB/NFS) oder SFTP.

Die Engine greift nur über diese Schnittstelle auf die Quelle zu – scannen,
frischen Zustand messen, lesen, löschen, in die Quarantäne verschieben. So gelten
für SFTP-Quellen dieselben Garantien wie für eingehängte Freigaben.

Adressierung: ``root`` ist die Wurzel der Anbindung, ``subdir`` der Quell-
Unterordner der Regel. Kandidaten tragen Pfade relativ zu ``subdir`` (so landen sie
im Ziel und in der Buchführung); die Quarantäne liegt relativ zu ``root``.

SFTP-Besonderheit: Änderungszeiten kommen nur sekundengenau. Die Stabilitäts-
prüfung stützt sich dort vor allem auf die Größe – mit einem Fenster ab 2 s
unproblematisch.
"""

from __future__ import annotations

import contextlib
import errno
import os
import posixpath
import socket
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import paramiko

from netmover.mounting.sftp import SftpSession
from netmover.mover.fsops import rename_noreplace
from netmover.mover.scan import (
    IGNORED_DIRS,
    Candidate,
    FileState,
    Matcher,
    current_state,
    is_ignored_name,
    scan,
)

_NET_ERRORS = (paramiko.SSHException, EOFError, socket.timeout, ConnectionError)


class SourceMissing(Exception):
    """Quellordner existiert nicht (Lauf abbrechen, nichts anfassen)."""


@dataclass(frozen=True)
class SourceStat:
    size: int
    mtime_ns: int
    atime_ns: int
    mode: int  # Rechte-Bits


def _join(*parts: str) -> str:
    return posixpath.join(*[p for p in parts if p]) if any(parts) else ""


@contextlib.contextmanager
def network_errors() -> Iterator[None]:
    """Verbindungsabbrüche bei SFTP als ``ENOTCONN`` melden – die Engine bricht den
    Lauf dann ab (Anbindung gestört), statt jede Datei einzeln scheitern zu lassen."""
    try:
        yield
    except _NET_ERRORS as exc:
        raise OSError(errno.ENOTCONN, f"SFTP-Verbindung unterbrochen: {exc}") from exc


class Source(ABC):
    kind: str

    def __init__(self, subdir: str = "") -> None:
        self.subdir = subdir.strip("/")

    def full(self, rel: str) -> str:
        """Pfad relativ zur Wurzel der Anbindung."""
        return _join(self.subdir, rel)

    @abstractmethod
    def describe(self, rel: str = "") -> str: ...

    @abstractmethod
    def check_root(self) -> None:
        """Wirft :class:`SourceMissing`, wenn der Quellordner fehlt."""

    @abstractmethod
    def scan(
        self, matcher: Matcher, recursive: bool, exclude: frozenset[str] = frozenset()
    ) -> list[Candidate]:
        """``exclude``: Verzeichnisse relativ zur Wurzel der Anbindung."""

    @abstractmethod
    def state(self, rel: str) -> FileState | None:
        """Frischer Zustand; ``None`` = verschwunden, ``BUSY`` = gerade gesperrt."""

    @abstractmethod
    def stat(self, rel: str) -> SourceStat: ...

    @abstractmethod
    def open(self, rel: str) -> contextlib.AbstractContextManager[IO[bytes]]: ...

    @abstractmethod
    def delete(self, rel: str) -> None: ...

    @abstractmethod
    def exists_root_rel(self, root_rel: str) -> bool: ...

    @abstractmethod
    def describe_root_rel(self, root_rel: str) -> str: ...

    @abstractmethod
    def move_to(self, rel: str, root_rel_target: str) -> None:
        """Innerhalb der Anbindung verschieben, ohne zu überschreiben
        (``FileExistsError``, wenn das Ziel existiert)."""

    def local_path(self, rel: str) -> Path | None:
        """Lokaler Pfad, falls vorhanden (für den Schnellweg ohne Kopie)."""
        return None

    def close(self) -> None:  # noqa: B027 – absichtlich optional
        pass


class LocalSource(Source):
    """Eingehängte Freigabe (oder lokales Verzeichnis)."""

    kind = "local"

    def __init__(self, root: Path, subdir: str = "") -> None:
        super().__init__(subdir)
        self.root = root
        self.base = root / self.subdir if self.subdir else root
        # zusätzlich auszulassende Verzeichnisse, z. B. ein Ziel innerhalb der Quelle
        self.exclude_paths: set[Path] = set()

    def _p(self, rel: str) -> Path:
        return self.base / rel

    def describe(self, rel: str = "") -> str:
        return str(self._p(rel)) if rel else str(self.base)

    def check_root(self) -> None:
        if not self.base.is_dir():
            raise SourceMissing(f"Quellordner {self.base} existiert nicht")

    def scan(
        self, matcher: Matcher, recursive: bool, exclude: frozenset[str] = frozenset()
    ) -> list[Candidate]:
        paths = {(self.root / e).resolve() for e in exclude} | set(self.exclude_paths)
        return list(scan(self.base, matcher, recursive, frozenset(paths)))

    def state(self, rel: str) -> FileState | None:
        return current_state(self._p(rel))

    def stat(self, rel: str) -> SourceStat:
        st = os.stat(self._p(rel))
        return SourceStat(st.st_size, st.st_mtime_ns, st.st_atime_ns, stat.S_IMODE(st.st_mode))

    def open(self, rel: str) -> contextlib.AbstractContextManager[IO[bytes]]:
        return self._p(rel).open("rb")

    def delete(self, rel: str) -> None:
        os.unlink(self._p(rel))

    def exists_root_rel(self, root_rel: str) -> bool:
        return os.path.lexists(self.root / root_rel)

    def describe_root_rel(self, root_rel: str) -> str:
        return str(self.root / root_rel)

    def move_to(self, rel: str, root_rel_target: str) -> None:
        target = self.root / root_rel_target
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if rename_noreplace(self._p(rel), target):
            return
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "exists", str(target))
        os.rename(self._p(rel), target)

    def local_path(self, rel: str) -> Path:
        return self._p(rel)


class SftpSource(Source):
    """Quelle auf einem SFTP-Server (z. B. ein Server, der nur per SSH erreichbar ist)."""

    kind = "sftp"

    def __init__(self, session: SftpSession, base: str, subdir: str = "", label: str = ""):
        super().__init__(subdir)
        self.session = session
        self.client = session.client
        self.root = base.rstrip("/") or "/"
        self.label = label or "sftp"

    def _p(self, rel: str) -> str:
        return posixpath.join(self.root, self.full(rel)) if self.full(rel) else self.root

    def describe(self, rel: str = "") -> str:
        return f"{self.label}:{self._p(rel)}"

    def close(self) -> None:
        self.session.close()

    def check_root(self) -> None:
        with network_errors():
            try:
                attrs = self.client.stat(self._p(""))
            except FileNotFoundError:
                raise SourceMissing(f"Quellordner {self.describe()} existiert nicht") from None
        if attrs.st_mode is None or not stat.S_ISDIR(attrs.st_mode):
            raise SourceMissing(f"{self.describe()} ist kein Verzeichnis")

    def scan(
        self, matcher: Matcher, recursive: bool, exclude: frozenset[str] = frozenset()
    ) -> list[Candidate]:
        excluded = {e.strip("/") for e in exclude}
        found: list[Candidate] = []

        def walk(rel_dir: str) -> None:
            with network_errors():
                entries = sorted(
                    self.client.listdir_attr(self._p(rel_dir)), key=lambda a: a.filename
                )
            for a in entries:
                name = a.filename
                if name.startswith(".") or a.st_mode is None:
                    continue
                rel = _join(rel_dir, name)
                if stat.S_ISDIR(a.st_mode):
                    if recursive and name not in IGNORED_DIRS and self.full(rel) not in excluded:
                        walk(rel)
                    continue
                if not stat.S_ISREG(a.st_mode) or is_ignored_name(name):
                    continue  # Symlinks, Geräte … und unfertige Übertragungen
                if matcher(rel):
                    found.append(Candidate(rel, None, _state(a)))

        walk("")
        return found

    def state(self, rel: str) -> FileState | None:
        with network_errors():
            try:
                a = self.client.lstat(self._p(rel))
            except FileNotFoundError:
                return None
        if a.st_mode is None or not stat.S_ISREG(a.st_mode):
            return None
        return _state(a)

    def stat(self, rel: str) -> SourceStat:
        with network_errors():
            a = self.client.lstat(self._p(rel))
        mtime = int(a.st_mtime or 0) * 1_000_000_000
        atime = int(a.st_atime or a.st_mtime or 0) * 1_000_000_000
        return SourceStat(int(a.st_size or 0), mtime, atime, stat.S_IMODE(a.st_mode or 0o644))

    @contextlib.contextmanager
    def _reader(self, rel: str) -> Iterator[IO[bytes]]:
        with network_errors():
            size = self.client.lstat(self._p(rel)).st_size
            fh: Any = self.client.open(self._p(rel), "rb")
            try:
                fh.prefetch(size)  # Größe mitgeben: spart ein fstat und liest vorab
                yield fh
            finally:
                with contextlib.suppress(*_NET_ERRORS, OSError):
                    fh.close()

    def open(self, rel: str) -> contextlib.AbstractContextManager[IO[bytes]]:
        return self._reader(rel)

    def delete(self, rel: str) -> None:
        with network_errors():
            self.client.remove(self._p(rel))

    def exists_root_rel(self, root_rel: str) -> bool:
        with network_errors():
            try:
                self.client.lstat(posixpath.join(self.root, root_rel))
            except FileNotFoundError:
                return False
        return True

    def describe_root_rel(self, root_rel: str) -> str:
        return f"{self.label}:{posixpath.join(self.root, root_rel)}"

    def move_to(self, rel: str, root_rel_target: str) -> None:
        target = posixpath.join(self.root, root_rel_target)
        with network_errors():
            current = self.root
            for part in posixpath.dirname(root_rel_target).split("/"):
                if not part:
                    continue
                current = posixpath.join(current, part)
                try:
                    self.client.stat(current)
                except FileNotFoundError:
                    self.client.mkdir(current, mode=0o755)
            if self.exists_root_rel(root_rel_target):
                raise FileExistsError(errno.EEXIST, "exists", target)
            self.client.rename(self._p(rel), target)  # SFTPv3-rename überschreibt nicht


def _state(a: paramiko.SFTPAttributes) -> FileState:
    return FileState(int(a.st_size or 0), int(a.st_mtime or 0) * 1_000_000_000)
