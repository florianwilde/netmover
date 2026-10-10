"""Benachrichtigung per generischem Webhook.

Der Body ist eine Vorlage mit ``$platzhaltern`` (``string.Template``):
``$event``, ``$title``, ``$message``, ``$rule``, ``$time``, ``$host``.
Ist der Content-Type JSON, werden die Werte JSON-maskiert eingesetzt – so
funktionieren auch eigene JSON-Formate (z. B. Gotify ``{"title": "$title", …}``).
Für ntfy reicht ``text/plain`` mit ``$message`` und der Topic-URL.

Versand im Hintergrund-Thread, damit ein langsamer Empfänger nie einen Lauf bremst.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from string import Template

import httpx
from sqlalchemy.orm import Session, sessionmaker

from netmover.logging_setup import event, get_logger
from netmover.settings_store import AppSettings, NotifyMode, load_settings

log = get_logger(__name__)


@dataclass(frozen=True)
class Notification:
    event: str  # z. B. "rule.failed", "rule.recovered", "mount.error"
    title: str
    message: str
    rule: str = ""


def render(template: str, content_type: str, n: Notification, now: datetime | None = None) -> str:
    values = {
        "event": n.event,
        "title": n.title,
        "message": n.message,
        "rule": n.rule,
        "time": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
    }
    if "json" in content_type.lower():
        values = {k: json.dumps(v, ensure_ascii=False)[1:-1] for k, v in values.items()}
    return Template(template).safe_substitute(values)


Sender = Callable[[str, str, str], None]  # (url, content_type, body)


def http_sender(url: str, content_type: str, body: str) -> None:
    resp = httpx.post(
        url, content=body.encode(), headers={"Content-Type": content_type}, timeout=10
    )
    resp.raise_for_status()


class Notifier:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        sender: Sender = http_sender,
        background: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self._sender = sender
        self._background = background

    def settings(self) -> AppSettings:
        return load_settings(self.session_factory)

    def send(self, n: Notification, settings: AppSettings | None = None) -> None:
        cfg = settings or self.settings()
        if cfg.notify_mode is NotifyMode.OFF or not cfg.webhook_url:
            return
        body = render(cfg.webhook_template, cfg.webhook_content_type, n)

        def deliver() -> None:
            try:
                self._sender(cfg.webhook_url, cfg.webhook_content_type, body)
                event(log, "notify.sent", kind=n.event, rule=n.rule)
            except Exception as exc:  # Benachrichtigung darf nie den Dienst stören
                # URL nicht protokollieren – sie enthält oft ein Token.
                event(log, "notify.failed", level=30, kind=n.event, error=type(exc).__name__)

        if self._background:
            threading.Thread(target=deliver, name="nm-notify", daemon=True).start()
        else:
            deliver()

    def rule_result(self, rule: str, failed: bool, consecutive: int, message: str) -> None:
        """Entscheidet nach Modus, ob ein Regelergebnis gemeldet wird."""
        cfg = self.settings()
        if cfg.notify_mode is NotifyMode.OFF:
            return
        threshold = 1 if cfg.notify_mode is NotifyMode.EVERY else max(1, cfg.notify_after)
        if failed:
            if cfg.notify_mode is NotifyMode.EVERY or consecutive == threshold:
                suffix = f" ({consecutive}× in Folge)" if consecutive > 1 else ""
                self.send(
                    Notification(
                        "rule.failed",
                        f"NetMover: Regel „{rule}“ fehlgeschlagen{suffix}",
                        message,
                        rule,
                    ),
                    cfg,
                )
        elif cfg.notify_recovery and consecutive >= threshold:
            self.send(
                Notification(
                    "rule.recovered",
                    f"NetMover: Regel „{rule}“ läuft wieder",
                    f"Nach {consecutive} fehlgeschlagenen Läufen wieder erfolgreich.",
                    rule,
                ),
                cfg,
            )

    def mount_event(self, kind: str, mount: str, message: str) -> None:
        """Passt zur ``Notifier``-Signatur des :class:`MountManager`."""
        title = (
            f"NetMover: Anbindung „{mount}“ wieder verfügbar"
            if kind == "mount.recovered"
            else f"NetMover: Anbindung „{mount}“ gestört"
        )
        self.send(Notification(kind, title, message))
