"""Gesundheitszustand für ``/healthz``, Uptime Kuma und die Statusseite (Fallstrick 9).

Ein Werkzeug, das nichts mehr tut, darf nicht wie eines aussehen, das nichts zu
tun hat. Geprüft wird:

* der Scheduler lebt (Herzschlag nicht älter als 3 Minuten),
* jede aktive Anbindung ist nicht im Fehlerzustand,
* jede aktive Regel hatte vor höchstens dem Dreifachen ihres Intervalls einen
  erfolgreichen Lauf (Bezug: spätester von letztem Erfolg, Anlage der Regel und
  Dienststart – sonst wäre direkt nach einem Neustart alles rot) und ist nicht
  mehrmals hintereinander gescheitert.

Jeder Punkt ist ein eigener :class:`Check` – so kann Uptime Kuma auch einzelne
Anbindungen und Regeln überwachen (``/healthz/mount/<slug>``, ``/healthz/rule/<id>``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import Mount, MountState, Rule, utcnow

STALE_FACTOR = 3
FAILURES_ALERT = 3
HEARTBEAT_MAX_AGE = timedelta(minutes=3)


@dataclass
class Check:
    kind: str  # "scheduler" | "mount" | "rule"
    name: str
    ok: bool
    message: str
    id: int | None = None
    slug: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Health:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    stale_rules: set[int] = field(default_factory=set)
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)
        if not check.ok:
            self.ok = False
            self.problems.append(check.message)

    def find(self, kind: str, *, slug: str | None = None, id: int | None = None) -> Check | None:
        for c in self.checks:
            if c.kind == kind and (slug is None or c.slug == slug) and (id is None or c.id == id):
                return c
        return None


def rule_is_stale(rule: Rule, service_started: datetime, now: datetime) -> bool:
    if not rule.enabled:
        return False
    reference = max(
        d for d in (rule.last_success_at, rule.created_at, service_started) if d is not None
    )
    return now - reference > timedelta(seconds=STALE_FACTOR * rule.interval_seconds)


def _mount_check(m: Mount) -> Check:
    def mk(ok: bool, message: str) -> Check:
        return Check("mount", m.name, ok, message, id=m.id, slug=m.slug)

    if not m.enabled:
        return mk(True, f"Anbindung „{m.name}“ deaktiviert")
    if m.state is MountState.ERROR:
        detail = (m.last_error or "").strip()
        return mk(False, f"Anbindung „{m.name}“ im Fehlerzustand: {detail}".strip())
    if m.state is MountState.DEGRADED:
        return mk(True, f"Anbindung „{m.name}“ gestört, wird neu eingehängt")
    return mk(True, f"Anbindung „{m.name}“ in Ordnung")


def _rule_check(r: Rule, service_started: datetime, now: datetime) -> Check:
    def mk(ok: bool, message: str) -> Check:
        return Check("rule", r.name, ok, message, id=r.id)

    if not r.enabled:
        return mk(True, f"Regel „{r.name}“ deaktiviert")
    if rule_is_stale(r, service_started, now):
        ago = (
            "noch nie"
            if r.last_success_at is None
            else f"seit {r.last_success_at:%Y-%m-%d %H:%M} UTC"
        )
        return mk(False, f"Regel „{r.name}“: kein erfolgreicher Lauf {ago}")
    if r.consecutive_failures >= FAILURES_ALERT:
        return mk(
            False, f"Regel „{r.name}“: {r.consecutive_failures} fehlgeschlagene Läufe in Folge"
        )
    return mk(True, f"Regel „{r.name}“ in Ordnung")


def check_health(
    session_factory: sessionmaker[Session],
    *,
    scheduler_running: bool,
    last_heartbeat: datetime | None,
    service_started: datetime,
    now: datetime | None = None,
) -> Health:
    now = now or utcnow()
    health = Health()
    if not scheduler_running:
        health.add(Check("scheduler", "Scheduler", False, "Scheduler läuft nicht"))
    elif last_heartbeat is None or now - last_heartbeat > HEARTBEAT_MAX_AGE:
        health.add(
            Check("scheduler", "Scheduler", False, "Scheduler reagiert nicht (kein Herzschlag)")
        )
    else:
        health.add(Check("scheduler", "Scheduler", True, "Scheduler läuft"))
    with session_scope(session_factory) as s:
        for m in s.scalars(select(Mount).order_by(Mount.name)):
            health.add(_mount_check(m))
        for r in s.scalars(select(Rule).order_by(Rule.name)):
            check = _rule_check(r, service_started, now)
            if r.enabled and rule_is_stale(r, service_started, now):
                health.stale_rules.add(r.id)
            health.add(check)
    return health
