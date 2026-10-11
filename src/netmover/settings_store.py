"""Laufzeit-Einstellungen in der Tabelle ``settings`` (in der Oberfläche änderbar)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import Setting


class NotifyMode(StrEnum):
    OFF = "off"
    EVERY = "every"  # bei jedem fehlgeschlagenen Lauf
    AFTER_N = "after_n"  # erst nach N fehlgeschlagenen Läufen in Folge


DEFAULT_TEMPLATE = (
    '{"event": "$event", "title": "$title", "message": "$message", '
    '"rule": "$rule", "time": "$time", "host": "$host"}'
)


@dataclass
class AppSettings:
    retention_days: int = 30
    webhook_url: str = ""
    notify_mode: NotifyMode = NotifyMode.OFF
    notify_after: int = 3
    notify_recovery: bool = True
    webhook_template: str = DEFAULT_TEMPLATE
    webhook_content_type: str = "application/json"
    kuma_push_url: str = ""
    # Wartungsmodus: geplante Läufe pausiert, kein Alarm für Regeln
    maintenance: bool = False
    # E-Mail (SMTP); Passwort Fernet-verschlüsselt
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_user: str = ""
    smtp_password_enc: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    # Anmeldung: Netze ohne Passwort (trusted_networks), Netze mit Passwort
    # (allowed_networks, leer = alle übrigen); alle anderen Netze werden abgewiesen.
    # auth_mode "off" = ohne Anmeldung von überall (aus 0.5.0, weiterhin gültig).
    auth_mode: str = "always"
    trusted_networks: str = ""
    allowed_networks: str = ""


_PREFIX = "app."


def load_settings(factory: sessionmaker[Session]) -> AppSettings:
    with session_scope(factory) as s:
        rows = {
            r.key[len(_PREFIX) :]: r.value
            for r in s.scalars(select(Setting).where(Setting.key.like(f"{_PREFIX}%")))
        }
    result = AppSettings()
    for f in fields(AppSettings):
        if f.name in rows:
            raw = json.loads(rows[f.name])
            setattr(result, f.name, NotifyMode(raw) if f.name == "notify_mode" else raw)
    return result


def save_settings(factory: sessionmaker[Session], settings: AppSettings) -> None:
    with session_scope(factory) as s:
        for key, value in asdict(settings).items():
            row = s.get(Setting, _PREFIX + key)
            encoded = json.dumps(value.value if isinstance(value, StrEnum) else value)
            if row is None:
                s.add(Setting(key=_PREFIX + key, value=encoded))
            else:
                row.value = encoded


def get_value(factory: sessionmaker[Session], key: str) -> str | None:
    with session_scope(factory) as s:
        row = s.get(Setting, key)
        return row.value if row else None


def set_value(factory: sessionmaker[Session], key: str, value: str) -> None:
    with session_scope(factory) as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
