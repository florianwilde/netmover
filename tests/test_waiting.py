"""0.4.0: automatisch pausieren/fortsetzen, Wartungsmodus, eine Meldung pro Anbindung."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.health import check_health
from netmover.models import Rule, Run, RunStatus, RunTrigger, utcnow
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager
from netmover.mover.engine import RunResult
from netmover.mover.runner import RuleRunner
from netmover.runs import RunService
from netmover.settings_store import AppSettings, save_settings
from tests.conftest import MountFactory


class FakeRunner:
    def __init__(self) -> None:
        self.results: list[RunResult] = []
        self.calls = 0

    def run(self, rule_id: int, *, dry_run: bool | None = None) -> RunResult:
        self.calls += 1
        return self.results.pop(0)


@pytest.fixture
def rule_id(make_mount: MountFactory, session_factory: sessionmaker[Session]) -> int:
    mid = make_mount()
    with session_scope(session_factory) as s:
        r = Rule(name="Scans", src_mount_id=mid, dst_path="/x", interval_seconds=30)
        s.add(r)
        s.flush()
        return r.id


def _rule(sf: sessionmaker[Session], rid: int) -> Rule:
    with sf() as s:
        r = s.get(Rule, rid)
        assert r is not None
        s.expunge(r)
        return r


def _runs(sf: sessionmaker[Session]) -> list[Run]:
    with sf() as s:
        return list(s.scalars(select(Run).order_by(Run.id)))


def unavailable() -> RunResult:
    return RunResult(
        RunStatus.ERROR,
        message="„NAS“ ist nicht eingehängt – Lauf abgebrochen, nichts angefasst.",
        unavailable=True,
    )


def test_waiting_logs_once_and_resumes_by_itself(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    runner = FakeRunner()
    service = RunService(session_factory, runner)  # type: ignore[arg-type]
    runner.results += [unavailable(), unavailable(), unavailable(), RunResult(RunStatus.OK)]

    first = service.execute(rule_id)
    assert first is not None
    r = _rule(session_factory, rule_id)
    assert r.waiting_since is not None and "nicht eingehängt" in (r.waiting_reason or "")
    assert r.consecutive_failures == 0  # Warten ist kein Fehlschlag

    assert service.execute(rule_id) is None  # kein neuer Eintrag alle 30 s
    assert service.execute(rule_id) is None
    assert len(_runs(session_factory)) == 1
    assert _runs(session_factory)[0].message.startswith("Pausiert, bis die Anbindung zurück ist")

    service.execute(rule_id)  # Anbindung wieder da
    r = _rule(session_factory, rule_id)
    assert r.waiting_since is None and r.waiting_reason is None and r.last_success_at is not None
    runs = _runs(session_factory)
    assert len(runs) == 2 and runs[1].message and runs[1].message.startswith("Fortgesetzt nach")
    assert r.consecutive_failures == 0


def test_real_errors_still_count(session_factory: sessionmaker[Session], rule_id: int) -> None:
    runner = FakeRunner()
    service = RunService(session_factory, runner)  # type: ignore[arg-type]
    runner.results += [RunResult(RunStatus.ERROR, message="kaputt")] * 2
    service.execute(rule_id)
    service.execute(rule_id)
    r = _rule(session_factory, rule_id)
    assert r.consecutive_failures == 2 and r.waiting_since is None
    assert len(_runs(session_factory)) == 2


def test_manual_dry_run_does_not_start_waiting(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    runner = FakeRunner()
    service = RunService(session_factory, runner)  # type: ignore[arg-type]
    runner.results.append(unavailable())
    service.execute(rule_id, RunTrigger.MANUAL, dry_run=True)
    assert _rule(session_factory, rule_id).waiting_since is None
    assert len(_runs(session_factory)) == 1  # Probelauf sieht man trotzdem


def test_maintenance_skips_scheduled_runs_but_not_manual(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    runner = FakeRunner()
    service = RunService(session_factory, runner)  # type: ignore[arg-type]
    save_settings(session_factory, AppSettings(maintenance=True))
    assert service.execute(rule_id) is None
    assert runner.calls == 0 and _runs(session_factory) == []
    runner.results.append(RunResult(RunStatus.OK))
    assert service.execute(rule_id, RunTrigger.MANUAL) is not None
    assert runner.calls == 1


def _health(sf: sessionmaker[Session], **kw: object):  # type: ignore[no-untyped-def]
    now = utcnow()
    return check_health(
        sf,
        scheduler_running=True,
        last_heartbeat=now,
        service_started=now - timedelta(hours=1),
        now=now,
        **kw,
    )  # type: ignore[arg-type]


def test_health_waiting_and_maintenance(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    with session_scope(session_factory) as s:
        r = s.get(Rule, rule_id)
        assert r is not None
        r.created_at = utcnow() - timedelta(days=1)
        r.last_success_at = utcnow() - timedelta(hours=2)
        r.waiting_since = utcnow() - timedelta(minutes=12)
        r.waiting_reason = "„NAS“ nicht verfügbar"
    h = _health(session_factory)
    assert not h.ok and "wartet seit" in h.problems[0] and "NAS" in h.problems[0]
    h = _health(session_factory, maintenance=True)
    assert h.ok and h.maintenance and h.stale_rules == set()
    check = h.find("rule", id=rule_id)
    assert check is not None and "Wartungsmodus" in check.message


# -- Integration mit MountManager und Runner ------------------------------------------------


def test_vanished_mount_notifies_once_with_affected_rules(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
    notifications: list[tuple[str, str, str]],
    tmp_path: Path,
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount("nas", share="scans")
    manager.mount(mid)
    with session_scope(session_factory) as s:
        s.add(Rule(name="Scans → Paperless", src_mount_id=mid, dst_path=str(tmp_path)))
        s.add(Rule(name="Archiv", src_mount_id=mid, dst_path=str(tmp_path), enabled=False))
    fake_backend.vanish(manager.mountpoint_for("nas"))

    runner = RuleRunner(session_factory, manager, sleep=lambda _: None)
    with session_factory() as s:
        rid = s.scalars(select(Rule.id).where(Rule.name == "Scans → Paperless")).one()
    for _ in range(3):
        result = runner.run(rid)
        assert result.unavailable and result.status is RunStatus.ERROR
    errors = [n for n in notifications if n[0] == "mount.error"]
    assert len(errors) == 1
    assert "Betroffene Regeln" in errors[0][2] and "Scans → Paperless" in errors[0][2]
    assert "Archiv" not in errors[0][2]  # deaktivierte Regel nicht erwähnt
