"""Zeitsteuerung mit APScheduler (``AsyncIOScheduler``).

* Ein Job pro aktiver Regel im jeweiligen Intervall. Erster Start mit kleinem
  zufälligem Versatz, dazu Jitter pro Termin – damit nicht alle Regeln
  gleichzeitig starten (Fallstrick 8).
* Die Läufe selbst sind blockierend und laufen in einem eigenen Thread-Pool,
  nie im Event-Loop. Überlappung regelt der Lock im :class:`RuleRunner`, der den
  übersprungenen Termin auch protokolliert – deshalb ``max_instances`` > 1.
* Herzschlag-Job für ``/healthz``, stündliches Aufräumen der Protokolle und alle
  5 Minuten die Frage, ob die tägliche Datensicherung fällig ist.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.logging_setup import event, get_logger
from netmover.models import Rule, RunTrigger, utcnow
from netmover.runs import RunService, cleanup

log = get_logger(__name__)

MAX_START_OFFSET = 10.0
HEARTBEAT_SECONDS = 30
CLEANUP_SECONDS = 3600
RETRY_SECONDS = 60
PUSH_SECONDS = 60
BACKUP_CHECK_SECONDS = 300


def _job_id(rule_id: int) -> str:
    return f"rule:{rule_id}"


class RuleScheduler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        service: RunService,
        *,
        retry_mounts: Callable[[], object] | None = None,
        push_health: Callable[[], object] | None = None,
        backup_tick: Callable[[], object] | None = None,
        rng: random.Random | None = None,
        workers: int = 8,
    ) -> None:
        self.session_factory = session_factory
        self.service = service
        self._retry_mounts = retry_mounts
        self._push_health = push_health
        self._backup_tick = backup_tick
        self._rng = rng or random.Random()  # noqa: S311 – nur Startversatz
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nm-run")
        self.scheduler = AsyncIOScheduler(
            timezone=UTC,
            job_defaults={"coalesce": True, "max_instances": 3},
        )
        self._intervals: dict[int, int] = {}
        self.last_heartbeat: datetime | None = None

    # -- Lebenszyklus ---------------------------------------------------------------

    def start(self) -> None:
        self.scheduler.start()
        self.heartbeat()
        self.scheduler.add_job(
            self.heartbeat, IntervalTrigger(seconds=HEARTBEAT_SECONDS), id="heartbeat"
        )
        self.scheduler.add_job(
            self._cleanup,
            IntervalTrigger(seconds=CLEANUP_SECONDS, start_date=utcnow() + timedelta(minutes=1)),
            id="cleanup",
        )
        if self._retry_mounts is not None:
            self.scheduler.add_job(
                self._retry, IntervalTrigger(seconds=RETRY_SECONDS), id="mount-retry"
            )
        if self._push_health is not None:
            self.scheduler.add_job(
                self._push, IntervalTrigger(seconds=PUSH_SECONDS), id="kuma-push"
            )
        if self._backup_tick is not None:
            self.scheduler.add_job(
                self._backup,
                IntervalTrigger(
                    seconds=BACKUP_CHECK_SECONDS, start_date=utcnow() + timedelta(minutes=2)
                ),
                id="backup",
            )
        self.sync()
        event(log, "scheduler.started", rules=len(self._intervals))

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        # Laufende Worker nicht abwarten: abgebrochene Übertragungen hinterlassen
        # höchstens .part-Dateien, die Quelle bleibt unangetastet.
        self._executor.shutdown(wait=False, cancel_futures=True)
        event(log, "scheduler.stopped")

    @property
    def running(self) -> bool:
        return bool(self.scheduler.running)

    def heartbeat(self) -> None:
        self.last_heartbeat = utcnow()

    # -- Regeln -------------------------------------------------------------------------

    def sync(self) -> None:
        """Jobs an die Regeln in der Datenbank angleichen (nach jeder Änderung)."""
        with session_scope(self.session_factory) as s:
            wanted = {
                rid: interval
                for rid, interval in s.execute(
                    select(Rule.id, Rule.interval_seconds).where(Rule.enabled.is_(True))
                ).all()
            }
        for rid in list(self._intervals):
            if rid not in wanted:
                if self.scheduler.get_job(_job_id(rid)):
                    self.scheduler.remove_job(_job_id(rid))
                del self._intervals[rid]
        for rid, interval in wanted.items():
            if self._intervals.get(rid) == interval and self.scheduler.get_job(_job_id(rid)):
                continue
            offset = self._rng.uniform(0, min(float(interval), MAX_START_OFFSET))
            trigger = IntervalTrigger(
                seconds=interval,
                start_date=utcnow() + timedelta(seconds=offset),
                jitter=max(1, min(5, interval // 10)),
            )
            self.scheduler.add_job(
                self._run_rule,
                trigger,
                args=[rid],
                id=_job_id(rid),
                name=f"Regel {rid}",
                replace_existing=True,
                misfire_grace_time=interval,
            )
            self._intervals[rid] = interval

    def next_run(self, rule_id: int) -> datetime | None:
        job = self.scheduler.get_job(_job_id(rule_id))
        return job.next_run_time if job is not None else None

    async def _run_rule(self, rule_id: int) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor, self.service.execute, rule_id, RunTrigger.SCHEDULE
        )

    async def run_now(self, rule_id: int, dry_run: bool | None = None) -> int | None:
        """„Jetzt ausführen“ / „Probelauf“ aus der Oberfläche."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self.service.execute, rule_id, RunTrigger.MANUAL, dry_run
        )

    async def _retry(self) -> None:
        if self._retry_mounts is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._retry_mounts)

    async def _push(self) -> None:
        if self._push_health is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._push_health)

    async def _backup(self) -> None:
        if self._backup_tick is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._backup_tick)

    async def _cleanup(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, cleanup, self.session_factory)
