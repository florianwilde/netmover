"""Test-Implementierung: simuliert Mounts auf einem temporären Verzeichnis.

* „Entfernte“ Freigaben sind Verzeichnisse unter ``<root>/remote/<server>/<share>``
  (anlegen mit :meth:`FakeMountBackend.export`).
* Einhängen ersetzt das leere Mountpoint-Verzeichnis durch einen Symlink auf die
  Freigabe; Aushängen stellt das leere Verzeichnis wieder her. Ein ausgehängter
  Mountpoint ist also – genau wie in echt – ein ganz normales leeres Verzeichnis.
* :meth:`inject_fault` lässt das Lebenszeichen eines Mounts mit ``ESTALE``,
  ``EIO``, ``ENOTCONN`` scheitern oder hängen (Fallstrick 13).
"""

from __future__ import annotations

import errno
import os
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from netmover.mounting.backend import MountBackend, MountError, MountSpec

Fault = int | Literal["hang"]


@dataclass
class FakeCall:
    action: str
    mountpoint: Path
    spec: MountSpec | None = None
    lazy: bool = False


class FakeMountBackend(MountBackend):
    def __init__(self, root: Path) -> None:
        self.remote_root = root / "remote"
        self.remote_root.mkdir(parents=True, exist_ok=True)
        self.mounted: dict[Path, Path] = {}
        self.faults: dict[Path, Fault] = {}
        self.mount_failures: deque[str] = deque()
        self.heal_on_remount = True
        self.busy: set[Path] = set()
        self.calls: list[FakeCall] = []
        self._release = threading.Event()

    # -- Steuerung für Tests -----------------------------------------------------

    def export(self, server: str, share: str) -> Path:
        path = self.remote_root / server / share.strip("/")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def fail_next_mount(
        self, message: str = "mount error(112): Host is down", times: int = 1
    ) -> None:
        self.mount_failures.extend([message] * times)

    def inject_fault(self, mountpoint: Path, fault: Fault) -> None:
        self.faults[Path(mountpoint)] = fault

    def clear_fault(self, mountpoint: Path) -> None:
        self.faults.pop(Path(mountpoint), None)

    def vanish(self, mountpoint: Path) -> None:
        """Mount verschwindet von außen (z. B. ``umount`` durch Dritte)."""
        mp = Path(mountpoint)
        if mp in self.mounted:
            mp.unlink()
            mp.mkdir()
            del self.mounted[mp]

    def release_hangs(self) -> None:
        self._release.set()

    def _remote_for(self, spec: MountSpec) -> Path:
        if spec.source.startswith("//"):
            server, _, share = spec.source[2:].partition("/")
        else:
            server, _, share = spec.source.partition(":")
        return self.remote_root / server / share.strip("/")

    # -- Interface -----------------------------------------------------------------

    def mount(self, spec: MountSpec, timeout: float = 60) -> None:
        mp = Path(spec.mountpoint)
        self.calls.append(FakeCall("mount", mp, spec=spec))
        if self.mount_failures:
            raise MountError(self.mount_failures.popleft())
        if mp in self.mounted:
            raise MountError(f"mount: {mp}: already mounted")
        remote = self._remote_for(spec)
        if not remote.is_dir():
            raise MountError("mount error(2): No such file or directory")
        if not mp.is_dir() or mp.is_symlink():
            raise MountError(f"mount: {mp}: mount point does not exist")
        mp.rmdir()  # scheitert, wenn nicht leer – wie ein echter Mount, der verdeckt
        mp.symlink_to(remote, target_is_directory=True)
        self.mounted[mp] = remote
        if self.heal_on_remount:
            self.faults.pop(mp, None)

    def unmount(self, mountpoint: Path, lazy: bool = False, timeout: float = 60) -> None:
        mp = Path(mountpoint)
        self.calls.append(FakeCall("unmount", mp, lazy=lazy))
        if mp not in self.mounted:
            raise MountError(f"umount: {mp}: not mounted.")
        if mp in self.busy and not lazy:
            raise MountError(f"umount: {mp}: target is busy.")
        mp.unlink()
        mp.mkdir()
        del self.mounted[mp]
        self.busy.discard(mp)

    def is_mounted(self, mountpoint: Path) -> bool:
        return Path(mountpoint) in self.mounted

    def probe(self, path: Path) -> None:
        p = Path(path)
        for mp, fault in self.faults.items():
            if p == mp or mp in p.parents:
                if fault == "hang":
                    self._release.wait()
                    raise OSError(errno.EIO, os.strerror(errno.EIO), str(p))
                raise OSError(fault, os.strerror(fault), str(p))
        super().probe(p)
