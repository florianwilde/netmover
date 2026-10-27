"""Protokollierung, Regelzähler, Benachrichtigung, Aufräumen, Health (Fallstrick 9)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.health import check_health
from netmover.models import (
    FileFailure,
    ItemStatus,
    Mount,
    MountState,
    Rule,
    Run,
    RunItem,
    RunStatus,
    RunTrigger,
    utcnow,
)
from netmover.mover.engine import ItemResult, RunResult
from netmover.mover.runner import RuleRunner
from netmover.notify import Notification, Notifier, render
from netmover.runs import RunService, cleanup
from netmover.settings_store import AppSettings, NotifyMode, load_settings, save_settings
from tests.conftest import MountFactory


class FakeRunner:
    def __init__(self) -> None:
        self.results: list[RunResult] = []

    def run(self, rule_id: int, *, dry_run: bool | None = None) -> RunResult:
        return self.results.pop(0)


@pytest.fixture
def rule_id(make_mount: MountFactory, session_factory: sessionmaker[Session]) -> int:
    mid = make_mount()
    with session_scope(session_factory) as s:
        r = Rule(name="scans", src_mount_id=mid, dst_path="/x", interval_seconds=30)
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def sent() -> list[tuple[str, str, str]]:
    return []


@pytest.fixture
def notifier(session_factory: sessionmaker[Session], sent: list[tuple[str, str, str]]) -> Notifier:
    return Notifier(session_factory, sender=lambda *a: sent.append(a), background=False)


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def service(
    session_factory: sessionmaker[Session], fake_runner: FakeRunner, notifier: Notifier
) -> RunService:
    return RunService(session_factory, fake_runner, notifier)  # type: ignore[arg-type]


def _rule(sf: sessionmaker[Session], rid: int) -> Rule:
    with sf() as s:
        r = s.get(Rule, rid)
        assert r is not None
        s.expunge(r)
        return r


def _enable_webhook(sf: sessionmaker[Session], mode: NotifyMode, after: int = 3) -> None:
    save_settings(
        sf, AppSettings(webhook_url="http://hook.invalid/x", notify_mode=mode, notify_after=after)
    )


def ok() -> RunResult:
    return RunResult(
        RunStatus.OK, seen=1, moved=1, items=[ItemResult("a.pdf", ItemStatus.MOVED, "/x/a.pdf", 3)]
    )


def err() -> RunResult:
    return RunResult(RunStatus.ERROR, message="„scans“ ist nicht eingehängt")


def partial() -> RunResult:
    return RunResult(
        RunStatus.PARTIAL,
        seen=2,
        moved=1,
        failed=1,
        items=[ItemResult("b.pdf", ItemStatus.FAILED, None, 1, "kaputt (Versuch 1)")],
    )


def skipped() -> RunResult:
    return RunResult(RunStatus.SKIPPED, message="läuft noch")


def test_run_and_items_are_recorded(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    fake_runner.results.append(ok())
    run_id = service.execute(rule_id)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert (run.status, run.seen, run.moved, run.trigger) == (
            RunStatus.OK,
            1,
            1,
            RunTrigger.SCHEDULE,
        )
        assert run.finished_at is not None and run.finished_at >= run.started_at
        items = s.scalars(select(RunItem)).all()
        assert [(i.src, i.dst, i.status) for i in items] == [
            ("a.pdf", "/x/a.pdf", ItemStatus.MOVED)
        ]
    r = _rule(session_factory, rule_id)
    assert r.last_success_at is not None and r.consecutive_failures == 0


def test_counters(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    fake_runner.results += [err(), partial(), skipped()]
    service.execute(rule_id)
    r = _rule(session_factory, rule_id)
    assert r.consecutive_failures == 1 and r.last_success_at is None and r.last_run_at is not None
    service.execute(rule_id)
    r = _rule(session_factory, rule_id)
    assert r.consecutive_failures == 2 and r.last_success_at is not None  # partial: Werkzeug lebt
    last_run = r.last_run_at
    service.execute(rule_id)  # übersprungen: protokolliert, Zähler unverändert
    r = _rule(session_factory, rule_id)
    assert r.consecutive_failures == 2 and r.last_run_at == last_run
    with session_factory() as s:
        assert [x.status for x in s.scalars(select(Run).order_by(Run.id))] == [
            RunStatus.ERROR,
            RunStatus.PARTIAL,
            RunStatus.SKIPPED,
        ]


def test_manual_dry_run_does_not_touch_counters(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    fake_runner.results.append(RunResult(RunStatus.ERROR, dry_run=True, message="x"))
    service.execute(rule_id, RunTrigger.MANUAL, dry_run=True)
    r = _rule(session_factory, rule_id)
    assert r.consecutive_failures == 0 and r.last_run_at is None


def test_deleted_rule_is_not_recorded(service: RunService, fake_runner: FakeRunner) -> None:
    fake_runner.results.append(err())
    assert service.execute(4711) is None


def test_items_are_capped(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("netmover.runs.MAX_ITEMS_PER_RUN", 3)
    fake_runner.results.append(
        RunResult(RunStatus.OK, items=[ItemResult(f"{i}.pdf", ItemStatus.MOVED) for i in range(5)])
    )
    run_id = service.execute(rule_id)
    with session_factory() as s:
        assert len(s.scalars(select(RunItem)).all()) == 3
        run = s.get(Run, run_id)
        assert run is not None and "2 weitere" in (run.message or "")


# -- Benachrichtigung -------------------------------------------------------------------------


def test_notify_every_failure(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    sent: list[tuple[str, str, str]],
) -> None:
    _enable_webhook(session_factory, NotifyMode.EVERY)
    fake_runner.results += [err(), err(), ok()]
    for _ in range(3):
        service.execute(rule_id)
    bodies = [json.loads(b) for _, _, b in sent]
    assert [b["event"] for b in bodies] == ["rule.failed", "rule.failed", "rule.recovered"]
    assert bodies[0]["message"] == "„scans“ ist nicht eingehängt"
    assert "2× in Folge" in bodies[1]["title"]


def test_notify_after_n_only_once_at_threshold(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    sent: list[tuple[str, str, str]],
) -> None:
    _enable_webhook(session_factory, NotifyMode.AFTER_N, after=3)
    fake_runner.results += [err(), err(), err(), err(), partial(), ok()]
    events = []
    for _ in range(6):
        service.execute(rule_id)
        events.append(len(sent))
    assert events == [0, 0, 1, 1, 1, 2]
    assert json.loads(sent[-1][2])["event"] == "rule.recovered"


def test_no_recovery_message_below_threshold(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    sent: list[tuple[str, str, str]],
) -> None:
    _enable_webhook(session_factory, NotifyMode.AFTER_N, after=3)
    fake_runner.results += [err(), ok()]
    service.execute(rule_id)
    service.execute(rule_id)
    assert sent == []


def test_notify_off_by_default(
    service: RunService, fake_runner: FakeRunner, rule_id: int, sent: list[tuple[str, str, str]]
) -> None:
    fake_runner.results.append(err())
    service.execute(rule_id)
    assert sent == []


def quarantined(*names: str) -> RunResult:
    return RunResult(
        RunStatus.OK,
        seen=len(names),
        skipped=len(names),
        items=[
            ItemResult(
                n, ItemStatus.QUARANTINED, f"nas:/quarantaene/{n}", 1, "nach 3 Fehlversuchen"
            )
            for n in names
        ],
    )


def test_notify_quarantine_in_every_mode(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    sent: list[tuple[str, str, str]],
) -> None:
    _enable_webhook(session_factory, NotifyMode.AFTER_N, after=3)
    fake_runner.results += [quarantined("a.pdf"), ok()]
    service.execute(rule_id)
    service.execute(rule_id)
    assert len(sent) == 1
    body = json.loads(sent[0][2])
    assert body["event"] == "rule.quarantined"
    assert body["rule"] == "scans"
    assert "Eine Datei in Quarantäne" in body["title"]
    assert "a.pdf → nas:/quarantaene/a.pdf" in body["message"]


def test_notify_quarantine_lists_at_most_ten(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    sent: list[tuple[str, str, str]],
) -> None:
    _enable_webhook(session_factory, NotifyMode.EVERY)
    fake_runner.results.append(quarantined(*(f"{i}.pdf" for i in range(12))))
    service.execute(rule_id)
    body = json.loads(sent[0][2])
    assert "12 Dateien in Quarantäne" in body["title"]
    assert body["message"].count("•") == 10
    assert "… und 2 weitere" in body["message"]


def test_no_quarantine_notice_when_off_or_dry_run(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
    sent: list[tuple[str, str, str]],
) -> None:
    fake_runner.results.append(quarantined("a.pdf"))
    service.execute(rule_id)
    assert sent == []
    _enable_webhook(session_factory, NotifyMode.EVERY)
    dry = quarantined("b.pdf")
    dry.dry_run = True
    fake_runner.results.append(dry)
    service.execute(rule_id, dry_run=True)
    assert sent == []


def test_render_json_escapes_values() -> None:
    n = Notification("rule.failed", 'Titel mit "Anführung"', "Zeile 1\nZeile 2 \\ $x", "r")
    body = render('{"t": "$title", "m": "$message", "x": "$unbekannt"}', "application/json", n)
    data = json.loads(body)
    assert data == {"t": 'Titel mit "Anführung"', "m": "Zeile 1\nZeile 2 \\ $x", "x": "$unbekannt"}
    assert render("$message", "text/plain", n) == "Zeile 1\nZeile 2 \\ $x"


def test_sender_failure_is_swallowed(session_factory: sessionmaker[Session]) -> None:
    def broken(*a: object) -> None:
        raise ConnectionError("weg")

    _enable_webhook(session_factory, NotifyMode.EVERY)
    Notifier(session_factory, sender=broken, background=False).send(Notification("x", "t", "m"))


def test_settings_roundtrip(session_factory: sessionmaker[Session]) -> None:
    assert load_settings(session_factory) == AppSettings()
    save_settings(session_factory, AppSettings(retention_days=7, notify_mode=NotifyMode.EVERY))
    loaded = load_settings(session_factory)
    assert loaded.retention_days == 7 and loaded.notify_mode is NotifyMode.EVERY


# -- Aufräumen ---------------------------------------------------------------------------------


def test_cleanup_removes_old_runs_with_items(
    service: RunService,
    fake_runner: FakeRunner,
    rule_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    fake_runner.results += [ok(), ok()]
    old = service.execute(rule_id)
    new = service.execute(rule_id)
    with session_scope(session_factory) as s:
        run = s.get(Run, old)
        assert run is not None
        run.started_at = utcnow() - timedelta(days=31)
        s.add(
            FileFailure(
                rule_id=rule_id,
                src_relpath="x",
                size=1,
                mtime_ns=1,
                attempts=1,
                last_attempt_at=utcnow() - timedelta(days=40),
            )
        )
    stats = cleanup(session_factory)
    assert stats == {"runs": 1, "file_failures": 1}
    with session_factory() as s:
        assert [r.id for r in s.scalars(select(Run))] == [new]
        assert len(s.scalars(select(RunItem)).all()) == 1


# -- Health (Fallstrick 9) -------------------------------------------------------------------


def _health(sf: sessionmaker[Session], started_ago: float = 3600, **kw: object):  # type: ignore[no-untyped-def]
    now = utcnow()
    args: dict[str, object] = {
        "scheduler_running": True,
        "last_heartbeat": now,
        "service_started": now - timedelta(seconds=started_ago),
        "now": now,
    }
    args.update(kw)
    return check_health(sf, **args)  # type: ignore[arg-type]


def _age_rule(sf: sessionmaker[Session], rid: int, seconds: float | None) -> None:
    with session_scope(sf) as s:
        r = s.get(Rule, rid)
        assert r is not None
        r.created_at = utcnow() - timedelta(days=1)
        r.last_success_at = None if seconds is None else utcnow() - timedelta(seconds=seconds)


def test_health_ok(session_factory: sessionmaker[Session], rule_id: int) -> None:
    _age_rule(session_factory, rule_id, 60)  # 2× Intervall
    h = _health(session_factory)
    assert h.ok, h.problems


def test_health_stale_rule_after_three_intervals(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    _age_rule(session_factory, rule_id, 91)
    h = _health(session_factory)
    assert not h.ok and h.stale_rules == {rule_id}
    assert "kein erfolgreicher Lauf" in h.problems[0]


def test_health_never_succeeded_but_service_just_started(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    _age_rule(session_factory, rule_id, None)
    assert _health(session_factory, started_ago=30).ok
    assert not _health(session_factory, started_ago=120).ok


def test_health_disabled_rule_is_ignored(
    session_factory: sessionmaker[Session], rule_id: int
) -> None:
    _age_rule(session_factory, rule_id, None)
    with session_scope(session_factory) as s:
        r = s.get(Rule, rule_id)
        assert r is not None
        r.enabled = False
    assert _health(session_factory).ok


def test_health_mount_error_only_for_enabled_mounts(
    session_factory: sessionmaker[Session], make_mount: MountFactory
) -> None:
    mid = make_mount("b", share="b", enabled=False)
    with session_scope(session_factory) as s:
        m = s.get(Mount, mid)
        assert m is not None
        m.state = MountState.ERROR
    assert _health(session_factory).ok
    with session_scope(session_factory) as s:
        m = s.get(Mount, mid)
        assert m is not None
        m.enabled = True
    h = _health(session_factory)
    assert not h.ok and "Fehlerzustand" in h.problems[0]


def test_health_scheduler(session_factory: sessionmaker[Session]) -> None:
    assert not _health(session_factory, scheduler_running=False).ok
    assert not _health(session_factory, last_heartbeat=utcnow() - timedelta(minutes=5)).ok


def test_runner_type_is_compatible(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    # FakeRunner oben muss dieselbe Schnittstelle bieten wie RuleRunner.run
    assert RuleRunner.run.__code__.co_kwonlyargcount == 1
