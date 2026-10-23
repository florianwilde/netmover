"""Läufe ausführen und protokollieren.

Pro Lauf eine Zeile in ``runs`` (auch für übersprungene Termine, Fallstrick 8),
die Einzeldateien in ``run_items``. Danach die Regelzähler:

* ``last_run_at`` – jeder Lauf außer übersprungenen,
* ``last_success_at`` – Status ``ok`` oder ``partial`` (das Werkzeug hat gearbeitet),
* ``consecutive_failures`` – ``error`` und ``partial`` zählen hoch, ``ok`` setzt zurück.

Ein manuell gestarteter Probelauf verändert keine Zähler.

Automatisch pausieren (0.4.0): Ist Quelle oder Ziel nicht erreichbar, wechselt die
Regel in „wartet auf Anbindung“. Protokolliert wird nur der Beginn, nicht jeder
weitere Termin, und „Fehler in Folge“ zählt nicht hoch – die Benachrichtigung
kommt einmal pro Anbindung vom MountManager. Sobald ein Lauf wieder durchkommt,
endet das Warten von selbst, und der Lauf vermerkt die Wartezeit.

Wartungsmodus: geplante Läufe werden gar nicht erst ausgeführt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.logging_setup import event, get_logger
from netmover.models import FileFailure, Rule, Run, RunItem, RunStatus, RunTrigger, utcnow
from netmover.mover.engine import RunResult
from netmover.mover.runner import RuleRunner
from netmover.notify import Notifier
from netmover.settings_store import load_settings

log = get_logger(__name__)

MAX_ITEMS_PER_RUN = 2000
SUCCESS = frozenset({RunStatus.OK, RunStatus.PARTIAL})
FAILURE = frozenset({RunStatus.ERROR, RunStatus.PARTIAL})


class RunService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        runner: RuleRunner,
        notifier: Notifier | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.runner = runner
        self.notifier = notifier

    def execute(
        self,
        rule_id: int,
        trigger: RunTrigger = RunTrigger.SCHEDULE,
        dry_run: bool | None = None,
    ) -> int | None:
        """Führt die Regel aus, protokolliert den Lauf und liefert die Run-ID
        (None, wenn die Regel inzwischen gelöscht wurde)."""
        if trigger is RunTrigger.SCHEDULE and load_settings(self.session_factory).maintenance:
            return None
        started = utcnow()
        result = self.runner.run(rule_id, dry_run=dry_run)
        finished = utcnow()
        count_it = not (trigger is RunTrigger.MANUAL and dry_run)
        return self.record(rule_id, trigger, result, started, finished, update_counters=count_it)

    def record(
        self,
        rule_id: int,
        trigger: RunTrigger,
        result: RunResult,
        started: datetime,
        finished: datetime,
        *,
        update_counters: bool = True,
    ) -> int | None:
        notify: tuple[str, bool, int, str] | None = None
        with session_scope(self.session_factory) as s:
            rule = s.get(Rule, rule_id)
            if rule is None:
                return None
            message = result.message
            if update_counters and result.unavailable:
                if rule.waiting_since is not None:
                    # wartet bereits – nur den Grund aktualisieren, kein neuer Eintrag
                    rule.waiting_reason = result.message
                    event(log, "rule.still_waiting", level=10, rule=rule.name)
                    return None
                rule.waiting_since = finished
                rule.waiting_reason = result.message
                rule.last_run_at = finished
                message = f"Pausiert, bis die Anbindung zurück ist: {result.message}"
                update_counters = False  # „Fehler in Folge“ nicht hochzählen
                event(log, "rule.waiting", level=30, rule=rule.name, reason=result.message or "")
            elif update_counters and result.waiting_on:
                # Ein Ziel fehlt, die übrigen wurden beliefert: Lauf normal
                # protokollieren, Regel aber als wartend kennzeichnen (Statusseite, Kuma).
                if rule.waiting_since is None:
                    rule.waiting_since = finished
                    event(log, "rule.waiting", level=30, rule=rule.name, reason=message or "")
                rule.waiting_reason = "; ".join(result.waiting_on)
            elif (
                update_counters
                and rule.waiting_since is not None
                and result.status is not RunStatus.SKIPPED
            ):
                waited = _human(finished - rule.waiting_since)
                rule.waiting_since = None
                rule.waiting_reason = None
                note = f"Fortgesetzt nach {waited} Wartezeit"
                message = f"{note}; {message}" if message else note
                event(log, "rule.resumed", rule=rule.name, waited=waited)
            items = result.items
            if len(items) > MAX_ITEMS_PER_RUN:
                extra = len(items) - MAX_ITEMS_PER_RUN
                items = items[:MAX_ITEMS_PER_RUN]
                note = f"{extra} weitere Dateien nicht einzeln protokolliert"
                message = f"{message}; {note}" if message else note
            run = Run(
                rule_id=rule_id,
                started_at=started,
                finished_at=finished,
                status=result.status,
                trigger=trigger,
                dry_run=result.dry_run,
                seen=result.seen,
                moved=result.moved,
                skipped=result.skipped,
                failed=result.failed,
                message=message,
            )
            s.add(run)
            s.flush()
            s.add_all(
                RunItem(
                    run_id=run.id,
                    src=i.src,
                    dst=i.dst,
                    status=i.status,
                    size=i.size,
                    message=i.message,
                )
                for i in items
            )
            if update_counters and result.status is not RunStatus.SKIPPED:
                previous = rule.consecutive_failures
                rule.last_run_at = finished
                if result.status in SUCCESS:
                    rule.last_success_at = finished
                if result.status in FAILURE:
                    rule.consecutive_failures += 1
                    notify = (rule.name, True, rule.consecutive_failures, self._summary(result))
                else:
                    rule.consecutive_failures = 0
                    if previous:
                        notify = (rule.name, False, previous, "")
            run_id = run.id
            rule_name = rule.name

        level = 20 if result.status in (RunStatus.OK, RunStatus.SKIPPED) else 30
        if result.status is RunStatus.OK and not result.items:
            level = 10  # leere Läufe alle 30 s nur auf DEBUG
        event(
            log,
            "run.finished",
            level=level,
            rule=rule_name,
            run=run_id,
            status=result.status.value,
            trigger=trigger.value,
            dry_run=result.dry_run,
            seen=result.seen,
            moved=result.moved,
            skipped=result.skipped,
            failed=result.failed,
            took_ms=int((finished - started).total_seconds() * 1000),
            message=result.message or "",
        )
        if notify is not None and self.notifier is not None:
            self.notifier.rule_result(*notify)
        return run_id

    @staticmethod
    def _summary(result: RunResult) -> str:
        if result.status is RunStatus.ERROR:
            return result.message or "Lauf fehlgeschlagen"
        failed = [i for i in result.items if i.status.value == "failed"][:5]
        lines = [f"{result.failed} Datei(en) fehlgeschlagen:"]
        lines += [f"• {i.src}: {i.message}" for i in failed]
        return "\n".join(lines)


def _human(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h".replace(".", ",")


def cleanup(session_factory: sessionmaker[Session], now: datetime | None = None) -> dict[str, int]:
    """Protokolle älter als die Aufbewahrungsfrist löschen (Items per CASCADE),
    dazu Fehlerzähler, die seitdem nicht mehr angefasst wurden."""
    days = load_settings(session_factory).retention_days
    cutoff = (now or utcnow()) - timedelta(days=days)
    with session_scope(session_factory) as s:
        old_ids = list(s.scalars(select(Run.id).where(Run.started_at < cutoff)))
        for i in range(0, len(old_ids), 500):
            chunk = old_ids[i : i + 500]
            s.execute(delete(RunItem).where(RunItem.run_id.in_(chunk)))
            s.execute(delete(Run).where(Run.id.in_(chunk)))
        failures = s.execute(delete(FileFailure).where(FileFailure.last_attempt_at < cutoff))
        removed_failures = getattr(failures, "rowcount", 0) or 0
    stats = {"runs": len(old_ids), "file_failures": removed_failures}
    event(log, "cleanup.done", retention_days=days, **stats)
    return stats
