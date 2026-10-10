"""Ziele: lokales Verzeichnis bzw. Kernel-Mount, oder SFTP.

Fallstrick 3 – nie eine wachsende Datei unter ihrem Endnamen, nie beide Kopien
verlieren. Ablauf für jede Datei:

1. in ``<ziel>/.netmover-<uuid>.part`` schreiben, ``flush`` + ``fsync``,
   Größe prüfen,
2. Metadaten setzen (mtime, Modus, Eigentümer – Fehler sind nur Hinweise),
3. prüfen, dass sich die Quelle seit der Stabilitätsmessung nicht verändert hat,
4. atomar auf den Endnamen umbenennen – ohne zu überschreiben (``link`` bzw.
   SFTP-``rename``), außer „überschreiben“ ist ausdrücklich eingestellt,
5. erst danach löscht der Aufrufer die Quelle.

Scheitert irgendein Schritt vor 4, wird nur die ``.part``-Datei entfernt; die
Quelle ist unangetastet. Bricht der Prozess hart ab, bleibt höchstens eine
``.part``-Datei zurück, die :meth:`Destination.prepare` später aufräumt.
"""

from __future__ import annotations

import contextlib
import errno
import os
import posixpath
import stat
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from netmover.models import CollisionPolicy
from netmover.mounting.sftp import SftpSession
from netmover.mover.naming import collision_candidates
from netmover.timeouts import CancelToken

CHUNK = 1024 * 1024
STALE_PART_SECONDS = 3600
PART_PREFIX = ".netmover-"
PART_SUFFIX = ".part"

FsKind = Literal["local", "cifs", "nfs", "sftp"]

# Fehler, bei denen ``link`` nicht geht und wir auf „prüfen + rename“ ausweichen.
_NO_LINK_ERRNOS = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV, errno.ENOSYS}
)


class TransferError(Exception):
    """Übertragung dieser Datei gescheitert (Meldung im Klartext)."""


class SourceChanged(TransferError):
    """Quelle hat sich seit der Stabilitätsmessung verändert – später erneut versuchen."""


class CollisionSkip(Exception):
    """Ziel existiert und die Regel sagt „überspringen“."""


@dataclass(frozen=True)
class MetaOptions:
    atime_ns: int
    mtime_ns: int
    mode: int | None  # Rechte-Bits, None = nicht setzen
    owner: tuple[int, int] | None  # None = nicht setzen


@dataclass
class CommitResult:
    final_rel: str
    notes: list[str] = field(default_factory=list)
    source_consumed: bool = False  # True: Quelle wurde per rename bereits „verschoben“


Verify = Callable[[], None]


class Destination(ABC):
    kind: FsKind

    @abstractmethod
    def describe(self, rel: str) -> str:
        """Lesbarer Zielpfad für Protokoll und Probelauf."""

    @abstractmethod
    def prepare(self) -> None:
        """Vor dem Lauf: Zielordner prüfen, alte ``.part``-Reste entfernen."""

    @abstractmethod
    def exists(self, rel: str) -> bool: ...

    @abstractmethod
    def free_bytes(self) -> int | None: ...

    def close(self) -> None:  # noqa: B027 – absichtlich optional
        pass

    # -- Primitive der Unterklassen -------------------------------------------------

    @abstractmethod
    def _ensure_parent(self, rel: str) -> None: ...

    @abstractmethod
    def _upload(self, src: Path, tmp: str, size: int, token: CancelToken) -> list[str]: ...

    @abstractmethod
    def _apply_meta(self, rel: str, meta: MetaOptions) -> list[str]: ...

    @abstractmethod
    def _link_noreplace(self, tmp: str, final: str) -> None:
        """Atomar umbenennen; ``FileExistsError``, wenn ``final`` existiert."""

    @abstractmethod
    def _replace(self, tmp: str, final: str) -> list[str]: ...

    @abstractmethod
    def _remove(self, rel: str) -> None: ...

    def _sync_dir(self, rel_dir: str) -> None:  # noqa: B027
        pass

    # -- gemeinsamer Ablauf ---------------------------------------------------------

    def plan(self, rel: str, policy: CollisionPolicy) -> str | None:
        """Voraussichtlicher Endname (Probelauf / Vorabprüfung); None = überspringen."""
        if policy is CollisionPolicy.OVERWRITE:
            return rel
        if policy is CollisionPolicy.SKIP:
            return None if self.exists(rel) else rel
        for cand in collision_candidates(rel):
            if not self.exists(cand):
                return cand
        raise TransferError(f"Kein freier Dateiname für {rel}")

    def commit(
        self,
        src: Path,
        size: int,
        rel: str,
        policy: CollisionPolicy,
        meta: MetaOptions,
        verify: Verify,
        token: CancelToken,
    ) -> CommitResult:
        self._ensure_parent(rel)
        tmp = posixpath.join(
            posixpath.dirname(rel), f"{PART_PREFIX}{uuid.uuid4().hex}{PART_SUFFIX}"
        )
        try:
            notes = self._upload(src, tmp, size, token)
            notes += self._apply_meta(tmp, meta)
            verify()
            token.check()
            final, more = self._finalize(tmp, rel, policy)
            notes += more
        except BaseException:
            with contextlib.suppress(Exception):
                self._remove(tmp)
            raise
        self._sync_dir(posixpath.dirname(final))
        return CommitResult(final, notes)

    def move_same_device(
        self, src: Path, rel: str, policy: CollisionPolicy, meta: MetaOptions
    ) -> CommitResult | None:
        """Schneller Weg ohne Kopie, wenn Quelle und Ziel auf demselben Gerät liegen."""
        return None

    def _finalize(self, tmp: str, rel: str, policy: CollisionPolicy) -> tuple[str, list[str]]:
        if policy is CollisionPolicy.OVERWRITE:
            return rel, self._replace(tmp, rel)
        candidates = collision_candidates(rel) if policy is CollisionPolicy.RENAME else iter([rel])
        for cand in candidates:
            if self.exists(cand):
                if policy is CollisionPolicy.SKIP:
                    raise CollisionSkip
                continue
            try:
                self._link_noreplace(tmp, cand)
            except FileExistsError:
                # Wettlauf: jemand anderes hat den Namen gerade belegt
                if policy is CollisionPolicy.SKIP:
                    raise CollisionSkip from None
                continue
            return cand, []
        raise TransferError(f"Kein freier Dateiname für {rel}")


class LocalDestination(Destination):
    """Lokales Verzeichnis oder Verzeichnis auf einem (geprüften) Kernel-Mount."""

    def __init__(
        self,
        root: Path,
        kind: Literal["local", "cifs", "nfs"] = "local",
        *,
        create_root: bool = False,
    ) -> None:
        self.root = root
        self.kind = kind
        self.create_root = create_root

    def _p(self, rel: str) -> Path:
        return self.root / rel

    def describe(self, rel: str) -> str:
        return str(self._p(rel))

    def prepare(self) -> None:
        if not self.root.is_dir():
            if not self.create_root:
                raise TransferError(f"Zielordner {self.root} existiert nicht")
            self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        cutoff = time.time() - STALE_PART_SECONDS
        for entry in os.scandir(self.root):
            if entry.name.startswith(PART_PREFIX) and entry.name.endswith(PART_SUFFIX):
                with contextlib.suppress(OSError):
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self._p(rel))

    def free_bytes(self) -> int | None:
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize

    def _ensure_parent(self, rel: str) -> None:
        self._p(rel).parent.mkdir(mode=0o755, parents=True, exist_ok=True)

    def _upload(self, src: Path, tmp: str, size: int, token: CancelToken) -> list[str]:
        written = 0
        fd = os.open(self._p(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with src.open("rb") as fin:
                while chunk := fin.read(CHUNK):
                    token.check()
                    view = memoryview(chunk)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                    written += len(chunk)
            if written != size:
                raise SourceChanged(
                    "Quelle hat sich während der Übertragung geändert "
                    f"({written} statt {size} Bytes)"
                )
            os.fsync(fd)
            if os.fstat(fd).st_size != size:
                raise TransferError("Größe der Zieldatei stimmt nach dem Schreiben nicht")
        finally:
            os.close(fd)
        return []

    def _apply_meta(self, rel: str, meta: MetaOptions) -> list[str]:
        notes: list[str] = []
        path = self._p(rel)
        if self.kind != "cifs":  # auf CIFS bestimmen die Mount-Optionen Eigentümer/Rechte
            if meta.owner is not None:
                try:
                    os.chown(path, *meta.owner)
                except PermissionError:
                    notes.append(
                        f"Eigentümer {meta.owner[0]}:{meta.owner[1]} nicht gesetzt – keine "
                        "Berechtigung (läuft NetMover als root? Root-Squash?)"
                    )
                except OSError as exc:
                    notes.append(f"Eigentümer nicht gesetzt: {exc.strerror}")
            if meta.mode is not None:
                try:
                    os.chmod(path, meta.mode)
                except OSError as exc:
                    notes.append(f"Modus {meta.mode:04o} nicht gesetzt: {exc.strerror}")
        try:
            os.utime(path, ns=(meta.atime_ns, meta.mtime_ns))
        except OSError as exc:
            notes.append(f"Änderungszeit nicht übernommen: {exc.strerror}")
        return notes

    def _link_noreplace(self, tmp: str, final: str) -> None:
        src, dst = self._p(tmp), self._p(final)
        try:
            os.link(src, dst)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in _NO_LINK_ERRNOS:
                raise
            # Dateisystem ohne Hardlinks (häufig CIFS): prüfen + rename.
            # Kleines Wettlauf-Fenster, aber nie eine halbe Datei unter dem Endnamen.
            if os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, "exists", str(dst)) from None
            os.rename(src, dst)
            return
        with contextlib.suppress(OSError):
            os.unlink(src)  # Rest wird sonst später als alte .part-Datei aufgeräumt

    def _replace(self, tmp: str, final: str) -> list[str]:
        os.replace(self._p(tmp), self._p(final))
        return []

    def _remove(self, rel: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._p(rel))

    def _sync_dir(self, rel_dir: str) -> None:
        with contextlib.suppress(OSError):
            fd = os.open(self._p(rel_dir) if rel_dir else self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def move_same_device(
        self, src: Path, rel: str, policy: CollisionPolicy, meta: MetaOptions
    ) -> CommitResult | None:
        try:
            same = os.stat(src).st_dev == os.stat(self.root).st_dev
        except OSError:
            return None
        if not same:
            return None
        self._ensure_parent(rel)
        consumed = True
        if policy is CollisionPolicy.OVERWRITE:
            os.replace(src, self._p(rel))
            final = rel
        else:
            final = ""
            candidates = (
                collision_candidates(rel) if policy is CollisionPolicy.RENAME else iter([rel])
            )
            for cand in candidates:
                if self.exists(cand):
                    if policy is CollisionPolicy.SKIP:
                        raise CollisionSkip
                    continue
                try:
                    os.link(src, self._p(cand))
                except FileExistsError:
                    if policy is CollisionPolicy.SKIP:
                        raise CollisionSkip from None
                    continue
                except OSError as exc:
                    if exc.errno not in _NO_LINK_ERRNOS:
                        raise
                    if self.exists(cand):
                        continue
                    os.rename(src, self._p(cand))
                    final = cand
                    break
                # Ziel ist vollständig da; den alten Namen entfernt der Aufrufer –
                # mit derselben Fehlerbehandlung wie nach einer Kopie.
                consumed = False
                final = cand
                break
            if not final:
                raise TransferError(f"Kein freier Dateiname für {rel}")
        notes = self._apply_meta(final, meta)
        self._sync_dir(posixpath.dirname(final))
        return CommitResult(final, notes, source_consumed=consumed)


class SftpDestination(Destination):
    kind: FsKind = "sftp"

    def __init__(self, session: SftpSession, base: str, label: str = "") -> None:
        self.session = session
        self.client = session.client
        self.base = base
        self.label = label

    def _p(self, rel: str) -> str:
        return posixpath.join(self.base, rel) if rel else self.base

    def describe(self, rel: str) -> str:
        prefix = f"{self.label}:" if self.label else "sftp:"
        return prefix + self._p(rel)

    def close(self) -> None:
        self.session.close()

    def prepare(self) -> None:
        try:
            attrs = self.client.stat(self.base)
        except OSError as exc:
            raise TransferError(f"SFTP-Zielordner {self.base}: {exc}") from None
        if attrs.st_mode is None or not stat.S_ISDIR(attrs.st_mode):
            raise TransferError(f"SFTP-Ziel {self.base} ist kein Verzeichnis")
        cutoff = time.time() - STALE_PART_SECONDS
        for a in self.client.listdir_attr(self.base):
            is_part = a.filename.startswith(PART_PREFIX) and a.filename.endswith(PART_SUFFIX)
            if is_part and a.st_mtime is not None and a.st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    self.client.remove(self._p(a.filename))

    def exists(self, rel: str) -> bool:
        try:
            self.client.lstat(self._p(rel))
        except FileNotFoundError:
            return False
        return True

    def free_bytes(self) -> int | None:
        usage = self.session.statvfs(self.base)
        return usage[1] if usage else None

    def _ensure_parent(self, rel: str) -> None:
        parent = posixpath.dirname(rel)
        if not parent:
            return
        current = self.base
        for part in parent.split("/"):
            current = posixpath.join(current, part)
            try:
                self.client.stat(current)
            except FileNotFoundError:
                self.client.mkdir(current, mode=0o755)

    def _upload(self, src: Path, tmp: str, size: int, token: CancelToken) -> list[str]:
        notes: list[str] = []
        written = 0
        with self.client.open(self._p(tmp), "wb") as fh, src.open("rb") as fin:
            fh.set_pipelined(True)
            while chunk := fin.read(CHUNK // 4):
                token.check()
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
            if written != size:
                raise SourceChanged(
                    "Quelle hat sich während der Übertragung geändert "
                    f"({written} statt {size} Bytes)"
                )
            if not self.session.fsync(fh):
                notes.append("SFTP-Server unterstützt kein fsync")
        remote = self.client.stat(self._p(tmp)).st_size
        if remote != size:
            raise TransferError(f"Zieldatei hat {remote} statt {size} Bytes")
        return notes

    def _apply_meta(self, rel: str, meta: MetaOptions) -> list[str]:
        notes: list[str] = []
        path = self._p(rel)
        if meta.owner is not None:
            try:
                self.client.chown(path, *meta.owner)
            except OSError as exc:
                notes.append(f"Eigentümer per SFTP nicht gesetzt: {exc}")
        if meta.mode is not None:
            try:
                self.client.chmod(path, meta.mode)
            except OSError as exc:
                notes.append(f"Modus per SFTP nicht gesetzt: {exc}")
        try:
            self.client.utime(path, (meta.atime_ns / 1e9, meta.mtime_ns / 1e9))
        except OSError as exc:
            notes.append(f"Änderungszeit nicht übernommen: {exc}")
        return notes

    def _link_noreplace(self, tmp: str, final: str) -> None:
        if self.exists(final):
            raise FileExistsError(errno.EEXIST, "exists", final)
        try:
            # SFTPv3-rename überschreibt nicht (OpenSSH: link + unlink).
            self.client.rename(self._p(tmp), self._p(final))
        except OSError:
            if self.exists(final):
                raise FileExistsError(errno.EEXIST, "exists", final) from None
            raise

    def _replace(self, tmp: str, final: str) -> list[str]:
        try:
            self.client.posix_rename(self._p(tmp), self._p(final))
        except OSError:
            # Server ohne posix-rename: löschen + umbenennen (kurz kein Ziel, nie halb)
            with contextlib.suppress(FileNotFoundError):
                self.client.remove(self._p(final))
            self.client.rename(self._p(tmp), self._p(final))
            return ["Server kann nicht atomar überschreiben – Ziel kurz nicht vorhanden"]
        return []

    def _remove(self, rel: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.client.remove(self._p(rel))
