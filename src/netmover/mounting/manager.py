"""Verwaltung der Anbindungen: einhängen, aushängen, testen, wiederherstellen.

Verbindet Datenbank, Geheimnisse und :class:`MountBackend`. Alle Methoden sind
synchron und blockierend; der Web-Teil ruft sie über ``asyncio.to_thread`` auf,
die Regel-Worker direkt.

Fallstricke:
* 2  – :meth:`MountManager.check_alive` prüft „eingehängt“ über das Backend
  (``/proc/self/mountinfo``) und bricht ab, statt in ein leeres Verzeichnis zu schreiben.
* 6  – Mountpoints entstehen nur als ``<mount_base>/<slug>``; vor dem Einhängen
  existiert das Verzeichnis und ist leer, nach dem Aushängen wird es entfernt.
* 13 – Lebenszeichen mit Zeitlimit; bei ESTALE/EIO/ENOTCONN/Zeitüberschreitung
  ``umount -l`` und neu einhängen, höchstens N Versuche mit wachsendem Abstand.
"""

from __future__ import annotations

import contextlib
import errno
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.crypto import SecretBox, redact
from netmover.db import session_scope
from netmover.logging_setup import event, get_logger
from netmover.models import CredentialKind, Mount, MountState, MountType, Rule, utcnow
from netmover.mounting.backend import CifsCredentials, MountBackend, MountError, MountSpec
from netmover.mounting.report import CheckReport, assess_write
from netmover.mounting.sftp import SftpConfig, SftpError, check_connection, connect
from netmover.timeouts import OperationTimeout, run_with_timeout
from netmover.validation import (
    ValidationError,
    parse_extra_options,
    slugify,
    validate_server,
    validate_share,
)

log = get_logger(__name__)

# Fehler, bei denen ein Neu-Einhängen hilft (Fallstrick 13).
RECOVERABLE_ERRNOS = frozenset({errno.ESTALE, errno.EIO, errno.ENOTCONN, errno.EHOSTDOWN})

Notifier = Callable[[str, str, str], None]  # (ereignis, anbindung, meldung)


class MountUnavailable(RuntimeError):
    """Anbindung für einen Lauf nicht nutzbar – Lauf abbrechen, nichts anfassen."""


@dataclass(frozen=True)
class CredentialData:
    kind: CredentialKind
    username: str
    secret: str
    domain: str | None = None

    def __repr__(self) -> str:
        return f"CredentialData(kind={self.kind}, username={self.username!r}, secret='***')"


@dataclass(frozen=True)
class MountConfig:
    name: str
    type: MountType
    server: str
    share: str
    port: int | None = None
    host_key: str | None = None
    uid: int | None = None
    gid: int | None = None
    file_mode: str | None = None
    dir_mode: str | None = None
    extra_options: str = ""

    @classmethod
    def from_model(cls, m: Mount) -> MountConfig:
        return cls(
            name=m.name,
            type=m.type,
            server=m.server,
            share=m.share,
            port=m.port,
            host_key=m.host_key,
            uid=m.uid,
            gid=m.gid,
            file_mode=m.file_mode,
            dir_mode=m.dir_mode,
            extra_options=m.extra_options,
        )


@dataclass(frozen=True)
class RestoreResult:
    name: str
    ok: bool
    message: str


def _redact_output(message: str, secrets: Sequence[str]) -> str:
    """Schwärzt nur die Ausgabe von ``mount``, nicht den angehängten Hinweistext
    (sonst würde z. B. das Passwort „falsch“ auch im Hinweis geschwärzt)."""
    output, sep, hint = message.partition("\n→ ")
    return redact(output, secrets) + sep + hint


def build_spec(cfg: MountConfig, mountpoint: Path, cred: CredentialData | None) -> MountSpec:
    """Baut den ``mount``-Aufruf. Validiert erneut (Verteidigung in der Tiefe) –
    auch Daten aus der Datenbank laufen hier noch einmal durch die Prüfung."""
    if not cfg.type.is_kernel_mount:
        raise MountError("SFTP-Anbindungen werden nicht eingehängt")
    server = validate_server(cfg.server)
    share = validate_share(cfg.type, cfg.share)
    options = [o.render() for o in parse_extra_options(cfg.type, cfg.extra_options)]
    credentials: CifsCredentials | None = None

    if cfg.type is MountType.CIFS:
        fstype, source = "cifs", f"//{server}/{share}"
        for key, value in (
            ("uid", cfg.uid),
            ("gid", cfg.gid),
            ("file_mode", cfg.file_mode),
            ("dir_mode", cfg.dir_mode),
        ):
            if value is not None:
                options.append(f"{key}={value}")
        if cred is None:
            options.append("guest")
        else:
            if cred.kind is not CredentialKind.PASSWORD:
                raise MountError("CIFS braucht Zugangsdaten mit Passwort, keinen SSH-Schlüssel")
            credentials = CifsCredentials(cred.username, cred.secret, cred.domain)
    elif cfg.type is MountType.NFS4:
        fstype, source = "nfs4", f"{server}:{share}"
    else:
        fstype, source = "nfs", f"{server}:{share}"
        options.insert(0, "vers=3")
    return MountSpec(fstype, source, mountpoint, tuple(options), credentials)


def sftp_config(cfg: MountConfig, cred: CredentialData | None) -> SftpConfig:
    if cred is None:
        raise SftpError("SFTP braucht Zugangsdaten (SSH-Schlüssel oder Passwort)")
    return SftpConfig(
        host=validate_server(cfg.server),
        port=cfg.port or 22,
        username=cred.username,
        base_path=validate_share(MountType.SFTP, cfg.share),
        password=cred.secret if cred.kind is CredentialKind.PASSWORD else None,
        private_key=cred.secret if cred.kind is CredentialKind.SSH_KEY else None,
        host_key=cfg.host_key,
    )


class MountManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        backend: MountBackend,
        secrets: SecretBox,
        mount_base: Path,
        *,
        mount_timeout: float = 60,
        probe_timeout: float = 15,
        recover_backoff: Sequence[float] = (0, 5, 15),
        sleep: Callable[[float], None] = time.sleep,
        euid: Callable[[], int] = os.geteuid,
        notifier: Notifier | None = None,
        retry_interval: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.backend = backend
        self.secrets = secrets
        self.mount_base = mount_base
        self.mount_timeout = mount_timeout
        self.probe_timeout = probe_timeout
        self.recover_backoff = tuple(recover_backoff)
        self._sleep = sleep
        self._euid = euid
        self._notifier = notifier
        self.retry_interval = retry_interval
        self._clock = clock
        # Mounts, die NetMover nach gescheiterter Wiederherstellung selbst ausgehängt
        # hat: nächster erlaubter Versuch (monotone Zeit). Nur diese werden
        # automatisch neu eingehängt – von außen ausgehängte nie (Fallstrick 2).
        self._retry_at: dict[int, float] = {}
        self._locks: dict[int, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Füllstand für die Statusseite: (Zeitpunkt, Wert) und laufende Abfragen
        # (Fertig-Signal, Startzeit). Je Mount höchstens eine Abfrage gleichzeitig.
        self._usage_cache: dict[int, tuple[float, tuple[int, int] | None]] = {}
        self._usage_pending: dict[int, tuple[threading.Event, float]] = {}
        self._usage_guard = threading.Lock()

    # -- Hilfen --------------------------------------------------------------------

    def _lock(self, mount_id: int) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(mount_id, threading.RLock())

    def _notify(self, kind: str, name: str, message: str, mount_id: int | None = None) -> None:
        if self._notifier is None:
            return
        if mount_id is not None:
            rules = self._affected_rules(mount_id)
            if rules and kind != "mount.recovered":
                waiting = "\nBetroffene Regeln (warten, bis die Anbindung zurück ist): "
                message += waiting + ", ".join(rules)
            elif rules:
                message += f"\nRegeln laufen wieder: {', '.join(rules)}"
        try:
            self._notifier(kind, name, message)
        except Exception:
            log.exception("notifier.failed")

    def _affected_rules(self, mount_id: int) -> list[str]:
        with session_scope(self.session_factory) as s:
            return list(
                s.scalars(
                    select(Rule.name)
                    .where(Rule.enabled.is_(True))
                    .where((Rule.src_mount_id == mount_id) | (Rule.dst_mount_id == mount_id))
                    .order_by(Rule.name)
                )
            )

    def mountpoint_for(self, slug: str) -> Path:
        if slugify(slug) != slug:
            raise ValidationError("slug", f"Ungültiger Slug: {slug!r}")
        base = self.mount_base.resolve()
        mp = base / slug
        if mp.parent != base:
            raise ValidationError("slug", "Mountpoint außerhalb des Basisverzeichnisses")
        return mp

    def _load(self, mount_id: int) -> tuple[Mount, CredentialData | None]:
        with session_scope(self.session_factory) as s:
            m = s.get(Mount, mount_id)
            if m is None:
                raise LookupError(f"Anbindung {mount_id} existiert nicht")
            cred: CredentialData | None = None
            if m.credential is not None:
                c = m.credential
                cred = CredentialData(
                    c.kind, m.username or c.username, self.secrets.decrypt(c.secret_enc), c.domain
                )
            s.expunge(m)
            return m, cred

    def _set_state(
        self,
        mount_id: int,
        state: MountState,
        error: str | None = None,
        *,
        mounted: bool = False,
    ) -> None:
        with session_scope(self.session_factory) as s:
            m = s.get(Mount, mount_id)
            if m is None:
                return
            if m.state is not state:
                m.state_since = utcnow()
                event(log, "mount.state", mount=m.name, old=m.state.value, new=state.value)
            m.state = state
            m.last_error = error
            if mounted:
                m.last_mounted_at = utcnow()
        with self._usage_guard:
            self._usage_cache.pop(mount_id, None)

    def _prepare_mountpoint(self, mp: Path) -> bool:
        """Legt den Mountpoint an bzw. prüft, dass er leer ist. True = neu angelegt."""
        self.mount_base.mkdir(mode=0o755, parents=True, exist_ok=True)
        if mp.is_symlink() or (mp.exists() and not mp.is_dir()):
            raise MountError(f"{mp} ist kein Verzeichnis – bitte von Hand prüfen")
        if mp.is_dir():
            if any(mp.iterdir()):
                raise MountError(
                    f"{mp} ist nicht leer. Vermutlich wurde dort geschrieben, während nichts "
                    "eingehängt war. Inhalt prüfen und sichern, dann Verzeichnis leeren."
                )
            return False
        mp.mkdir(mode=0o755)
        return True

    @staticmethod
    def _remove_mountpoint(mp: Path) -> None:
        try:
            mp.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            event(log, "mount.rmdir_failed", level=30, mountpoint=str(mp), error=str(exc))

    def _probe(self, name: str, mp: Path) -> None:
        run_with_timeout(lambda: self.backend.probe(mp), self.probe_timeout, f"probe:{name}")

    # -- Einhängen / Aushängen -----------------------------------------------------

    def mount(self, mount_id: int) -> None:
        with self._lock(mount_id):
            self._retry_at.pop(mount_id, None)
            m, cred = self._load(mount_id)
            if not m.type.is_kernel_mount:
                raise MountError("SFTP-Anbindungen werden nicht eingehängt, nur getestet")
            mp = self.mountpoint_for(m.slug)
            if self.backend.is_mounted(mp):
                self._set_state(mount_id, MountState.MOUNTED)
                return
            try:
                spec = build_spec(MountConfig.from_model(m), mp, cred)
                created = self._prepare_mountpoint(mp)
            except (MountError, ValidationError) as exc:
                self._set_state(mount_id, MountState.ERROR, str(exc))
                raise MountError(str(exc)) from None
            try:
                self.backend.mount(spec, timeout=self.mount_timeout)
            except MountError as exc:
                if created:
                    self._remove_mountpoint(mp)
                self._set_state(mount_id, MountState.ERROR, str(exc))
                event(log, "mount.failed", level=40, mount=m.name, error=str(exc))
                raise
            self._set_state(mount_id, MountState.MOUNTED, mounted=True)

    def unmount(self, mount_id: int) -> None:
        with self._lock(mount_id):
            self._retry_at.pop(mount_id, None)
            m, _ = self._load(mount_id)
            if not m.type.is_kernel_mount:
                self._set_state(mount_id, MountState.UNMOUNTED)
                return
            mp = self.mountpoint_for(m.slug)
            if self.backend.is_mounted(mp):
                try:
                    self.backend.unmount(mp, timeout=self.mount_timeout)
                except MountError as exc:
                    msg = str(exc)
                    if "busy" in msg.lower():
                        msg += "\n→ Ein Prozess benutzt den Mount noch (z. B. ein Regellauf)."
                    raise MountError(msg) from None
            if not self.backend.is_mounted(mp):
                self._remove_mountpoint(mp)
            self._set_state(mount_id, MountState.UNMOUNTED)

    def restore_all(self) -> list[RestoreResult]:
        """Beim Dienststart: alle Anbindungen mit ``auto_mount`` wiederherstellen."""
        with session_scope(self.session_factory) as s:
            rows = s.execute(
                select(Mount.id, Mount.name, Mount.type).where(
                    Mount.auto_mount.is_(True), Mount.enabled.is_(True)
                )
            ).all()
        results: list[RestoreResult] = []
        for mount_id, name, mtype in rows:
            if not MountType(mtype).is_kernel_mount:
                continue
            try:
                self.mount(mount_id)
                results.append(RestoreResult(name, True, "eingehängt"))
            except (MountError, ValidationError, LookupError) as exc:
                # z. B. Stromausfall: die VM bootet schneller als das NAS
                self._retry_at[mount_id] = self._clock() + self.retry_interval
                results.append(RestoreResult(name, False, str(exc)))
                self._notify("mount.restore_failed", name, str(exc), mount_id)
        for r in results:
            event(
                log,
                "mount.restore",
                level=20 if r.ok else 40,
                mount=r.name,
                ok=r.ok,
                message=r.message,
            )
        return results

    # -- Test -------------------------------------------------------------------------

    def check(
        self,
        cfg: MountConfig,
        cred: CredentialData | None,
        mount_id: int | None = None,
    ) -> CheckReport:
        """Testet eine (ggf. noch nicht gespeicherte) Anbindung in drei Schritten."""
        if cfg.type is MountType.SFTP:
            report = self._check_sftp(cfg, cred)
        else:
            report = self._check_kernel(cfg, cred, mount_id)
        if mount_id is not None:
            self._store_check(mount_id, report, cfg)
        return report

    def _check_sftp(self, cfg: MountConfig, cred: CredentialData | None) -> CheckReport:
        try:
            scfg = sftp_config(cfg, cred)
        except (SftpError, ValidationError) as exc:
            report = CheckReport()
            report.step("connect", False, str(exc))
            return report
        return run_with_timeout(
            lambda: check_connection(scfg, timeout=self.probe_timeout),
            self.probe_timeout * 4,
            f"check:{cfg.name}",
        )

    def _check_kernel(
        self, cfg: MountConfig, cred: CredentialData | None, mount_id: int | None
    ) -> CheckReport:
        report = CheckReport()
        secrets = [cred.secret] if cred else []
        target: Path | None = None
        temp: Path | None = None

        if mount_id is not None:
            m, _ = self._load(mount_id)
            mp = self.mountpoint_for(m.slug)
            if self.backend.is_mounted(mp):
                target = mp
                report.step("mount", True, "bereits eingehängt")

        if target is None:
            self.mount_base.mkdir(mode=0o755, parents=True, exist_ok=True)
            temp = self.mount_base / f".test-{uuid.uuid4().hex[:12]}"
            temp.mkdir(mode=0o700)
            try:
                self.backend.mount(build_spec(cfg, temp, cred), timeout=self.mount_timeout)
            except (MountError, ValidationError) as exc:
                self._remove_mountpoint(temp)
                report.step("mount", False, _redact_output(str(exc), secrets))
                report.step("read", None)
                report.step("write", None)
                return report
            target = temp
            report.step("mount", True, "Einhängen erfolgreich")

        try:
            self._check_read_write(cfg, target, report)
        finally:
            if temp is not None:
                try:
                    self.backend.unmount(temp, timeout=self.mount_timeout)
                except MountError:
                    try:
                        self.backend.unmount(temp, lazy=True, timeout=self.mount_timeout)
                    except MountError as exc:
                        event(log, "check.unmount_failed", level=40, path=str(temp), error=str(exc))
                if not self.backend.is_mounted(temp):
                    self._remove_mountpoint(temp)
        return report

    def _check_read_write(self, cfg: MountConfig, target: Path, report: CheckReport) -> None:
        name = cfg.name
        try:
            count = run_with_timeout(
                lambda: len(os.listdir(target)), self.probe_timeout, f"read:{name}"
            )
            report.can_read = True
            report.step("read", True, f"{count} Einträge")
        except (OSError, OperationTimeout) as exc:
            report.step("read", False, str(exc))
            report.step("write", None)
            return

        def write_probe() -> tuple[OSError | None, int | None, int | None, OSError | None]:
            # Punkt am Anfang + .tmp: ein parallel laufender Mover fasst sie nie an.
            path = target / f".netmover-test-{uuid.uuid4().hex}.tmp"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    os.write(fd, b"netmover")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                return exc, None, None, None
            st = os.stat(path)
            try:
                path.unlink()
            except OSError as exc:
                return None, st.st_uid, st.st_gid, exc
            return None, st.st_uid, st.st_gid, None

        try:
            werr, uid, gid, derr = run_with_timeout(
                write_probe, self.probe_timeout, f"write:{name}"
            )
        except OperationTimeout as exc:
            report.step("write", False, str(exc))
            return
        result = assess_write(cfg.type, self._euid(), werr, uid, derr)
        report.can_write = result.write_ok
        report.can_delete = result.delete_ok
        report.root_squash = result.root_squash
        report.hints.extend(result.hints)
        if uid is not None:
            report.owner = f"{uid}:{gid}"
        report.step("write", result.write_ok, result.message)
        if cfg.type is MountType.CIFS and result.write_ok:
            report.hints.append(
                "CIFS: Eigentümer und Rechte bestimmen die Mount-Optionen uid/gid/file_mode – "
                "chown/chmod nach dem Verschieben werden übersprungen."
            )
        try:
            st = run_with_timeout(lambda: os.statvfs(target), self.probe_timeout, f"statvfs:{name}")
            report.total_bytes = st.f_blocks * st.f_frsize
            report.free_bytes = st.f_bavail * st.f_frsize
        except (OSError, OperationTimeout):
            pass

    def _store_check(self, mount_id: int, report: CheckReport, cfg: MountConfig) -> None:
        with session_scope(self.session_factory) as s:
            m = s.get(Mount, mount_id)
            if m is None:
                return
            m.last_test_at = utcnow()
            m.last_test_result = report.to_json()
            m.write_ok = report.can_write
            if report.root_squash is not None:
                m.root_squash = report.root_squash
            # SFTP: Hostschlüssel beim ersten erfolgreichen Test festhalten (TOFU).
            if cfg.type is MountType.SFTP and m.host_key is None and report.can_read:
                m.host_key = report.host_key
                event(log, "sftp.host_key_pinned", mount=m.name, fp=report.host_key_fingerprint)
            # SFTP hat keinen Mount-Zustand – ein erfolgreicher Test heißt „erreichbar“.
            if cfg.type is MountType.SFTP and report.ok and m.state is not MountState.MOUNTED:
                m.state = MountState.MOUNTED
                m.state_since = utcnow()
                m.last_error = None

    # -- Lebenszeichen und Wiederherstellung --------------------------------------

    def check_alive(self, mount_id: int) -> Path | None:
        """Vor jedem Lauf. Liefert den Mountpoint (None bei SFTP) oder wirft
        :class:`MountUnavailable`. Stellt gestörte Mounts selbst wieder her."""
        with self._lock(mount_id):
            m, cred = self._load(mount_id)
            if not m.enabled:
                raise MountUnavailable(f"Anbindung „{m.name}“ ist deaktiviert")
            if m.type is MountType.SFTP:
                self._check_alive_sftp(mount_id, m, cred)
                return None
            mp = self.mountpoint_for(m.slug)
            if not self.backend.is_mounted(mp):
                if mount_id in self._retry_at:
                    self._retry_mount(mount_id, m, cred, mp)
                    return mp
                msg = f"„{m.name}“ ist nicht eingehängt – Lauf abgebrochen, nichts angefasst."
                if m.state is not MountState.ERROR:
                    self._notify("mount.error", m.name, msg, mount_id)
                self._set_state(mount_id, MountState.ERROR, msg)
                raise MountUnavailable(msg)
            try:
                self._probe(m.name, mp)
            except OperationTimeout as exc:
                self._recover(mount_id, m, cred, f"hängt: {exc}")
            except OSError as exc:
                if exc.errno not in RECOVERABLE_ERRNOS:
                    msg = f"„{m.name}“: {exc.strerror} ({exc.filename})"
                    self._set_state(mount_id, MountState.ERROR, msg)
                    raise MountUnavailable(msg) from None
                self._recover(
                    mount_id, m, cred, f"{errno.errorcode.get(exc.errno or 0, '?')}: {exc.strerror}"
                )
            else:
                if m.state is not MountState.MOUNTED:
                    self._set_state(mount_id, MountState.MOUNTED)
            return mp

    def _check_alive_sftp(self, mount_id: int, m: Mount, cred: CredentialData | None) -> None:
        try:
            scfg = sftp_config(MountConfig.from_model(m), cred)

            def probe() -> None:
                with connect(scfg, timeout=self.probe_timeout) as sess:
                    sess.client.stat(scfg.base_path)

            run_with_timeout(probe, self.probe_timeout * 2, f"probe:{m.name}")
        except (SftpError, ValidationError, OSError, OperationTimeout) as exc:
            msg = f"„{m.name}“ nicht erreichbar: {exc}"
            self._set_state(mount_id, MountState.ERROR, msg)
            if m.state is not MountState.ERROR:
                self._notify("mount.error", m.name, msg, mount_id)
            raise MountUnavailable(msg) from None
        if m.state is MountState.ERROR:
            self._notify("mount.recovered", m.name, f"„{m.name}“ ist wieder erreichbar.", mount_id)
        if m.state is not MountState.MOUNTED:
            self._set_state(mount_id, MountState.MOUNTED)

    def _recover(self, mount_id: int, m: Mount, cred: CredentialData | None, reason: str) -> None:
        mp = self.mountpoint_for(m.slug)
        self._set_state(mount_id, MountState.DEGRADED, f"gestört ({reason}) – wird neu eingehängt")
        event(log, "mount.degraded", level=30, mount=m.name, reason=reason)
        last: str = reason
        for attempt, delay in enumerate(self.recover_backoff, start=1):
            if delay:
                self._sleep(delay)
            try:
                if self.backend.is_mounted(mp):
                    self.backend.unmount(mp, lazy=True, timeout=self.mount_timeout)
                self._prepare_mountpoint(mp)
                self.backend.mount(
                    build_spec(MountConfig.from_model(m), mp, cred), timeout=self.mount_timeout
                )
                self._probe(m.name, mp)
            except (MountError, OSError, OperationTimeout, ValidationError) as exc:
                last = str(exc)
                event(
                    log, "mount.recover_failed", level=30, mount=m.name, attempt=attempt, error=last
                )
                continue
            self._set_state(mount_id, MountState.MOUNTED, mounted=True)
            event(log, "mount.recovered", mount=m.name, attempt=attempt)
            return
        msg = (
            f"„{m.name}“ gestört ({reason}); Neu-Einhängen nach "
            f"{len(self.recover_backoff)} Versuchen gescheitert: {last}"
        )
        if m.auto_mount:
            self._retry_at[mount_id] = self._clock() + self.retry_interval
            msg += f"\n→ Neuer Versuch automatisch alle {self.retry_interval:g} s."
        self._set_state(mount_id, MountState.ERROR, msg)
        self._notify("mount.error", m.name, msg, mount_id)
        raise MountUnavailable(msg)

    def retry_pending(self) -> list[str]:
        """Fällige Neuversuche ausführen (Scheduler-Job, jede Minute) – auch für
        Anbindungen, die gerade keine Regel benutzt. Liefert die zurückgeholten Namen."""
        recovered: list[str] = []
        for mount_id in list(self._retry_at):
            if self._retry_at.get(mount_id, float("inf")) > self._clock():
                continue
            with self._lock(mount_id):
                if mount_id not in self._retry_at:
                    continue
                try:
                    m, cred = self._load(mount_id)
                except LookupError:
                    self._retry_at.pop(mount_id, None)
                    continue
                if not (m.enabled and m.auto_mount and m.type.is_kernel_mount):
                    self._retry_at.pop(mount_id, None)
                    continue
                mp = self.mountpoint_for(m.slug)
                if self.backend.is_mounted(mp):
                    self._retry_at.pop(mount_id, None)
                    continue
                try:
                    self._retry_mount(mount_id, m, cred, mp)
                    recovered.append(m.name)
                except MountUnavailable:
                    pass
        return recovered

    def _retry_mount(self, mount_id: int, m: Mount, cred: CredentialData | None, mp: Path) -> None:
        """Ein Versuch, einen von NetMover selbst ausgehängten Mount zurückzuholen –
        höchstens einmal pro ``retry_interval``, damit kein Lauf alle 10 s hängt."""
        wait = self._retry_at[mount_id] - self._clock()
        if wait > 0:
            raise MountUnavailable(
                f"„{m.name}“ nicht verfügbar – nächster Versuch, neu einzuhängen, "
                f"in {math.ceil(wait)} s. Lauf abgebrochen, nichts angefasst."
            )
        try:
            self._prepare_mountpoint(mp)
            self.backend.mount(
                build_spec(MountConfig.from_model(m), mp, cred), timeout=self.mount_timeout
            )
            self._probe(m.name, mp)
        except (MountError, OSError, OperationTimeout, ValidationError) as exc:
            if self.backend.is_mounted(mp):
                with contextlib.suppress(MountError):
                    self.backend.unmount(mp, lazy=True, timeout=self.mount_timeout)
            self._retry_at[mount_id] = self._clock() + self.retry_interval
            msg = (
                f"„{m.name}“ weiterhin nicht verfügbar: {exc}\n"
                f"→ Neuer Versuch in {self.retry_interval:g} s."
            )
            self._set_state(mount_id, MountState.ERROR, msg)
            event(log, "mount.retry_failed", level=30, mount=m.name, error=str(exc))
            raise MountUnavailable(msg) from None
        del self._retry_at[mount_id]
        self._set_state(mount_id, MountState.MOUNTED, mounted=True)
        event(log, "mount.recovered", mount=m.name, attempt="retry")
        self._notify("mount.recovered", m.name, f"„{m.name}“ ist wieder eingehängt.", mount_id)

    def _usage_target(self, mount_id: int) -> tuple[str, Path] | None:
        m, _ = self._load(mount_id)
        if not m.type.is_kernel_mount:
            return None
        mp = self.mountpoint_for(m.slug)
        return (m.name, mp) if self.backend.is_mounted(mp) else None

    def usage(self, mount_id: int, timeout: float | None = None) -> tuple[int, int] | None:
        """(gesamt, frei) in Bytes – nur bei eingehängten Kernel-Mounts."""
        target = self._usage_target(mount_id)
        if target is None:
            return None
        name, mp = target
        try:
            st = run_with_timeout(
                lambda: os.statvfs(mp), timeout or self.probe_timeout, f"statvfs:{name}"
            )
        except (OSError, OperationTimeout):
            return None
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize

    def cached_usage(
        self, mount_id: int, *, wait: float = 2.0, max_age: float = 60.0
    ) -> tuple[int, int] | None:
        """Füllstand für die Statusseite, ohne auf hängende Mounts zu warten.

        Ein Wert jünger als ``max_age`` kommt sofort aus dem Zwischenspeicher. Ein
        älterer wird im Hintergrund erneuert und bis dahin weiter gezeigt; nur ohne
        jeden Wert wird bis ``wait`` gewartet. Hängt die Abfrage länger als ``wait``,
        ist der Füllstand unbekannt (None). Solange sie hängt, startet keine weitere –
        ein hängender Mount bindet höchstens einen Thread.
        """
        now = self._clock()
        with self._usage_guard:
            cached = self._usage_cache.get(mount_id)
            if cached is not None and now - cached[0] < max_age:
                return cached[1]
            pending = self._usage_pending.get(mount_id)
            if pending is None:
                pending = (threading.Event(), now)
                self._usage_pending[mount_id] = pending
                threading.Thread(
                    target=self._refresh_usage,
                    args=(mount_id, pending[0]),
                    name=f"nm-usage:{mount_id}",
                    daemon=True,
                ).start()
        done, started = pending
        if cached is not None and now - started < wait:
            return cached[1]
        if cached is None and done.wait(max(0.0, wait - (now - started))):
            with self._usage_guard:
                fresh = self._usage_cache.get(mount_id)
            return fresh[1] if fresh is not None else None
        return None

    def _refresh_usage(self, mount_id: int, done: threading.Event) -> None:
        value: tuple[int, int] | None = None
        try:
            target = self._usage_target(mount_id)
            if target is not None:
                st = os.statvfs(target[1])  # darf hängen: läuft im eigenen Thread
                value = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        except (OSError, MountError, LookupError):
            value = None
        finally:
            with self._usage_guard:
                self._usage_cache[mount_id] = (self._clock(), value)
                self._usage_pending.pop(mount_id, None)
            done.set()

    def sftp_config_for(self, mount_id: int) -> SftpConfig:
        """Entschlüsselte Verbindungsdaten einer SFTP-Anbindung für einen Regellauf."""
        m, cred = self._load(mount_id)
        if m.type is not MountType.SFTP:
            raise SftpError(f"„{m.name}“ ist keine SFTP-Anbindung")
        return sftp_config(MountConfig.from_model(m), cred)
