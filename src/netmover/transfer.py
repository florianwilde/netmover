"""Export/Import der Konfiguration als JSON – ohne Zugangsdaten.

Ausgenommen sind Passwörter, private Schlüssel, Benutzernamen und die
Webhook-URL (enthält meist ein Token). Nach einem Import müssen Zugangsdaten
neu eingegeben werden. Anbindungen und Regeln werden über ihren Namen
zugeordnet; vorhandene Namen werden übersprungen, nie überschrieben.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from netmover import __version__
from netmover.models import (
    CollisionPolicy,
    Mount,
    MountState,
    MountType,
    PatternType,
    Rule,
    RuleAction,
)
from netmover.mounting.manager import MountManager
from netmover.settings_store import AppSettings, NotifyMode
from netmover.validation import (
    ValidationError,
    parse_extra_options,
    safe_relpath,
    slugify,
    validate_server,
    validate_share,
)

FORMAT_VERSION = 1

MOUNT_FIELDS = (
    "name",
    "type",
    "server",
    "share",
    "port",
    "host_key",
    "uid",
    "gid",
    "file_mode",
    "dir_mode",
    "extra_options",
    "auto_mount",
    "enabled",
)
RULE_FIELDS = (
    "name",
    "enabled",
    "src_subpath",
    "dst_path",
    "pattern",
    "pattern_type",
    "recursive",
    "interval_seconds",
    "stability_seconds",
    "min_age_seconds",
    "max_size_mb",
    "op_timeout_seconds",
    "action",
    "on_collision",
    "set_owner",
    "set_mode",
    "dry_run",
    "quarantine_path",
    "quarantine_after",
)
SETTING_FIELDS = (
    "retention_days",
    "notify_mode",
    "notify_after",
    "notify_recovery",
    "webhook_template",
    "webhook_content_type",
)


def _plain(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def export_config(s: Session, settings: AppSettings) -> dict[str, Any]:
    mounts = s.scalars(select(Mount).order_by(Mount.name)).all()
    names = {m.id: m.name for m in mounts}
    return {
        "format": FORMAT_VERSION,
        "netmover": __version__,
        "mounts": [{f: _plain(getattr(m, f)) for f in MOUNT_FIELDS} for m in mounts],
        "rules": [
            {
                **{f: _plain(getattr(r, f)) for f in RULE_FIELDS},
                "src_mount": names[r.src_mount_id],
                "dst_mount": names.get(r.dst_mount_id) if r.dst_mount_id else None,
            }
            for r in s.scalars(select(Rule).order_by(Rule.name))
        ],
        "settings": {f: _plain(getattr(settings, f)) for f in SETTING_FIELDS},
    }


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)


def import_config(s: Session, mounts: MountManager, data: dict[str, Any]) -> ImportReport:
    report = ImportReport()
    if not isinstance(data, dict) or data.get("format") != FORMAT_VERSION:
        report.errors.append("Unbekanntes Format – erwartet wird ein NetMover-Export (format 1).")
        return report

    for raw in data.get("mounts") or []:
        name = str(raw.get("name", ""))
        if s.scalar(select(Mount.id).where(Mount.name == name)) is not None:
            report.skipped.append(f"Anbindung „{name}“ (existiert)")
            continue
        try:
            mtype = MountType(raw["type"])
            slug = slugify(name)
            if s.scalar(select(Mount.id).where(Mount.slug == slug)) is not None:
                raise ValidationError("name", "Slug bereits vergeben")
            m = Mount(
                name=name,
                slug=slug,
                type=mtype,
                server=validate_server(str(raw["server"])),
                share=validate_share(mtype, str(raw["share"])),
                port=raw.get("port"),
                host_key=raw.get("host_key"),
                uid=raw.get("uid"),
                gid=raw.get("gid"),
                file_mode=raw.get("file_mode"),
                dir_mode=raw.get("dir_mode"),
                extra_options=str(raw.get("extra_options") or ""),
                # ohne Zugangsdaten nicht automatisch einhängen
                auto_mount=bool(raw.get("auto_mount")) and mtype is not MountType.CIFS,
                enabled=bool(raw.get("enabled", True)),
                state=MountState.UNMOUNTED,
                mountpoint=str(mounts.mountpoint_for(slug)) if mtype.is_kernel_mount else None,
            )
            parse_extra_options(mtype, m.extra_options)
            s.add(m)
            s.flush()
            report.created.append(f"Anbindung „{name}“")
        except (KeyError, ValueError, ValidationError) as exc:
            report.errors.append(f"Anbindung „{name}“: {exc}")

    ids = {name: mid for mid, name in s.execute(select(Mount.id, Mount.name)).all()}
    for raw in data.get("rules") or []:
        name = str(raw.get("name", ""))
        if s.scalar(select(Rule.id).where(Rule.name == name)) is not None:
            report.skipped.append(f"Regel „{name}“ (existiert)")
            continue
        try:
            src = ids[raw["src_mount"]]
            dst = ids[raw["dst_mount"]] if raw.get("dst_mount") else None
            r = Rule(src_mount_id=src, dst_mount_id=dst)
            for f in RULE_FIELDS:
                if f in raw:
                    setattr(r, f, raw[f])
            r.pattern_type = PatternType(r.pattern_type)
            r.action = RuleAction(r.action)
            r.on_collision = CollisionPolicy(r.on_collision)
            r.src_subpath = safe_relpath("src_subpath", r.src_subpath or "")
            if int(r.interval_seconds) < 10:
                raise ValueError("Intervall < 10 s")
            r.enabled = False  # erst nach Eingabe der Zugangsdaten aktivieren
            s.add(r)
            s.flush()
            report.created.append(f"Regel „{name}“ (deaktiviert)")
        except KeyError as exc:
            report.errors.append(f"Regel „{name}“: Anbindung {exc} fehlt")
        except (ValueError, ValidationError) as exc:
            report.errors.append(f"Regel „{name}“: {exc}")

    if isinstance(data.get("settings"), dict):
        report.settings = {f: data["settings"][f] for f in SETTING_FIELDS if f in data["settings"]}
    return report


def apply_imported_settings(current: AppSettings, values: dict[str, Any]) -> AppSettings:
    """Nach dem Commit des Imports aufrufen (eigene Transaktion)."""
    for key, value in values.items():
        setattr(current, key, NotifyMode(value) if key == "notify_mode" else value)
    return current
