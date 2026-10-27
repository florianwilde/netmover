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

from netmover.crypto import SecretBox
from netmover.logging_setup import event, get_logger
from netmover.mail import mail_configured, smtp_send
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


Mailer = Callable[[AppSettings, "str | None", str, str], None]  # (cfg, passwort, betreff, text)


class Notifier:
    """Verteilt Benachrichtigungen auf die eingerichteten Kanäle (Webhook, E-Mail)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        sender: Sender = http_sender,
        mailer: Mailer | None = None,
        secrets: SecretBox | None = None,
        background: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self._sender = sender
        self._mailer: Mailer = mailer or smtp_send
        self._secrets = secrets
        self._background = background

    def settings(self) -> AppSettings:
        return load_settings(self.session_factory)

    def smtp_password(self, cfg: AppSettings) -> str | None:
        if not cfg.smtp_password_enc or self._secrets is None:
            return None
        return self._secrets.decrypt(cfg.smtp_password_enc)

    def send_mail_now(self, subject: str, body: str, settings: AppSettings | None = None) -> None:
        """Synchron senden (Test-Knopf); Fehler werden weitergereicht."""
        cfg = settings or self.settings()
        self._mailer(cfg, self.smtp_password(cfg), subject, body)

    def send(self, n: Notification, settings: AppSettings | None = None) -> None:
        cfg = settings or self.settings()
        if cfg.notify_mode is NotifyMode.OFF:
            return
        jobs: list[tuple[str, Callable[[], None]]] = []
        if cfg.webhook_url:
            body = render(cfg.webhook_template, cfg.webhook_content_type, n)
            jobs.append(
                ("webhook", lambda: self._sender(cfg.webhook_url, cfg.webhook_content_type, body))
            )
        if mail_configured(cfg):
            password = self.smtp_password(cfg)
            jobs.append(("email", lambda: self._mailer(cfg, password, n.title, n.message)))
        if not jobs:
            return

        def deliver() -> None:
            for channel, job in jobs:
                try:
                    job()
                    event(log, "notify.sent", kind=n.event, rule=n.rule, channel=channel)
                except Exception as exc:  # Benachrichtigung darf nie den Dienst stören
                    # Weder URL noch Zugangsdaten protokollieren.
                    event(
                        log,
                        "notify.failed",
                        level=30,
                        kind=n.event,
                        channel=channel,
                        error=type(exc).__name__,
                    )

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

    def quarantined(self, rule: str, files: list[tuple[str, str]]) -> None:
        """Dateien, die eine Regel in die Quarantäne verschoben hat: (Datei, neuer Ort).

        Gemeldet in jedem Modus außer „aus“: Jede Datei landet nur einmal dort und
        bleibt liegen, bis jemand sie sich ansieht."""
        cfg = self.settings()
        if cfg.notify_mode is NotifyMode.OFF or not files:
            return
        shown = files[:10]
        lines = [f"• {src} → {dst}" for src, dst in shown]
        if len(files) > len(shown):
            lines.append(f"… und {len(files) - len(shown)} weitere")
        count = f"{len(files)} Dateien" if len(files) > 1 else "Eine Datei"
        self.send(
            Notification(
                "rule.quarantined",
                f"NetMover: Regel „{rule}“ – {count} in Quarantäne",
                "Nach wiederholten Fehlversuchen beiseitegelegt; bitte prüfen:\n"
                + "\n".join(lines),
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
