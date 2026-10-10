"""Gesundheitszustand für ``/healthz`` und die Statusseite (Fallstrick 9).

Ein Werkzeug, das nichts mehr tut, darf nicht wie eines aussehen, das nichts zu
tun hat. Deshalb:

* der Scheduler muss leben (Herzschlag nicht älter als 3 Minuten),
* keine aktive Anbindung im Fehlerzustand,
* keine aktive Regel, deren letzter erfolgreicher Lauf länger als das Dreifache
  ihres Intervalls zurückliegt. Bezugspunkt ist der späteste von letztem Erfolg,
  Anlage der Regel und Dienststart – sonst wäre direkt nach einem Neustart alles rot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import Mount, MountState, Rule, utcnow

STALE_FACTOR = 3
HEARTBEAT_MAX_AGE = timedelta(minutes=3)


@dataclass
class Health:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    stale_rules: set[int] = field(default_factory=set)

    def fail(self, problem: str) -> None:
        self.ok = False
        self.problems.append(problem)


def rule_is_stale(rule: Rule, service_started: datetime, now: datetime) -> bool:
    if not rule.enabled:
        return False
    reference = max(
        d for d in (rule.last_success_at, rule.created_at, service_started) if d is not None
    )
    return now - reference > timedelta(seconds=STALE_FACTOR * rule.interval_seconds)


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
        health.fail("Scheduler läuft nicht")
    elif last_heartbeat is None or now - last_heartbeat > HEARTBEAT_MAX_AGE:
        health.fail("Scheduler reagiert nicht (kein Herzschlag)")
    with session_scope(session_factory) as s:
        for m in s.scalars(select(Mount).where(Mount.enabled.is_(True))):
            if m.state is MountState.ERROR:
                health.fail(f"Anbindung „{m.name}“ im Fehlerzustand: {m.last_error or ''}".strip())
        for r in s.scalars(select(Rule).where(Rule.enabled.is_(True))):
            if rule_is_stale(r, service_started, now):
                health.stale_rules.add(r.id)
                ago = (
                    "noch nie"
                    if r.last_success_at is None
                    else f"seit {r.last_success_at:%Y-%m-%d %H:%M} UTC"
                )
                health.fail(f"Regel „{r.name}“: kein erfolgreicher Lauf {ago}")
    return health
