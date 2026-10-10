"""Scheduler: Jobs je Regel, Versatz, Neuabgleich, manuelle Läufe, Dienststart."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.app_state import NetMover
from netmover.config import Settings
from netmover.db import session_scope
from netmover.models import Mount, MountState, MountType, Rule, Run, RunStatus, RunTrigger, utcnow
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager
from netmover.mover.runner import RuleRunner
from netmover.runs import RunService
from netmover.scheduler import MAX_START_OFFSET, RuleScheduler
from tests.conftest import MountFactory


def arun(fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(fn())


@pytest.fixture
def rules(
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
    fake_backend: FakeMountBackend,
    manager: MountManager,
) -> list[int]:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    ids = []
    with session_scope(session_factory) as s:
        for name, interval, enabled in [("a", 30, True), ("b", 60, True), ("c", 30, False)]:
            r = Rule(
                name=name,
                src_mount_id=mid,
                dst_path=str(fake_backend.remote_root),
                interval_seconds=interval,
                enabled=enabled,
                stability_seconds=0,
            )
            s.add(r)
            s.flush()
            ids.append(r.id)
    return ids


@pytest.fixture
def scheduler(session_factory: sessionmaker[Session], manager: MountManager) -> RuleScheduler:
    runner = RuleRunner(session_factory, manager, sleep=lambda _: None)
    return RuleScheduler(session_factory, RunService(session_factory, runner), rng=random.Random(1))  # noqa: S311


def test_jobs_follow_rules(
    scheduler: RuleScheduler, rules: list[int], session_factory: sessionmaker[Session]
) -> None:
    a, b, c = rules

    async def main() -> None:
        scheduler.start()
        try:
            assert scheduler.next_run(a) is not None
            assert scheduler.next_run(b) is not None
            assert scheduler.next_run(c) is None
            # erster Termin mit kleinem Versatz
            for rid in (a, b):
                nxt = scheduler.next_run(rid)
                assert nxt is not None
                # Versatz ≤ 10 s plus Jitter ≤ 5 s
                assert nxt - utcnow() <= timedelta(seconds=MAX_START_OFFSET + 5 + 1)

            with session_scope(session_factory) as s:
                ra, rc = s.get(Rule, a), s.get(Rule, c)
                assert ra is not None and rc is not None
                ra.enabled = False
                rc.enabled = True
                rb = s.get(Rule, b)
                assert rb is not None
                rb.interval_seconds = 120
            scheduler.sync()
            assert scheduler.next_run(a) is None
            assert scheduler.next_run(c) is not None
            job = scheduler.scheduler.get_job(f"rule:{b}")
            assert job is not None and job.trigger.interval == timedelta(seconds=120)
        finally:
            scheduler.shutdown()

    arun(main)


def test_run_now_records_manual_run(
    scheduler: RuleScheduler, rules: list[int], session_factory: sessionmaker[Session]
) -> None:
    a = rules[0]

    async def main() -> None:
        scheduler.start()
        try:
            run_id = await scheduler.run_now(a, dry_run=True)
        finally:
            scheduler.shutdown()
        with session_factory() as s:
            run = s.get(Run, run_id)
            assert run is not None
            assert run.trigger is RunTrigger.MANUAL and run.dry_run and run.status is RunStatus.OK

    arun(main)


def test_scheduled_job_runs_in_thread_pool(
    scheduler: RuleScheduler, rules: list[int], session_factory: sessionmaker[Session]
) -> None:
    a = rules[0]

    async def main() -> None:
        scheduler.start()
        try:
            await scheduler._run_rule(a)
            assert scheduler.running and scheduler.last_heartbeat is not None
        finally:
            scheduler.shutdown()
        with session_factory() as s:
            runs = s.scalars(select(Run)).all()
            assert [(r.rule_id, r.trigger) for r in runs] == [(a, RunTrigger.SCHEDULE)]

    arun(main)


def test_app_startup_restores_mounts_before_scheduler(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "lib",
        run_dir=tmp_path / "run",
        mount_base=tmp_path / "mnt",
        mount_backend="fake",
    )
    backend = FakeMountBackend(tmp_path / "fake")
    backend.export("nas", "scans")
    app = NetMover(settings, backend=backend)
    with session_scope(app.session_factory) as s:
        s.add(
            Mount(
                name="scans",
                slug="scans",
                type=MountType.CIFS,
                server="nas",
                share="scans",
                mountpoint=str((tmp_path / "mnt" / "scans").resolve()),
            )
        )
    assert (tmp_path / "lib" / "secret.key").stat().st_mode & 0o777 == 0o600

    async def main() -> None:
        app.start()
        try:
            assert [r.ok for r in app.restore_results] == [True]
            assert app.scheduler.running
            assert app.health().ok
        finally:
            app.stop()

    arun(main)
    # Beim Beenden NICHT aushängen
    assert backend.is_mounted((tmp_path / "mnt").resolve() / "scans")
    with app.session_factory() as s:
        m = s.scalars(select(Mount)).one()
        assert m.state is MountState.MOUNTED
