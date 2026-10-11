"""Verwaltung von Anbindungen und Regeln – Prüfen und Speichern von Formulardaten.

Unabhängig vom Web-Framework: nimmt ein ``Mapping[str, str]`` (Formular) und
liefert entweder gespeicherte Objekte oder ein Fehler-Dict ``{feld: meldung}``.

Passwörter: leer lassen heißt „unverändert“; sie werden nie zurückgegeben.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netmover.crypto import SecretBox, generate_ssh_keypair
from netmover.models import (
    ArchiveKind,
    ArchiveLayout,
    CollisionPolicy,
    Credential,
    CredentialKind,
    Mount,
    MountState,
    MountType,
    PatternType,
    Rule,
    RuleAction,
)
from netmover.mounting.manager import CredentialData, MountConfig, MountManager
from netmover.validation import (
    MountOption,
    ValidationError,
    parse_extra_options,
    safe_abspath,
    safe_relpath,
    slugify,
    validate_id,
    validate_mode,
    validate_port,
    validate_secret,
    validate_server,
    validate_share,
    validate_username,
)

Errors = dict[str, str]

MIN_INTERVAL = 10
_OWNER_RE = re.compile(r"^\d{1,10}(:\d{1,10})?$")


class AdminError(Exception):
    """Aktion nicht möglich – Meldung für die Oberfläche."""


def _bool(form: Mapping[str, str], key: str) -> bool:
    return form.get(key, "") in ("on", "true", "1", "yes")


def _int(
    form: Mapping[str, str],
    key: str,
    errors: Errors,
    *,
    default: int | None = None,
    minimum: int | None = None,
    label: str = "",
) -> int | None:
    raw = (form.get(key) or "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        errors[key] = "Bitte eine ganze Zahl angeben."
        return default
    if minimum is not None and value < minimum:
        errors[key] = f"{label or 'Wert'} muss mindestens {minimum} sein."
    return value


# -- Anbindungen ------------------------------------------------------------------------


NEW_KEY = "new"
OWN = "own"


@dataclass
class MountInput:
    name: str = ""
    type: MountType = MountType.CIFS
    server: str = ""
    share: str = ""
    port: int | None = None
    username: str = ""
    password: str = ""  # leer = unverändert
    domain: str = ""
    auth: CredentialKind = CredentialKind.PASSWORD  # nur sftp: Schlüssel oder Passwort
    # nur sftp + Schlüssel: "new" = eigener Schlüssel nur für diese Anbindung,
    # sonst ID eines Schlüssels aus der Schlüsselverwaltung
    key_id: str = NEW_KEY
    # Passwort-Anmeldung (SMB, SFTP): "own" = eigene Angaben, sonst ID gespeicherter Zugangsdaten
    cred_id: str = OWN
    uid: int | None = None
    gid: int | None = None
    file_mode: str | None = None
    dir_mode: str | None = None
    extra_options: str = ""
    auto_mount: bool = True
    enabled: bool = True

    def config(self) -> MountConfig:
        return MountConfig(
            name=self.name or "neu",
            type=self.type,
            server=self.server,
            share=self.share,
            port=self.port,
            uid=self.uid,
            gid=self.gid,
            file_mode=self.file_mode,
            dir_mode=self.dir_mode,
            extra_options=self.extra_options,
        )


def mount_input_from_model(m: Mount) -> MountInput:
    c = m.credential
    return MountInput(
        name=m.name,
        type=m.type,
        server=m.server,
        share=m.share,
        port=m.port,
        username=m.username or (c.username if c else ""),
        domain=(c.domain or "") if c else "",
        auth=c.kind if c else CredentialKind.PASSWORD,
        key_id=str(c.id)
        if c is not None and c.managed and c.kind is CredentialKind.SSH_KEY
        else NEW_KEY,
        cred_id=str(c.id)
        if c is not None and c.managed and c.kind is CredentialKind.PASSWORD
        else OWN,
        uid=m.uid,
        gid=m.gid,
        file_mode=m.file_mode,
        dir_mode=m.dir_mode,
        extra_options=m.extra_options,
        auto_mount=m.auto_mount,
        enabled=m.enabled,
    )


def parse_mount_form(form: Mapping[str, str]) -> tuple[MountInput, Errors]:
    errors: Errors = {}
    data = MountInput(
        name=(form.get("name") or "").strip(),
        server=(form.get("server") or "").strip(),
        share=(form.get("share") or "").strip(),
        username=(form.get("username") or "").strip(),
        password=form.get("password") or "",
        domain=(form.get("domain") or "").strip(),
        extra_options=(form.get("extra_options") or "").strip(),
        auto_mount=_bool(form, "auto_mount"),
        enabled=_bool(form, "enabled"),
        key_id=(form.get("key_id") or NEW_KEY).strip(),
        cred_id=(form.get("cred_id") or OWN).strip(),
    )
    if data.key_id != NEW_KEY and not data.key_id.isdigit():
        errors["key_id"] = "Ungültige Schlüsselauswahl."
        data.key_id = NEW_KEY
    if data.cred_id != OWN and not data.cred_id.isdigit():
        errors["cred_id"] = "Ungültige Auswahl."
        data.cred_id = OWN
    try:
        data.type = MountType(form.get("type", "cifs"))
    except ValueError:
        errors["type"] = "Unbekannter Typ."
        return data, errors
    try:
        data.auth = CredentialKind(form.get("auth") or "password")
    except ValueError:
        errors["auth"] = "Unbekannte Anmeldeart."

    if not data.name:
        errors["name"] = "Name fehlt."
    elif len(data.name) > 100:
        errors["name"] = "Höchstens 100 Zeichen."
    else:
        try:
            slugify(data.name)
        except ValidationError as exc:
            errors["name"] = str(exc)

    def check[T](key: str, fn: Callable[[], T], fallback: T) -> T:
        try:
            return fn()
        except ValidationError as exc:
            errors[key] = str(exc)
            return fallback

    data.server = check("server", lambda: validate_server(data.server), data.server)
    data.share = check("share", lambda: validate_share(data.type, data.share), data.share)
    no_options: list[MountOption] = []
    check("extra_options", lambda: parse_extra_options(data.type, data.extra_options), no_options)
    check("password", lambda: validate_secret(data.password), "")
    if data.type is MountType.CIFS:
        data.uid = check("uid", lambda: validate_id("uid", form.get("uid")), None)
        data.gid = check("gid", lambda: validate_id("gid", form.get("gid")), None)
        data.file_mode = check(
            "file_mode", lambda: validate_mode("file_mode", form.get("file_mode")), None
        )
        data.dir_mode = check(
            "dir_mode", lambda: validate_mode("dir_mode", form.get("dir_mode")), None
        )
    if data.type is MountType.SFTP:
        data.port = check("port", lambda: validate_port(form.get("port")), None)
    if data.username:
        data.username = check("username", lambda: validate_username(data.username), data.username)
    if data.domain:
        check("domain", lambda: validate_username(data.domain), "")
    if data.auth is CredentialKind.SSH_KEY or data.type in (MountType.NFS3, MountType.NFS4):
        data.cred_id = OWN
    uses_stored = data.cred_id != OWN
    if uses_stored:
        data.username = data.domain = data.password = ""  # kommen aus den Zugangsdaten
        errors.pop("username", None)
        errors.pop("password", None)
    if data.type is MountType.SFTP and not data.username and not uses_stored:
        errors["username"] = "SFTP braucht einen Benutzernamen."
    if data.type is not MountType.SFTP:
        data.auth = CredentialKind.PASSWORD
    if data.auth is not CredentialKind.SSH_KEY:
        data.key_id = NEW_KEY
    return data, errors


def credential_for_test(
    data: MountInput, existing: Mount | None, secrets: SecretBox, s: Session | None = None
) -> CredentialData | None:
    """Zugangsdaten für „Testen“ aus dem Formular – leeres Passwort = gespeichertes,
    gewählter Schlüssel aus der Schlüsselverwaltung."""
    if data.type in (MountType.NFS3, MountType.NFS4):
        return None
    if data.cred_id != OWN and s is not None:
        stored = s.get(Credential, int(data.cred_id))
        if stored is None or not stored.managed or stored.kind is not CredentialKind.PASSWORD:
            return None
        return CredentialData(
            CredentialKind.PASSWORD,
            stored.username,
            secrets.decrypt(stored.secret_enc),
            stored.domain,
        )
    if not data.username:
        return None
    if data.auth is CredentialKind.SSH_KEY and data.key_id != NEW_KEY and s is not None:
        key = s.get(Credential, int(data.key_id))
        if key is None or not key.managed:
            return None
        return CredentialData(data.auth, data.username, secrets.decrypt(key.secret_enc))
    secret = data.password
    stored = existing.credential if existing is not None else None
    if stored is not None and stored.managed:
        stored = None  # eigener Schlüssel gewählt, gespeicherter ist nicht gemeint
    if not secret and stored is not None and stored.kind is data.auth:
        secret = secrets.decrypt(stored.secret_enc)
    if not secret:
        return None
    return CredentialData(data.auth, data.username, secret, data.domain or None)


_CONNECTION_FIELDS = (
    "type",
    "server",
    "share",
    "port",
    "uid",
    "gid",
    "file_mode",
    "dir_mode",
    "extra_options",
)


def save_mount(
    s: Session,
    secrets: SecretBox,
    mounts: MountManager,
    data: MountInput,
    mount_id: int | None = None,
) -> tuple[Mount | None, Errors]:
    errors: Errors = {}
    m = s.get(Mount, mount_id) if mount_id is not None else None
    if mount_id is not None and m is None:
        raise AdminError("Anbindung existiert nicht.")
    dup = s.scalar(select(Mount.id).where(Mount.name == data.name))
    if dup is not None and dup != mount_id:
        errors["name"] = "Name ist bereits vergeben."
        return None, errors

    if m is None:
        base = slugify(data.name)
        slug, n = base, 1
        while s.scalar(select(Mount.id).where(Mount.slug == slug)) is not None:
            n += 1
            slug = f"{base[:44]}-{n}"
        m = Mount(slug=slug, state=MountState.UNMOUNTED, extra_options="")
        if data.type.is_kernel_mount:
            m.mountpoint = str(mounts.mountpoint_for(slug))
        s.add(m)
    else:
        changed = [f for f in _CONNECTION_FIELDS if getattr(m, f) != getattr(data, f)]
        if (
            changed
            and m.type.is_kernel_mount
            and m.state in (MountState.MOUNTED, MountState.DEGRADED)
        ):
            errors["_"] = (
                "Verbindungsdaten lassen sich nur ändern, wenn die Anbindung ausgehängt ist."
            )
            return None, errors
        if m.type is MountType.SFTP and (m.server != data.server or m.port != data.port):
            m.host_key = None  # anderer Server → Hostschlüssel neu bestätigen
        if data.type.is_kernel_mount and not m.mountpoint:
            m.mountpoint = str(mounts.mountpoint_for(m.slug))
        if not data.type.is_kernel_mount:
            m.mountpoint = None

    for f in (
        "name",
        "type",
        "server",
        "share",
        "port",
        "uid",
        "gid",
        "file_mode",
        "dir_mode",
        "extra_options",
        "auto_mount",
        "enabled",
    ):
        setattr(m, f, getattr(data, f))
    if not data.type.is_kernel_mount:
        m.auto_mount = False

    # Zugangsdaten: entweder ein gespeicherter Schlüssel aus der Schlüsselverwaltung
    # (geteilt, wird hier nie verändert) oder eigene Zugangsdaten nur dieser Anbindung.
    current = m.credential
    own = current if current is not None and not current.managed else None
    stored_password = data.cred_id != OWN and (
        data.type is MountType.CIFS
        or (data.type is MountType.SFTP and data.auth is CredentialKind.PASSWORD)
    )
    needs_cred = data.type is MountType.SFTP or (
        data.type is MountType.CIFS and (bool(data.username) or stored_password)
    )
    use_managed = (
        data.type is MountType.SFTP
        and data.auth is CredentialKind.SSH_KEY
        and data.key_id != NEW_KEY
    )
    if not needs_cred:
        m.credential = None
        m.username = None
    elif stored_password:
        stored = s.get(Credential, int(data.cred_id))
        if stored is None or not stored.managed or stored.kind is not CredentialKind.PASSWORD:
            s.rollback()
            return None, {"cred_id": "Zugangsdaten existieren nicht."}
        m.credential = stored
        m.username = None
    elif use_managed:
        key = s.get(Credential, int(data.key_id))
        if key is None or not key.managed or key.kind is not CredentialKind.SSH_KEY:
            s.rollback()
            return None, {"key_id": "Schlüssel existiert nicht."}
        m.credential = key
        m.username = data.username
    else:
        m.username = None
        cred = own
        if cred is None or cred.kind is not data.auth:
            if data.auth is CredentialKind.SSH_KEY:
                private, public = generate_ssh_keypair(f"netmover@{m.slug}")
                secret, pub = private, public
            else:
                if not data.password:
                    errors["password"] = "Passwort fehlt."  # noqa: S105
                    s.rollback()
                    return None, errors
                secret, pub = data.password, None
            if cred is None:
                cred = Credential(name=f"mount:{m.slug}", managed=False)
                s.add(cred)
            cred.kind = data.auth
            cred.secret_enc = secrets.encrypt(secret)
            cred.public_key = pub
        elif data.password and data.auth is CredentialKind.PASSWORD:
            cred.secret_enc = secrets.encrypt(data.password)
        cred.username = data.username
        cred.domain = data.domain or None
        m.credential = cred
    # Eigene Zugangsdaten, die nicht mehr gebraucht werden, entfernen.
    if own is not None and m.credential is not own:
        s.flush()
        if not own.mounts:
            s.delete(own)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        errors["_"] = "Speichern fehlgeschlagen (Name oder Mountpoint doppelt)."
        return None, errors
    return m, errors


def delete_mount(s: Session, mounts: MountManager, mount_id: int) -> None:
    m = s.get(Mount, mount_id)
    if m is None:
        return
    used = s.scalars(
        select(Rule.name).where(
            (Rule.src_mount_id == mount_id)
            | (Rule.dst_mount_id == mount_id)
            | (Rule.archive_mount_id == mount_id)
        )
    ).all()
    if used:
        raise AdminError(f"Wird noch von Regeln benutzt: {', '.join(used)}")
    if m.type.is_kernel_mount and m.state is not MountState.UNMOUNTED:
        mounts.unmount(mount_id)
    cred = m.credential
    s.delete(m)
    s.flush()
    # Eigene Zugangsdaten gehen mit; Schlüssel aus der Schlüsselverwaltung bleiben.
    if cred is not None and not cred.managed and not cred.mounts:
        s.delete(cred)


# -- Regeln ----------------------------------------------------------------------------------


@dataclass
class RuleInput:
    name: str = ""
    enabled: bool = True
    src_mount_id: int | None = None
    src_subpath: str = ""
    dst_kind: str = "mount"  # "mount" | "local"
    dst_mount_id: int | None = None
    dst_path: str = ""
    pattern: str = "*"
    pattern_type: PatternType = PatternType.GLOB
    recursive: bool = False
    interval_seconds: int = 30
    stability_seconds: int = 5
    min_age_seconds: int = 0
    max_size_mb: int | None = None
    op_timeout_seconds: int = 120
    action: RuleAction = RuleAction.MOVE
    on_collision: CollisionPolicy = CollisionPolicy.RENAME
    set_owner: str = ""
    set_mode: str = ""
    dry_run: bool = False
    quarantine_path: str = ""
    quarantine_after: int = 3
    archive_kind: ArchiveKind = ArchiveKind.NONE
    archive_mount_id: int | None = None
    archive_path: str = ""
    archive_layout: ArchiveLayout = ArchiveLayout.FLAT
    extra: dict[str, str] = field(default_factory=dict)


def rule_input_from_model(r: Rule) -> RuleInput:
    return RuleInput(
        name=r.name,
        enabled=r.enabled,
        src_mount_id=r.src_mount_id,
        src_subpath=r.src_subpath,
        dst_kind="mount" if r.dst_mount_id else "local",
        dst_mount_id=r.dst_mount_id,
        dst_path=r.dst_path,
        pattern=r.pattern,
        pattern_type=r.pattern_type,
        recursive=r.recursive,
        interval_seconds=r.interval_seconds,
        stability_seconds=r.stability_seconds,
        min_age_seconds=r.min_age_seconds,
        max_size_mb=r.max_size_mb,
        op_timeout_seconds=r.op_timeout_seconds,
        action=r.action,
        on_collision=r.on_collision,
        set_owner=r.set_owner or "",
        set_mode=r.set_mode or "",
        dry_run=r.dry_run,
        quarantine_path=r.quarantine_path or "",
        quarantine_after=r.quarantine_after,
        archive_kind=r.archive_kind,
        archive_mount_id=r.archive_mount_id,
        archive_path=r.archive_path,
        archive_layout=r.archive_layout,
    )


def parse_rule_form(s: Session, form: Mapping[str, str]) -> tuple[RuleInput, Errors]:
    errors: Errors = {}
    d = RuleInput(
        name=(form.get("name") or "").strip(),
        enabled=_bool(form, "enabled"),
        src_subpath=form.get("src_subpath") or "",
        dst_kind=form.get("dst_kind") or "mount",
        dst_path=(form.get("dst_path") or "").strip(),
        pattern=(form.get("pattern") or "").strip() or "*",
        recursive=_bool(form, "recursive"),
        set_owner=(form.get("set_owner") or "").strip(),
        set_mode=(form.get("set_mode") or "").strip(),
        dry_run=_bool(form, "dry_run"),
        quarantine_path=form.get("quarantine_path") or "",
    )
    if not d.name:
        errors["name"] = "Name fehlt."
    for key, enum_cls in (
        ("pattern_type", PatternType),
        ("action", RuleAction),
        ("on_collision", CollisionPolicy),
    ):
        try:
            setattr(d, key, enum_cls(form.get(key) or getattr(d, key).value))
        except ValueError:
            errors[key] = "Ungültiger Wert."
    d.src_mount_id = _int(form, "src_mount_id", errors)
    d.dst_mount_id = _int(form, "dst_mount_id", errors) if d.dst_kind == "mount" else None
    d.interval_seconds = (
        _int(form, "interval_seconds", errors, default=30, minimum=MIN_INTERVAL, label="Intervall")
        or 30
    )
    d.stability_seconds = _int(form, "stability_seconds", errors, default=5, minimum=0) or 0
    d.min_age_seconds = _int(form, "min_age_seconds", errors, default=0, minimum=0) or 0
    d.max_size_mb = _int(form, "max_size_mb", errors, minimum=1)
    d.op_timeout_seconds = (
        _int(form, "op_timeout_seconds", errors, default=120, minimum=10, label="Zeitlimit") or 120
    )
    d.quarantine_after = _int(form, "quarantine_after", errors, default=3, minimum=1) or 3

    src = s.get(Mount, d.src_mount_id) if d.src_mount_id else None
    if src is None:
        errors["src_mount_id"] = "Quelle wählen."
    try:
        d.src_subpath = safe_relpath("src_subpath", d.src_subpath)
    except ValidationError as exc:
        errors["src_subpath"] = str(exc)
    if d.dst_kind == "mount":
        if d.dst_mount_id is None or s.get(Mount, d.dst_mount_id) is None:
            errors["dst_mount_id"] = "Ziel wählen."
        try:
            d.dst_path = safe_relpath("dst_path", d.dst_path)
        except ValidationError as exc:
            errors["dst_path"] = str(exc)
        if (
            d.dst_mount_id == d.src_mount_id
            and "src_subpath" not in errors
            and d.dst_path == d.src_subpath
        ):
            errors["dst_path"] = "Quelle und Ziel sind derselbe Ordner."
    elif d.dst_kind == "local":
        try:
            d.dst_path = safe_abspath("dst_path", d.dst_path)
        except ValidationError as exc:
            errors["dst_path"] = str(exc)
    else:
        errors["dst_kind"] = "Ungültige Zielart."

    if d.pattern_type is PatternType.REGEX:
        try:
            re.compile(d.pattern)
        except re.error as exc:
            errors["pattern"] = f"Ungültiger regulärer Ausdruck: {exc}"
    if d.stability_seconds >= d.op_timeout_seconds:
        errors["stability_seconds"] = "Muss kleiner als das Zeitlimit sein."
    elif src is not None and src.type is MountType.SFTP and d.stability_seconds < 2:
        errors["stability_seconds"] = (
            "Bei SFTP-Quellen mindestens 2 s – der Server meldet Zeiten nur sekundengenau."
        )
    if d.set_owner and not _OWNER_RE.fullmatch(d.set_owner):
        errors["set_owner"] = "Als „uid:gid“ angeben, z. B. „1000:1000“."
    try:
        d.set_mode = validate_mode("set_mode", d.set_mode) or ""
    except ValidationError as exc:
        errors["set_mode"] = str(exc)
    if d.quarantine_path:
        try:
            d.quarantine_path = safe_relpath("quarantine_path", d.quarantine_path)
        except ValidationError as exc:
            errors["quarantine_path"] = str(exc)

    # Archiv
    try:
        d.archive_kind = ArchiveKind(form.get("archive_kind") or "none")
        d.archive_layout = ArchiveLayout(form.get("archive_layout") or "flat")
    except ValueError:
        errors["archive_kind"] = "Ungültige Auswahl."
    d.archive_path = (form.get("archive_path") or "").strip()
    if d.archive_kind is ArchiveKind.MOUNT:
        d.archive_mount_id = _int(form, "archive_mount_id", errors)
        if d.archive_mount_id is None or s.get(Mount, d.archive_mount_id) is None:
            errors["archive_mount_id"] = "Archiv-Anbindung wählen."
        try:
            d.archive_path = safe_relpath("archive_path", d.archive_path)
        except ValidationError as exc:
            errors["archive_path"] = str(exc)
        if (
            "archive_path" not in errors
            and d.archive_mount_id == d.dst_mount_id
            and d.dst_kind == "mount"
            and d.archive_path == d.dst_path
        ):
            errors["archive_path"] = "Archiv und Ziel sind derselbe Ordner."
    elif d.archive_kind is ArchiveKind.LOCAL:
        d.archive_mount_id = None
        try:
            d.archive_path = safe_abspath("archive_path", d.archive_path)
        except ValidationError as exc:
            errors["archive_path"] = str(exc)
        if d.dst_kind == "local" and d.archive_path == d.dst_path:
            errors["archive_path"] = "Archiv und Ziel sind derselbe Ordner."
    else:
        d.archive_mount_id, d.archive_path = None, ""
    return d, errors


def save_rule(s: Session, d: RuleInput, rule_id: int | None = None) -> tuple[Rule | None, Errors]:
    dup = s.scalar(select(Rule.id).where(Rule.name == d.name))
    if dup is not None and dup != rule_id:
        return None, {"name": "Name ist bereits vergeben."}
    r = s.get(Rule, rule_id) if rule_id is not None else None
    if rule_id is not None and r is None:
        raise AdminError("Regel existiert nicht.")
    if r is None:
        r = Rule()
        s.add(r)
    for f in (
        "name",
        "enabled",
        "src_mount_id",
        "src_subpath",
        "dst_mount_id",
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
        "dry_run",
        "quarantine_after",
        "archive_kind",
        "archive_mount_id",
        "archive_path",
        "archive_layout",
    ):
        setattr(r, f, getattr(d, f))
    r.set_owner = d.set_owner or None
    r.set_mode = d.set_mode or None
    r.quarantine_path = d.quarantine_path or None
    s.flush()
    return r, {}
