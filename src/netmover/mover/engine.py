"""Ablauf eines Regellaufs – ohne Scheduler, ohne Datenbank-Protokoll.

Reihenfolge pro Lauf:

1. Ziel vorbereiten, Quelle scannen (Filter, versteckte/unfertige Dateien raus),
2. Kopier-Buchführung: bereits übertragene Dateiversionen still auslassen,
3. Größe und Mindestalter prüfen,
4. Stabilität: nach dem Stabilitätsfenster erneut messen – nur Dateien mit
   unveränderter Größe **und** mtime gehen weiter (Fallstrick 1),
5. pro Datei: ggf. Quarantäne, Kollisionsregel, Platzprüfung (Größe + 5 %,
   Fallstrick 14), atomare Übertragung, erst dann Quelle löschen.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from netmover.logging_setup import event, get_logger
from netmover.models import (
    CollisionPolicy,
    ItemStatus,
    PatternType,
    RuleAction,
    RunStatus,
)
from netmover.mover.destinations import (
    CollisionSkip,
    Destination,
    LocalDestination,
    MetaOptions,
    SourceChanged,
    TransferError,
)
from netmover.mover.ledger import FailureInfo, Ledger
from netmover.mover.naming import collision_candidates
from netmover.mover.scan import BUSY, Candidate, FileState, Matcher, current_state, scan
from netmover.timeouts import Cancelled, CancelToken

log = get_logger(__name__)

SPACE_RESERVE = 1.05
# Fehler, die auf eine gestörte Anbindung hindeuten: Lauf abbrechen statt jede
# weitere Datei scheitern zu lassen – der nächste Lauf prüft und hängt neu ein.
MOUNT_ERRNOS = frozenset({errno.ESTALE, errno.EIO, errno.ENOTCONN, errno.EHOSTDOWN})


class RunAborted(Exception):
    pass


@dataclass(frozen=True)
class RuleSnapshot:
    id: int
    name: str
    action: RuleAction = RuleAction.MOVE
    on_collision: CollisionPolicy = CollisionPolicy.RENAME
    pattern: str = "*"
    pattern_type: PatternType = PatternType.GLOB
    recursive: bool = False
    stability_seconds: int = 5
    min_age_seconds: int = 0
    max_size_mb: int | None = None
    set_owner: tuple[int, int] | None = None
    set_mode: int | None = None
    quarantine_after: int = 3
    dst_root_squash: bool | None = None  # nur NFS-Ziele: Ergebnis des Schreibtests


@dataclass
class ItemResult:
    src: str
    status: ItemStatus
    dst: str | None = None
    size: int | None = None
    message: str | None = None


@dataclass
class RunResult:
    status: RunStatus = RunStatus.OK
    dry_run: bool = False
    seen: int = 0
    moved: int = 0
    skipped: int = 0
    failed: int = 0
    message: str | None = None
    items: list[ItemResult] = field(default_factory=list)

    def add(self, item: ItemResult) -> None:
        self.items.append(item)
        if item.status in (ItemStatus.MOVED, ItemStatus.COPIED, ItemStatus.PLANNED):
            self.moved += 1
        elif item.status in (ItemStatus.SKIPPED, ItemStatus.QUARANTINED):
            self.skipped += 1
        else:
            self.failed += 1


def _describe_os_error(exc: OSError) -> str:
    where = f" ({exc.filename})" if exc.filename else ""
    return f"{exc.strerror or exc}{where}"


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


class Engine:
    def __init__(
        self,
        ledger: Ledger,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        warn_once: Callable[[str], bool] | None = None,
    ) -> None:
        self.ledger = ledger
        self._sleep = sleep
        self._clock = clock
        self._warned: set[str] = set()
        self._warn_once = warn_once or self._default_warn_once

    def _default_warn_once(self, key: str) -> bool:
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    # -- Lauf -------------------------------------------------------------------------

    def execute(
        self,
        rule: RuleSnapshot,
        src_root: Path,
        dest: Destination,
        token: CancelToken,
        *,
        dry_run: bool = False,
        quarantine_root: Path | None = None,
    ) -> RunResult:
        result = RunResult(dry_run=dry_run)
        try:
            self._execute(rule, src_root, dest, token, result, dry_run, quarantine_root)
        except Cancelled:
            result.status = RunStatus.ERROR
            result.message = "Lauf abgebrochen"
        except RunAborted as exc:
            result.status = RunStatus.ERROR
            result.message = str(exc)
        except TransferError as exc:
            result.status = RunStatus.ERROR
            result.message = str(exc)
        except OSError as exc:
            result.status = RunStatus.ERROR
            result.message = _describe_os_error(exc)
        else:
            if result.failed:
                result.status = RunStatus.PARTIAL
        return result

    def _execute(
        self,
        rule: RuleSnapshot,
        src_root: Path,
        dest: Destination,
        token: CancelToken,
        result: RunResult,
        dry_run: bool,
        quarantine_root: Path | None,
    ) -> None:
        if not src_root.is_dir():
            raise RunAborted(f"Quellordner {src_root} existiert nicht")
        if not dry_run:
            dest.prepare()
        token.check()

        exclude: set[Path] = set()
        if quarantine_root is not None:
            exclude.add(quarantine_root.resolve())
        if isinstance(dest, LocalDestination):
            exclude.add(dest.root.resolve())  # Ziel innerhalb der Quelle nie erneut scannen
        matcher = Matcher(rule.pattern, rule.pattern_type)
        candidates = list(scan(src_root, matcher, rule.recursive, frozenset(exclude)))
        result.seen = len(candidates)
        token.check()

        # Kopier-Buchführung (Fallstrick 11). Gilt auch für „verschieben“, falls
        # dort einmal die Quelle nicht gelöscht werden konnte.
        done = self.ledger.copied_keys(rule.id)
        if not dry_run:
            self.ledger.prune_copied(rule.id, {c.relpath for c in candidates})
        fresh = [c for c in candidates if (c.relpath, c.state.size, c.state.mtime_ns) not in done]
        unchanged = len(candidates) - len(fresh)

        pending: list[Candidate] = []
        now = self._clock()
        for c in fresh:
            if rule.max_size_mb is not None and c.state.size > rule.max_size_mb * 1024 * 1024:
                result.add(
                    ItemResult(
                        c.relpath,
                        ItemStatus.SKIPPED,
                        size=c.state.size,
                        message=f"größer als {rule.max_size_mb} MB",
                    )
                )
            elif rule.min_age_seconds and now - c.state.mtime_ns / 1e9 < rule.min_age_seconds:
                result.add(
                    ItemResult(
                        c.relpath,
                        ItemStatus.SKIPPED,
                        size=c.state.size,
                        message=f"jünger als {rule.min_age_seconds} s",
                    )
                )
            else:
                pending.append(c)

        # Stabilität (Fallstrick 1): zwei frische Messungen (öffnen + fstat, nicht
        # der Verzeichnis-Cache) im Abstand des Stabilitätsfensters.
        first: dict[str, FileState | None] = {c.relpath: current_state(c.path) for c in pending}
        if pending and rule.stability_seconds > 0:
            self._sleep(rule.stability_seconds)
            token.check()
        stable: list[Candidate] = []
        for c in pending:
            before, st = first[c.relpath], current_state(c.path)
            if before is None or st is None:
                continue  # inzwischen verschwunden – nichts zu tun
            if st != before or st == BUSY:
                result.add(
                    ItemResult(
                        c.relpath,
                        ItemStatus.SKIPPED,
                        size=max(st.size, 0),
                        message="wird noch geschrieben (Größe/Zeit geändert)"
                        if st != BUSY
                        else "Datei ist gerade in Benutzung",
                    )
                )
                continue
            # ab hier gilt der frisch gemessene Zustand (für Prüfung vor dem Umbenennen
            # und für die Kopier-Buchführung)
            stable.append(dataclasses.replace(c, state=st))

        failures = self.ledger.failures(rule.id)
        for c in stable:
            token.check()
            result.add(self._process(rule, c, dest, token, dry_run, quarantine_root, failures))

        if unchanged:
            result.message = f"{unchanged} bereits übertragen"

    # -- einzelne Datei ------------------------------------------------------------------

    def _meta(
        self, rule: RuleSnapshot, path: Path, dest: Destination
    ) -> tuple[MetaOptions, list[str]]:
        st = os.stat(path)
        notes: list[str] = []
        owner = rule.set_owner
        if owner is not None:
            if dest.kind == "cifs":
                owner = None
                if self._warn_once(f"{rule.id}:cifs-chown"):
                    notes.append(
                        "CIFS-Ziel: Eigentümer bestimmen die Mount-Optionen, chown übersprungen"
                    )
            elif dest.kind == "nfs" and rule.dst_root_squash is not False:
                owner = None
                if self._warn_once(f"{rule.id}:nfs-chown"):
                    reason = "Root-Squash aktiv" if rule.dst_root_squash else "Schreibtest fehlt"
                    notes.append(f"NFS-Ziel: chown übersprungen ({reason})")
        mode = rule.set_mode if rule.set_mode is not None else stat.S_IMODE(st.st_mode)
        return MetaOptions(st.st_atime_ns, st.st_mtime_ns, mode, owner), notes

    def _process(
        self,
        rule: RuleSnapshot,
        c: Candidate,
        dest: Destination,
        token: CancelToken,
        dry_run: bool,
        quarantine_root: Path | None,
        failures: dict[str, FailureInfo],
    ) -> ItemResult:
        size = c.state.size
        failure = failures.get(c.relpath)

        # Quarantäne für wiederholt scheiternde Dateien
        if (
            quarantine_root is not None
            and failure is not None
            and failure.attempts >= rule.quarantine_after
            and (failure.size, failure.mtime_ns) == (c.state.size, c.state.mtime_ns)
        ):
            if dry_run:
                return ItemResult(
                    c.relpath,
                    ItemStatus.PLANNED,
                    str(quarantine_root / c.relpath),
                    size,
                    "würde in Quarantäne verschoben",
                )
            return self._quarantine(rule, c, quarantine_root, failure.attempts)

        try:
            planned = dest.plan(c.relpath, rule.on_collision)
        except OSError as exc:
            return self._fail(rule, c, exc, dry_run)
        if planned is None:
            return ItemResult(
                c.relpath,
                ItemStatus.SKIPPED,
                dest.describe(c.relpath),
                size,
                "existiert bereits im Ziel",
            )
        if dry_run:
            msg = (
                "würde überschreiben"
                if (rule.on_collision is CollisionPolicy.OVERWRITE and dest.exists(planned))
                else None
            )
            verb = "kopieren" if rule.action is RuleAction.COPY else "verschieben"
            return ItemResult(
                c.relpath, ItemStatus.PLANNED, dest.describe(planned), size, msg or f"würde {verb}"
            )

        try:
            free = dest.free_bytes()
        except OSError:
            free = None
        if free is not None and free < size * SPACE_RESERVE:
            return ItemResult(
                c.relpath,
                ItemStatus.FAILED,
                dest.describe(planned),
                size,
                f"zu wenig Platz am Ziel (frei {_mb(free)}, "
                f"benötigt {_mb(int(size * SPACE_RESERVE))})",
            )

        def verify() -> None:
            if current_state(c.path) != c.state:
                raise SourceChanged("Quelle hat sich während der Übertragung geändert")

        try:
            meta, notes = self._meta(rule, c.path, dest)
            res = None
            if rule.action is RuleAction.MOVE:
                res = dest.move_same_device(c.path, c.relpath, rule.on_collision, meta)
            if res is None:
                res = dest.commit(c.path, size, c.relpath, rule.on_collision, meta, verify, token)
        except CollisionSkip:
            return ItemResult(
                c.relpath,
                ItemStatus.SKIPPED,
                dest.describe(c.relpath),
                size,
                "existiert bereits im Ziel",
            )
        except SourceChanged as exc:
            return ItemResult(c.relpath, ItemStatus.SKIPPED, None, size, str(exc))
        except (TransferError, OSError) as exc:
            return self._fail(rule, c, exc, dry_run)

        notes += res.notes
        target = dest.describe(res.final_rel)
        if c.relpath in failures:
            self.ledger.clear_failure(rule.id, c.relpath)

        if rule.action is RuleAction.COPY:
            self.ledger.record_copy(rule.id, c.relpath, size, c.state.mtime_ns, target)
            return ItemResult(c.relpath, ItemStatus.COPIED, target, size, "; ".join(notes) or None)

        if not res.source_consumed:
            try:
                os.unlink(c.path)
            except OSError as exc:
                # Ziel ist vollständig – Buchführung verhindert Duplikate im nächsten Lauf.
                self.ledger.record_copy(rule.id, c.relpath, size, c.state.mtime_ns, target)
                return ItemResult(
                    c.relpath,
                    ItemStatus.FAILED,
                    target,
                    size,
                    f"Ziel geschrieben, aber Quelle nicht löschbar: {_describe_os_error(exc)} – "
                    "Datei wird nicht erneut übertragen",
                )
        return ItemResult(c.relpath, ItemStatus.MOVED, target, size, "; ".join(notes) or None)

    def _fail(self, rule: RuleSnapshot, c: Candidate, exc: Exception, dry_run: bool) -> ItemResult:
        if isinstance(exc, OSError) and exc.errno in MOUNT_ERRNOS:
            raise RunAborted(f"Anbindung gestört: {_describe_os_error(exc)}") from exc
        msg = _describe_os_error(exc) if isinstance(exc, OSError) else str(exc)
        if not dry_run:
            attempts = self.ledger.record_failure(
                rule.id, c.relpath, c.state.size, c.state.mtime_ns, msg
            )
            msg = f"{msg} (Versuch {attempts})"
        event(log, "file.failed", level=30, rule=rule.name, file=c.relpath, error=msg)
        return ItemResult(c.relpath, ItemStatus.FAILED, None, c.state.size, msg)

    def _quarantine(
        self, rule: RuleSnapshot, c: Candidate, root: Path, attempts: int
    ) -> ItemResult:
        try:
            for cand in collision_candidates(c.relpath):
                target = root / cand
                if os.path.lexists(target):
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                os.rename(c.path, target)
                break
            else:
                raise TransferError("kein freier Name im Quarantäneordner")
        except (OSError, TransferError) as exc:
            msg = _describe_os_error(exc) if isinstance(exc, OSError) else str(exc)
            return ItemResult(
                c.relpath, ItemStatus.FAILED, None, c.state.size, f"Quarantäne gescheitert: {msg}"
            )
        self.ledger.clear_failure(rule.id, c.relpath)
        event(log, "file.quarantined", level=30, rule=rule.name, file=c.relpath)
        return ItemResult(
            c.relpath,
            ItemStatus.QUARANTINED,
            str(target),
            c.state.size,
            f"nach {attempts} Fehlversuchen in Quarantäne verschoben",
        )


__all__ = ["Engine", "FileState", "ItemResult", "RuleSnapshot", "RunAborted", "RunResult"]
