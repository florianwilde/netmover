"""Einen Regellauf sicher ausführen.

* Fallstrick 8: ein Lock pro Regel; läuft sie noch, wird der Termin übersprungen.
* Fallstrick 2: vor jedem Lauf prüft der :class:`MountManager`, dass Quelle und
  Ziel wirklich eingehängt und lebendig sind – sonst wird nichts angefasst.
* Fallstrick 13: die Dateizugriffe laufen in einem eigenen Worker mit
  Inaktivitäts-Zeitlimit. Der Lock wird erst freigegeben, wenn der Worker
  wirklich endet – ein hängender Lauf kann also nie parallel zu einem neuen
  dieselben Dateien bearbeiten; neue Termine werden bis dahin übersprungen.
"""

from __future__ import annotations

import posixpath
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.logging_setup import get_logger
from netmover.models import MountType, Rule, RunStatus
from netmover.mounting.manager import MountManager, MountUnavailable
from netmover.mounting.sftp import SftpError, connect
from netmover.mover.destinations import (
    Destination,
    LocalDestination,
    SftpDestination,
    TransferError,
)
from netmover.mover.engine import Engine, RuleSnapshot, RunResult
from netmover.mover.ledger import Ledger
from netmover.mover.sources import LocalSource, SftpSource, Source
from netmover.timeouts import CancelToken, OperationTimeout, run_with_watchdog
from netmover.validation import ValidationError, safe_abspath, safe_relpath

log = get_logger(__name__)


@dataclass(frozen=True)
class RulePlan:
    snapshot: RuleSnapshot
    name: str
    src_mount_id: int
    src_type: MountType
    src_name: str
    src_subpath: str
    dst_mount_id: int | None
    dst_type: MountType | None
    dst_name: str | None
    dst_path: str
    quarantine_path: str | None
    op_timeout: int
    dry_run: bool


def parse_owner(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    uid, _, gid = value.partition(":")
    try:
        return int(uid), int(gid or uid)
    except ValueError:
        raise ValidationError("set_owner", "Eigentümer bitte als „uid:gid“ angeben") from None


def parse_mode(value: str | None) -> int | None:
    return int(value, 8) if value else None


def load_plan(session: Session, rule_id: int) -> RulePlan:
    r = session.get(Rule, rule_id)
    if r is None:
        raise LookupError(f"Regel {rule_id} existiert nicht")
    dst = r.dst_mount
    snap = RuleSnapshot(
        id=r.id,
        name=r.name,
        action=r.action,
        on_collision=r.on_collision,
        pattern=r.pattern,
        pattern_type=r.pattern_type,
        recursive=r.recursive,
        stability_seconds=r.stability_seconds,
        min_age_seconds=r.min_age_seconds,
        max_size_mb=r.max_size_mb,
        set_owner=parse_owner(r.set_owner),
        set_mode=parse_mode(r.set_mode),
        quarantine_after=r.quarantine_after,
        dst_root_squash=dst.root_squash if dst is not None else None,
    )
    return RulePlan(
        snapshot=snap,
        name=r.name,
        src_mount_id=r.src_mount_id,
        src_type=r.src_mount.type,
        src_name=r.src_mount.name,
        src_subpath=safe_relpath("src_subpath", r.src_subpath),
        dst_mount_id=r.dst_mount_id,
        dst_type=dst.type if dst is not None else None,
        dst_name=dst.name if dst is not None else None,
        dst_path=r.dst_path,
        quarantine_path=safe_relpath("quarantine_path", r.quarantine_path)
        if r.quarantine_path
        else None,
        op_timeout=r.op_timeout_seconds,
        dry_run=r.dry_run,
    )


class RuleRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        mounts: MountManager,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_factory = session_factory
        self.mounts = mounts
        self.engine = Engine(Ledger(session_factory), sleep=sleep, clock=clock)
        self._clock = clock
        self._locks: dict[int, threading.Lock] = {}
        self._started: dict[int, float] = {}
        self._guard = threading.Lock()

    def _lock(self, rule_id: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(rule_id, threading.Lock())

    def running_since(self, rule_id: int) -> datetime | None:
        ts = self._started.get(rule_id)
        return datetime.fromtimestamp(ts, UTC) if ts is not None else None

    def run(self, rule_id: int, *, dry_run: bool | None = None) -> RunResult:
        lock = self._lock(rule_id)
        if not lock.acquire(blocking=False):
            since = self.running_since(rule_id)
            when = f" (seit {since.astimezone():%H:%M:%S})" if since else ""
            return RunResult(
                status=RunStatus.SKIPPED,
                dry_run=bool(dry_run),
                message=f"Termin übersprungen – voriger Lauf läuft noch{when}",
            )
        # Wird True, sobald der Worker-Thread den Lock übernommen hat; ab dann gibt
        # ihn ausschließlich der Worker frei (on_finish), auch nach einem Zeitlimit.
        handed_over = [False]
        try:
            with session_scope(self.session_factory) as s:
                plan = load_plan(s, rule_id)
            dry = plan.dry_run if dry_run is None else dry_run
            return self._run_locked(plan, dry, lock, rule_id, handed_over)
        except (ValidationError, LookupError) as exc:
            return RunResult(status=RunStatus.ERROR, dry_run=bool(dry_run), message=str(exc))
        finally:
            if not handed_over[0]:
                lock.release()

    def _run_locked(
        self,
        plan: RulePlan,
        dry: bool,
        lock: threading.Lock,
        rule_id: int,
        handed_over: list[bool],
    ) -> RunResult:
        try:
            src_mp = self.mounts.check_alive(plan.src_mount_id)
            dst_mp = (
                self.mounts.check_alive(plan.dst_mount_id)
                if plan.dst_mount_id is not None
                else None
            )
        except MountUnavailable as exc:
            return RunResult(RunStatus.ERROR, dry, message=str(exc), unavailable=True)

        try:
            make_source = self._source_factory(plan, src_mp)
            src_root = src_mp / plan.src_subpath if src_mp and plan.src_subpath else src_mp
            make_dest = self._dest_factory(plan, dst_mp, src_root)
        except (ValidationError, TransferError, SftpError) as exc:
            return RunResult(RunStatus.ERROR, dry, message=str(exc))

        def work(token: CancelToken) -> RunResult:
            source = make_source()
            try:
                dest = make_dest()
            except BaseException:
                source.close()
                raise
            try:
                return self.engine.execute(
                    plan.snapshot,
                    source,
                    dest,
                    token,
                    dry_run=dry,
                    quarantine_rel=plan.quarantine_path,
                )
            finally:
                dest.close()
                source.close()

        def finish() -> None:
            self._started.pop(rule_id, None)
            lock.release()

        self._started[rule_id] = self._clock()
        handed_over[0] = True
        try:
            return run_with_watchdog(work, plan.op_timeout, f"rule:{plan.name}", on_finish=finish)
        except OperationTimeout:
            return RunResult(
                RunStatus.ERROR,
                dry,
                message=(
                    f"Keine Reaktion seit {plan.op_timeout} s – Lauf abgebrochen. Vermutlich hängt "
                    "eine Anbindung; sie wird vor dem nächsten Lauf geprüft und ggf. neu "
                    "eingehängt."
                ),
            )
        except (TransferError, SftpError, OSError) as exc:
            return RunResult(RunStatus.ERROR, dry, message=str(exc))

    def _under_mount_base(self, path: Path) -> bool:
        # Wörtlich und aufgelöst prüfen – ein Symlink darf die Prüfung nicht umgehen.
        bases = {self.mounts.mount_base.absolute(), self.mounts.mount_base.resolve()}
        paths = {path.absolute(), path.resolve()}
        return any(p == b or b in p.parents for p in paths for b in bases)

    def _source_factory(self, plan: RulePlan, src_mp: Path | None) -> Callable[[], Source]:
        if plan.src_type is MountType.SFTP:
            scfg = self.mounts.sftp_config_for(plan.src_mount_id)
            label = plan.src_name or "sftp"

            def make_sftp() -> Source:
                return SftpSource(connect(scfg), scfg.base_path, plan.src_subpath, label=label)

            return make_sftp
        if src_mp is None:
            raise TransferError("Quell-Anbindung ist nicht eingehängt")
        root = src_mp

        def make_local() -> Source:
            return LocalSource(root, plan.src_subpath)

        return make_local

    def _dest_factory(
        self, plan: RulePlan, dst_mp: Path | None, src_root: Path | None
    ) -> Callable[[], Destination]:
        if plan.dst_mount_id is None:
            root = Path(safe_abspath("dst_path", plan.dst_path))
            if self._under_mount_base(root):
                base = self.mounts.mount_base
                raise ValidationError(
                    "dst_path",
                    f"Lokales Ziel liegt unter {base} – bitte die Anbindung als Ziel wählen, "
                    "damit geprüft wird, ob sie eingehängt ist.",
                )
            kind = "local"
            local_root = root
            create = False
        elif plan.dst_type is MountType.SFTP:
            scfg = self.mounts.sftp_config_for(plan.dst_mount_id)
            sub = safe_relpath("dst_path", plan.dst_path)
            remote = posixpath.join(scfg.base_path, sub) if sub else scfg.base_path
            label = plan.dst_name or "sftp"

            def make_sftp() -> Destination:
                return SftpDestination(connect(scfg), remote, label=label)

            return make_sftp
        else:
            if dst_mp is None:
                raise TransferError("Ziel-Anbindung ist nicht eingehängt")
            sub = safe_relpath("dst_path", plan.dst_path)
            local_root = dst_mp / sub if sub else dst_mp
            kind = "cifs" if plan.dst_type is MountType.CIFS else "nfs"
            create = True

        if src_root is not None and local_root.resolve() == src_root.resolve():
            raise ValidationError("dst_path", "Quelle und Ziel sind derselbe Ordner")

        def make_local() -> Destination:
            return LocalDestination(local_root, kind, create_root=create)  # type: ignore[arg-type]

        return make_local
