"""Uptime Kuma: NetMover meldet sich regelmäßig bei einem „Push“-Monitor.

Vorteil gegenüber dem Abfragen von ``/healthz``: Es funktioniert auch, wenn Kuma
NetMover nicht erreichen kann (z. B. Bindung an 127.0.0.1 hinter einem Proxy). Und
steht der Dienst oder die ganze VM, bleibt die Meldung aus – dann schlägt Kuma
von selbst Alarm.

Die Push-URL kopiert man aus Kuma (``https://kuma/api/push/<token>?status=up&msg=OK&ping=``);
NetMover ersetzt ``status`` und ``msg`` bei jeder Meldung.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from netmover.health import Health
from netmover.logging_setup import event, get_logger

log = get_logger(__name__)

MAX_MESSAGE = 250

Getter = Callable[..., Any]


def ok_message(metrics: dict[str, object] | None) -> str:
    """Kurzer Text für die Kuma-Ansicht, z. B. „OK · 24 h: 12 Dateien“."""
    if not metrics:
        return "OK"
    parts = [f"24 h: {metrics.get('files_24h', 0)} Dateien"]
    failed = metrics.get("failed_24h") or 0
    if failed:
        parts.append(f"{failed} Fehler")
    return "OK · " + ", ".join(parts)


def push_url(url: str, health: Health, metrics: dict[str, object] | None = None) -> str:
    """Push-URL mit aktuellem Zustand (vorhandene Parameter werden ersetzt)."""
    msg = ok_message(metrics) if health.ok else "; ".join(health.problems)
    if len(msg) > MAX_MESSAGE:
        msg = msg[: MAX_MESSAGE - 1] + "…"
    target = httpx.URL(url)
    for key, value in (("status", "up" if health.ok else "down"), ("msg", msg), ("ping", "")):
        target = target.copy_set_param(key, value)
    return str(target)


def push(
    url: str,
    health: Health,
    getter: Getter | None = None,
    metrics: dict[str, object] | None = None,
) -> bool:
    """Meldet den Zustand an Kuma. Fehler werden protokolliert, nie weitergereicht –
    die URL nicht, weil sie das Token enthält."""
    try:
        resp = (getter or httpx.get)(push_url(url, health, metrics), timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        event(log, "kuma.push_failed", level=30, error=type(exc).__name__)
        return False
    event(log, "kuma.pushed", level=10, status="up" if health.ok else "down")
    return True
