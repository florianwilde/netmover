"""Interface der Mount-Schicht.

Alles, was den Kernel betrifft (einhängen, aushängen, „ist eingehängt?“, Lebens-
zeichen), läuft über :class:`MountBackend`. Dadurch ist die gesamte Mover-Logik
mit :class:`~netmover.mounting.fake.FakeMountBackend` ohne root testbar.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CifsCredentials:
    username: str
    password: str
    domain: str | None = None

    def __repr__(self) -> str:  # nie das Passwort in Logs/Tracebacks
        return f"CifsCredentials(username={self.username!r}, password='***')"


@dataclass(frozen=True)
class MountSpec:
    fstype: str  # Kernel-Dateisystemtyp: "cifs", "nfs", "nfs4"
    source: str  # "//server/share" bzw. "server:/export"
    mountpoint: Path
    options: tuple[str, ...] = ()
    credentials: CifsCredentials | None = field(default=None, repr=True)


class MountError(RuntimeError):
    """``mount``/``umount`` gescheitert. Die Meldung ist bereits geschwärzt."""


class MountBackend(ABC):
    @abstractmethod
    def mount(self, spec: MountSpec, timeout: float = 60) -> None: ...

    @abstractmethod
    def unmount(self, mountpoint: Path, lazy: bool = False, timeout: float = 60) -> None: ...

    @abstractmethod
    def is_mounted(self, mountpoint: Path) -> bool: ...

    def probe(self, path: Path) -> None:
        """Lebenszeichen: ``stat`` + ``listdir``. Kann bei hängenden Mounts blockieren
        – Aufrufer verwenden :func:`netmover.timeouts.run_with_timeout`."""
        os.stat(path)
        os.listdir(path)
