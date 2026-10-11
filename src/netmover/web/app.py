"""FastAPI-Anwendung: Middleware, Anmeldung, Seiten.

Seiten werden serverseitig gerendert (Jinja2). HTMX nur dort, wo es echten
Nutzen bringt: Testergebnisse, Probelauf-Tabelle, aufklappbare Protokolle und
das automatische Aktualisieren der Statusseite. Alle Aktionen funktionieren
auch als normale Formulare (Post/Redirect/Get).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import markdown
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlalchemy import func, or_, select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from netmover import __version__
from netmover.admin import (
    AdminError,
    MountInput,
    RuleInput,
    credential_for_test,
    delete_mount,
    mount_input_from_model,
    parse_mount_form,
    parse_rule_form,
    rule_input_from_model,
    save_mount,
    save_rule,
)
from netmover.app_state import NetMover
from netmover.config import Settings, get_settings
from netmover.credentials import (
    create_password,
    delete_credential,
    generate_key,
    import_key,
    list_keys,
    list_passwords,
    rename_credential,
    update_password,
)
from netmover.db import session_scope
from netmover.kuma import push as kuma_push
from netmover.logging_setup import event, get_logger
from netmover.mail import SECURITY_MODES, MailConfigError, parse_recipients, valid_address
from netmover.models import (
    CollisionPolicy,
    Credential,
    CredentialKind,
    Mount,
    MountState,
    MountType,
    PatternType,
    Rule,
    RuleAction,
    Run,
    RunItem,
    RunStatus,
    utcnow,
)
from netmover.mounting.backend import MountError
from netmover.mounting.report import CheckReport
from netmover.notify import Notification
from netmover.settings_store import AppSettings, NotifyMode, load_settings, save_settings
from netmover.transfer import apply_imported_settings, export_config, import_config
from netmover.web import auth

log = get_logger(__name__)

PAGE_SIZE = 50
STATE_LABELS = {
    MountState.MOUNTED: "eingehängt",
    MountState.UNMOUNTED: "ausgehängt",
    MountState.DEGRADED: "gestört",
    MountState.ERROR: "Fehler",
}
SFTP_STATE_LABELS = {
    MountState.MOUNTED: "erreichbar",
    MountState.UNMOUNTED: "nicht geprüft",
    MountState.DEGRADED: "gestört",
    MountState.ERROR: "nicht erreichbar",
}
RUN_LABELS = {
    RunStatus.OK: "OK",
    RunStatus.PARTIAL: "teilweise",
    RunStatus.ERROR: "Fehler",
    RunStatus.SKIPPED: "übersprungen",
    RunStatus.RUNNING: "läuft",
}


# -- Jinja-Filter -----------------------------------------------------------------------


def _ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "–"
    seconds = ((now or datetime.now(UTC)) - value).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{int(seconds)} s"
    elif seconds < 3600:
        text = f"{int(seconds // 60)} min"
    elif seconds < 86400:
        text = f"{seconds / 3600:.1f} h".replace(".", ",")
    else:
        text = f"{int(seconds // 86400)} Tg."
    return f"in {text}" if future else f"vor {text}"


def _localdt(value: datetime | None) -> str:
    return value.astimezone().strftime("%d.%m.%Y %H:%M:%S") if value else "–"


def _bytes(value: int | None) -> str:
    if value is None:
        return "–"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    n = float(value)
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}".replace(".", ",") if i else f"{int(n)} B"


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "–"
    s = (end - start).total_seconds()
    return f"{s:.1f} s".replace(".", ",") if s < 60 else f"{int(s // 60)} min {int(s % 60)} s"


TEMPLATES = Jinja2Templates(directory=str(files("netmover.web") / "templates"))
TEMPLATES.env.filters.update(ago=_ago, localdt=_localdt, bytes=_bytes)
TEMPLATES.env.globals.update(
    version=__version__,
    duration=_duration,
    STATE_LABELS=STATE_LABELS,
    SFTP_STATE_LABELS=SFTP_STATE_LABELS,
    RUN_LABELS=RUN_LABELS,
    MountType=MountType,
    MountState=MountState,
    RunStatus=RunStatus,
    PatternType=PatternType,
    RuleAction=RuleAction,
    CollisionPolicy=CollisionPolicy,
    CredentialKind=CredentialKind,
    NotifyMode=NotifyMode,
)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    # Neu zuweisen statt anhängen: die Session erkennt nur Zuweisungen als Änderung.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message]]


# Hilfe-Abschnitt passend zur Seite (Link „Hilfe zu dieser Seite“)
HELP_ANCHORS = {
    "status": "status",
    "mounts": "anbindungen",
    "keys": "zugangsdaten",
    "rules": "regeln",
    "log": "protokoll",
    "settings": "einstellungen",
}


@dataclass
class NavState:
    healthy: bool
    problems: int
    mounts: int
    mounts_bad: int
    keys: int
    rules: int
    rules_active: int
    maintenance: bool = False


def _nav_state(request: Request) -> NavState | None:
    if not _authorized(request):
        return None
    state = nm(request)
    health = state.health()
    with state.session_factory() as s:
        mounts = s.execute(select(Mount.state, Mount.enabled)).all()
        rules = s.execute(select(Rule.enabled)).all()
        keys = s.scalar(
            select(func.count()).select_from(Credential).where(Credential.managed.is_(True))
        )
    return NavState(
        healthy=health.ok,
        problems=len(health.problems),
        mounts=len(mounts),
        mounts_bad=sum(
            1 for st, en in mounts if en and st in (MountState.ERROR, MountState.DEGRADED)
        ),
        keys=keys or 0,
        rules=len(rules),
        rules_active=sum(1 for (en,) in rules if en),
        maintenance=health.maintenance,
    )


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    partial = name.startswith("partials/")
    flashes = request.session.pop("flash", []) if not partial else []
    active = ctx.get("active", "")
    if not partial:
        ctx.setdefault("nav", _nav_state(request))
        ctx.setdefault("help_anchor", HELP_ANCHORS.get(active))
    return TEMPLATES.TemplateResponse(
        request,
        name,
        {
            "csrf": auth.csrf_token(request),
            "flashes": flashes,
            "logged_in": _authorized(request),
            "session_login": auth.is_logged_in(request),
            "auth_bypass": getattr(request.state, "auth_bypass", None),
            "active": ctx.pop("active", ""),
            **ctx,
        },
        status_code=status_code,
    )


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def nm(request: Request) -> NetMover:
    app_state: NetMover = request.app.state.nm
    return app_state


def _form_dict(form: Any) -> dict[str, str]:
    return {k: v for k, v in form.items() if isinstance(v, str)}


# -- Schutz für alle geschützten Routen ---------------------------------------------------


async def guard(request: Request) -> None:
    state = nm(request)
    # Ein Passwort ist immer Pflicht – sonst ließe sich die Anmeldung nie wieder einschalten.
    if not auth.password_is_set(state.session_factory):
        raise auth.LoginRequired(setup=True)
    cfg = load_settings(state.session_factory)
    bypass = auth.login_bypass(request, cfg.auth_mode, cfg.trusted_networks, cfg.allowed_networks)
    if bypass:
        request.state.auth_bypass = bypass
    elif not auth.is_logged_in(request):
        raise auth.LoginRequired()
    await auth.check_csrf(request)


def _authorized(request: Request) -> bool:
    return auth.is_logged_in(request) or bool(getattr(request.state, "auth_bypass", None))


protected = APIRouter(dependencies=[Depends(guard)])
public = APIRouter()


# -- Öffentlich: Health, Einrichtung, Anmeldung --------------------------------------------


@public.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Für Uptime Kuma & Co.: 200 = alles in Ordnung, 503 = mindestens ein Problem.
    Ohne Anmeldung erreichbar; enthält Namen und Fehlermeldungen, keine Geheimnisse."""
    state = nm(request)
    health = await run_in_threadpool(state.health)
    beat = state.scheduler.last_heartbeat
    body = {
        "status": "ok" if health.ok else "error",
        "version": __version__,
        "problems": health.problems,
        "scheduler": {
            "running": state.scheduler.running,
            "last_heartbeat": beat.isoformat() if beat else None,
        },
        "checks": [c.as_dict() for c in health.checks],
    }
    return JSONResponse(body, status_code=200 if health.ok else 503)


async def _single_check(request: Request, kind: str, **key: Any) -> JSONResponse:
    health = await run_in_threadpool(nm(request).health)
    check = health.find(kind, **key)
    if check is None:
        return JSONResponse({"status": "unknown", "message": "nicht gefunden"}, status_code=404)
    body = {"status": "ok" if check.ok else "error", "name": check.name, "message": check.message}
    return JSONResponse(body, status_code=200 if check.ok else 503)


@public.get("/healthz/mount/{slug}")
async def healthz_mount(request: Request, slug: str) -> JSONResponse:
    """Eigener Kuma-Monitor pro Anbindung."""
    return await _single_check(request, "mount", slug=slug)


@public.get("/healthz/rule/{rule_id}")
async def healthz_rule(request: Request, rule_id: int) -> JSONResponse:
    """Eigener Kuma-Monitor pro Regel."""
    return await _single_check(request, "rule", id=rule_id)


@public.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request) -> Response:
    if auth.password_is_set(nm(request).session_factory):
        return redirect("/login")
    return render(request, "setup.html")


@public.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request) -> Response:
    await auth.check_csrf(request)
    state = nm(request)
    if auth.password_is_set(state.session_factory):
        return redirect("/login")
    form = await request.form()
    password = str(form.get("password") or "")
    problem = auth.password_problem(password, str(form.get("confirm") or ""))
    if not request.app.state.setup_code.matches(str(form.get("code") or "")):
        problem = (
            "Einrichtungscode stimmt nicht – er steht im Log des Dienstes (journalctl -u netmover)."
        )
    if problem:
        return render(request, "setup.html", status_code=400, error=problem)
    await run_in_threadpool(auth.set_password, state.session_factory, password)
    auth.login_session(request)
    event(log, "auth.setup_done")
    flash(request, "Passwort gesetzt. Willkommen bei NetMover!")
    return redirect("/")


@public.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    state = nm(request)
    if not auth.password_is_set(state.session_factory):
        return redirect("/setup")
    cfg = load_settings(state.session_factory)
    if auth.login_bypass(request, cfg.auth_mode, cfg.trusted_networks, cfg.allowed_networks):
        return redirect("/")
    return render(request, "login.html")


@public.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    await auth.check_csrf(request)
    throttle: auth.LoginThrottle = request.app.state.throttle
    client = request.client.host if request.client else "?"
    wait = throttle.locked_for(client)
    if wait:
        return render(
            request,
            "login.html",
            status_code=429,
            error=f"Zu viele Fehlversuche – bitte {math.ceil(wait / 60)} min warten.",
        )
    form = await request.form()
    ok = await run_in_threadpool(
        auth.verify_password, nm(request).session_factory, str(form.get("password") or "")
    )
    if not ok:
        throttle.failure(client)
        event(log, "auth.login_failed", level=30, client=client)
        return render(request, "login.html", status_code=401, error="Passwort falsch.")
    throttle.success(client)
    auth.login_session(request)
    event(log, "auth.login", client=client)
    target = request.query_params.get("next", "/")
    return redirect(target if target.startswith("/") and not target.startswith("//") else "/")


@protected.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return redirect("/login")


# -- Status -----------------------------------------------------------------------------------


@dataclass
class MountView:
    m: Mount
    usage: tuple[int, int] | None


@dataclass
class RuleView:
    r: Rule
    last_run: Run | None
    next_run: datetime | None
    running_since: datetime | None
    stale: bool
    target: str


def _archive_target(r: Rule) -> str | None:
    if r.archive_kind.value == "none":
        return None
    if r.archive_kind.value == "local" or r.archive_mount is None:
        place = r.archive_path
    else:
        place = f"{r.archive_mount.name}:/{r.archive_path}"
    return place + (" (nach Datum)" if r.archive_layout.value == "date" else "")


def _rule_target(r: Rule) -> str:
    src = f"{r.src_mount.name}:/{r.src_subpath}" if r.src_subpath else f"{r.src_mount.name}:/"
    if r.dst_mount is None:
        dst = r.dst_path
    else:
        dst = f"{r.dst_mount.name}:/{r.dst_path}" if r.dst_path else f"{r.dst_mount.name}:/"
    archive = _archive_target(r)
    return f"{src} → {dst}" + (f" + Archiv {archive}" if archive else "")


async def _status_context(request: Request) -> dict[str, Any]:
    state = nm(request)
    health = await run_in_threadpool(state.health)
    with state.session_factory() as s:
        mounts = s.scalars(select(Mount).order_by(Mount.name)).all()
        rules = s.scalars(select(Rule).order_by(Rule.name)).all()
        last_runs = {
            run.rule_id: run
            for run in s.scalars(
                select(Run).where(
                    Run.id.in_(
                        select(func.max(Run.id))
                        .where(Run.status != RunStatus.SKIPPED)
                        .group_by(Run.rule_id)
                    )
                )
            )
        }
        rule_views = [
            RuleView(
                r,
                last_runs.get(r.id),
                state.scheduler.next_run(r.id),
                state.runner.running_since(r.id),
                r.id in health.stale_rules,
                _rule_target(r),
            )
            for r in rules
        ]
        for m in mounts:
            s.expunge(m)

    async def usage(m: Mount) -> tuple[int, int] | None:
        if m.state is not MountState.MOUNTED or not m.type.is_kernel_mount:
            return None
        return await run_in_threadpool(state.mounts.usage, m.id, 2.0)

    usages = await asyncio.gather(*(usage(m) for m in mounts))
    return {
        "health": health,
        "mounts": [MountView(m, u) for m, u in zip(mounts, usages, strict=True)],
        "rules": rule_views,
        "now": utcnow(),
    }


@protected.get("/", response_class=HTMLResponse)
async def status_page(request: Request) -> Response:
    return render(request, "status.html", active="status", **await _status_context(request))


@protected.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request) -> Response:
    return render(request, "partials/status_body.html", **await _status_context(request))


@protected.post("/maintenance")
async def toggle_maintenance(request: Request) -> Response:
    state = nm(request)
    cfg = load_settings(state.session_factory)
    cfg.maintenance = not cfg.maintenance
    save_settings(state.session_factory, cfg)
    event(log, "maintenance", enabled=cfg.maintenance)
    flash(
        request,
        "Wartungsmodus eingeschaltet – geplante Läufe sind pausiert."
        if cfg.maintenance
        else "Wartungsmodus beendet – Regeln laufen wieder nach Plan.",
        "info",
    )
    return redirect("/")


@protected.post("/rules/{rule_id}/run")
async def rule_run_now(request: Request, rule_id: int) -> Response:
    state = nm(request)
    task = asyncio.create_task(state.scheduler.run_now(rule_id))
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)
    flash(request, "Lauf gestartet – das Ergebnis erscheint gleich im Protokoll.")
    return redirect(request.headers.get("referer") or "/")


# -- Anbindungen ----------------------------------------------------------------------------


@protected.get("/mounts", response_class=HTMLResponse)
async def mounts_page(request: Request) -> Response:
    with nm(request).session_factory() as s:
        mounts = s.scalars(select(Mount).order_by(Mount.name)).all()
        reports = {
            m.id: CheckReport(**_report_data(m.last_test_result))
            for m in mounts
            if m.last_test_result
        }
        pubkeys = {
            m.id: m.credential.public_key
            for m in mounts
            if m.credential is not None and m.credential.public_key and not m.credential.managed
        }
        key_names = {
            m.id: m.credential.name
            for m in mounts
            if m.credential is not None and m.credential.managed
        }  # Name gespeicherter Zugangsdaten (Passwort oder Schlüssel)
        return render(
            request,
            "mounts.html",
            active="mounts",
            mounts=mounts,
            reports=reports,
            pubkeys=pubkeys,
            key_names=key_names,
        )


def _report_data(raw: str | None) -> dict[str, Any]:
    from netmover.mounting.report import CheckStep

    data: dict[str, Any] = json.loads(raw or "{}")
    data.pop("roles", None)
    data.pop("ok", None)
    data["steps"] = [CheckStep(**st) for st in data.get("steps", [])]
    return data


def _password_options(request: Request) -> list[tuple[str, str]]:
    with nm(request).session_factory() as s:
        stored = list_passwords(s)
    return [("own", "eigene Angaben für diese Anbindung")] + [
        (str(p.id), f"{p.name} ({p.domain + chr(92) if p.domain else ''}{p.username})")
        for p in stored
    ]


def _key_options(request: Request) -> list[tuple[str, str]]:
    with nm(request).session_factory() as s:
        keys = list_keys(s)
    return [("new", "neuer Schlüssel nur für diese Anbindung")] + [
        (str(k.id), f"{k.name} ({k.key_type.removeprefix('ssh-')})") for k in keys
    ]


def _mount_form(
    request: Request,
    data: MountInput,
    errors: dict[str, str],
    mount: Mount | None = None,
    status_code: int = 200,
) -> Response:
    pub = (
        mount.credential.public_key
        if mount is not None and mount.credential and not mount.credential.managed
        else None
    )
    return render(
        request,
        "mount_form.html",
        status_code=status_code,
        active="mounts",
        d=data,
        errors=errors,
        mount=mount,
        public_key=pub,
        has_secret=bool(
            mount is not None and mount.credential is not None and not mount.credential.managed
        ),
        key_options=_key_options(request),
        password_options=_password_options(request),
    )


@protected.get("/mounts/new", response_class=HTMLResponse)
async def mount_new(request: Request) -> Response:
    return _mount_form(request, MountInput(), {})


@protected.get("/mounts/{mount_id}/edit", response_class=HTMLResponse)
async def mount_edit(request: Request, mount_id: int) -> Response:
    with nm(request).session_factory() as s:
        m = s.get(Mount, mount_id)
        if m is None:
            return redirect("/mounts")
        return _mount_form(request, mount_input_from_model(m), {}, m)


@protected.post("/mounts/new", response_class=HTMLResponse)
@protected.post("/mounts/{mount_id}/edit", response_class=HTMLResponse)
async def mount_save(request: Request, mount_id: int | None = None) -> Response:
    state = nm(request)
    data, errors = parse_mount_form(_form_dict(await request.form()))
    with session_scope(state.session_factory) as s:
        existing = s.get(Mount, mount_id) if mount_id is not None else None
        if not errors:
            try:
                m, errors = save_mount(s, state.secrets, state.mounts, data, mount_id)
            except AdminError as exc:
                errors = {"_": str(exc)}
                m = None
        else:
            m = None
        if errors or m is None:
            s.rollback()
            existing = s.get(Mount, mount_id) if mount_id is not None else None
            return _mount_form(request, data, errors, existing, status_code=400)
        saved_id, name = m.id, m.name
        is_sftp_key = m.type is MountType.SFTP and data.auth is CredentialKind.SSH_KEY
    flash(request, f"Anbindung „{name}“ gespeichert.")
    if is_sftp_key:
        flash(
            request,
            "Öffentlichen Schlüssel beim Zielserver in authorized_keys eintragen, dann „Testen“.",
            "info",
        )
    state.scheduler.sync()
    return redirect(f"/mounts#mount-{saved_id}")


@protected.post("/mounts/test", response_class=HTMLResponse)
async def mount_test_form(request: Request) -> Response:
    """„Testen“ im Formular – mit den (noch nicht gespeicherten) Eingaben."""
    state = nm(request)
    form = _form_dict(await request.form())
    data, errors = parse_mount_form(form)
    if errors:
        return render(request, "partials/check_result.html", errors=errors, report=None)
    existing_id = int(form["mount_id"]) if form.get("mount_id", "").isdigit() else None
    with state.session_factory() as s:
        existing = s.get(Mount, existing_id) if existing_id else None
        cred = credential_for_test(data, existing, state.secrets, s)
    cfg = data.config()
    if data.type is MountType.SFTP and existing is not None:
        cfg = dataclasses.replace(cfg, host_key=existing.host_key)
    report = await run_in_threadpool(state.mounts.check, cfg, cred, None)
    return render(request, "partials/check_result.html", report=report, errors={})


@protected.post("/mounts/{mount_id}/test", response_class=HTMLResponse)
async def mount_test(request: Request, mount_id: int) -> Response:
    state = nm(request)
    with state.session_factory() as s:
        m = s.get(Mount, mount_id)
        if m is None:
            return HTMLResponse("Anbindung existiert nicht.", status_code=404)
        data = mount_input_from_model(m)
        cred = credential_for_test(data, m, state.secrets, s)
        from netmover.mounting.manager import MountConfig

        cfg = MountConfig.from_model(m)
    report = await run_in_threadpool(state.mounts.check, cfg, cred, mount_id)
    return render(request, "partials/check_result.html", report=report, errors={})


async def _mount_action(
    request: Request, mount_id: int, fn: Callable[[int], None], ok: str
) -> Response:
    try:
        await run_in_threadpool(fn, mount_id)
        flash(request, ok)
    except (MountError, LookupError) as exc:
        flash(request, str(exc), "error")
    return redirect(f"/mounts#mount-{mount_id}")


@protected.post("/mounts/{mount_id}/mount")
async def mount_mount(request: Request, mount_id: int) -> Response:
    return await _mount_action(request, mount_id, nm(request).mounts.mount, "Eingehängt.")


@protected.post("/mounts/{mount_id}/unmount")
async def mount_unmount(request: Request, mount_id: int) -> Response:
    return await _mount_action(request, mount_id, nm(request).mounts.unmount, "Ausgehängt.")


@protected.post("/mounts/{mount_id}/delete")
async def mount_delete(request: Request, mount_id: int) -> Response:
    state = nm(request)

    def work() -> None:
        with session_scope(state.session_factory) as s:
            delete_mount(s, state.mounts, mount_id)

    try:
        await run_in_threadpool(work)
        flash(request, "Anbindung gelöscht.")
    except (AdminError, MountError) as exc:
        flash(request, str(exc), "error")
    return redirect("/mounts")


@protected.post("/mounts/{mount_id}/reset-hostkey")
async def mount_reset_hostkey(request: Request, mount_id: int) -> Response:
    with session_scope(nm(request).session_factory) as s:
        m = s.get(Mount, mount_id)
        if m is not None:
            m.host_key = None
            m.state = MountState.UNMOUNTED
    flash(
        request,
        "Hostschlüssel zurückgesetzt – beim nächsten „Testen“ wird er neu festgehalten.",
        "info",
    )
    return redirect(f"/mounts#mount-{mount_id}")


# -- Zugangsdaten ------------------------------------------------------------------------------


@protected.get("/credentials", response_class=HTMLResponse)
async def credentials_page(request: Request) -> Response:
    with nm(request).session_factory() as s:
        return render(
            request,
            "credentials.html",
            active="keys",
            keys=list_keys(s),
            passwords=list_passwords(s),
        )


@protected.get("/keys")
async def keys_redirect(request: Request) -> Response:
    return RedirectResponse("/credentials", status_code=301)


async def _cred_action(request: Request, work: Callable[[Any], str]) -> Response:
    state = nm(request)
    try:
        with session_scope(state.session_factory) as s:
            flash(request, work(s))
    except AdminError as exc:
        flash(request, str(exc), "error")
    return redirect("/credentials")


@protected.post("/credentials/passwords")
async def password_create(request: Request) -> Response:
    form = _form_dict(await request.form())
    secrets = nm(request).secrets

    def work(s: Any) -> str:
        cred = create_password(
            s,
            secrets,
            form.get("name", ""),
            form.get("username", ""),
            form.get("password", ""),
            form.get("domain", ""),
        )
        return f"Zugangsdaten „{cred.name}“ gespeichert."

    return await _cred_action(request, work)


@protected.post("/credentials/{cred_id}/password")
async def password_update(request: Request, cred_id: int) -> Response:
    form = _form_dict(await request.form())
    secrets = nm(request).secrets

    def work(s: Any) -> str:
        cred = update_password(
            s,
            secrets,
            cred_id,
            form.get("username", ""),
            form.get("password", ""),
            form.get("domain", ""),
        )
        return f"Zugangsdaten „{cred.name}“ geändert – gilt ab sofort für alle Anbindungen."

    return await _cred_action(request, work)


@protected.post("/credentials/keys/generate")
async def key_generate(request: Request) -> Response:
    form = _form_dict(await request.form())
    secrets = nm(request).secrets

    def work(s: Any) -> str:
        key = generate_key(s, secrets, form.get("name", ""))
        return f"Schlüssel „{key.name}“ erzeugt."

    return await _cred_action(request, work)


@protected.post("/credentials/keys/import")
async def key_import(request: Request) -> Response:
    form = _form_dict(await request.form())
    secrets = nm(request).secrets

    def work(s: Any) -> str:
        key = import_key(
            s,
            secrets,
            form.get("name", ""),
            form.get("private_key", ""),
            form.get("passphrase", ""),
        )
        return f"Schlüssel „{key.name}“ importiert."

    return await _cred_action(request, work)


@protected.post("/credentials/{cred_id}/rename")
async def credential_rename(request: Request, cred_id: int) -> Response:
    form = _form_dict(await request.form())

    def work(s: Any) -> str:
        rename_credential(s, cred_id, form.get("name", ""))
        return "Umbenannt."

    return await _cred_action(request, work)


@protected.post("/credentials/{cred_id}/delete")
async def credential_delete(request: Request, cred_id: int) -> Response:
    def work(s: Any) -> str:
        return f"„{delete_credential(s, cred_id)}“ gelöscht."

    return await _cred_action(request, work)


# -- Regeln -----------------------------------------------------------------------------------


def _rule_form(
    request: Request,
    d: RuleInput,
    errors: dict[str, str],
    rule_id: int | None,
    status_code: int = 200,
) -> Response:
    with nm(request).session_factory() as s:
        mounts = s.scalars(select(Mount).order_by(Mount.name)).all()
        for m in mounts:
            s.expunge(m)
    return render(
        request,
        "rule_form.html",
        status_code=status_code,
        active="rules",
        d=d,
        errors=errors,
        rule_id=rule_id,
        src_mounts=mounts,
        src_options=[("", "– wählen –")] + [(m.id, f"{m.name} ({m.type.value})") for m in mounts],
        dst_options=[("", "– wählen –")] + [(m.id, f"{m.name} ({m.type.value})") for m in mounts],
    )


@protected.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request) -> Response:
    ctx = await _status_context(request)
    return render(request, "rules.html", active="rules", rules=ctx["rules"], now=ctx["now"])


@protected.get("/rules/new", response_class=HTMLResponse)
async def rule_new(request: Request) -> Response:
    return _rule_form(request, RuleInput(), {}, None)


@protected.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
async def rule_edit(request: Request, rule_id: int) -> Response:
    with nm(request).session_factory() as s:
        r = s.get(Rule, rule_id)
        if r is None:
            return redirect("/rules")
        d = rule_input_from_model(r)
    return _rule_form(request, d, {}, rule_id)


@protected.post("/rules/new", response_class=HTMLResponse)
@protected.post("/rules/{rule_id}/edit", response_class=HTMLResponse)
async def rule_save(request: Request, rule_id: int | None = None) -> Response:
    state = nm(request)
    form = _form_dict(await request.form())
    with session_scope(state.session_factory) as s:
        d, errors = parse_rule_form(s, form)
        r = None
        if not errors:
            try:
                r, errors = save_rule(s, d, rule_id)
            except AdminError as exc:
                errors = {"_": str(exc)}
        if errors or r is None:
            s.rollback()
            return _rule_form(request, d, errors, rule_id, status_code=400)
        name = r.name
    state.scheduler.sync()
    flash(request, f"Regel „{name}“ gespeichert.")
    return redirect("/rules")


@protected.post("/rules/{rule_id}/toggle")
async def rule_toggle(request: Request, rule_id: int) -> Response:
    state = nm(request)
    with session_scope(state.session_factory) as s:
        r = s.get(Rule, rule_id)
        if r is not None:
            r.enabled = not r.enabled
            flash(request, f"Regel „{r.name}“ {'aktiviert' if r.enabled else 'deaktiviert'}.")
    state.scheduler.sync()
    return redirect("/rules")


@protected.post("/rules/{rule_id}/delete")
async def rule_delete(request: Request, rule_id: int) -> Response:
    state = nm(request)
    with session_scope(state.session_factory) as s:
        r = s.get(Rule, rule_id)
        if r is not None:
            s.delete(r)
            flash(request, f"Regel „{r.name}“ gelöscht.")
    state.scheduler.sync()
    return redirect("/rules")


@protected.post("/rules/{rule_id}/dryrun", response_class=HTMLResponse)
async def rule_dryrun(request: Request, rule_id: int) -> Response:
    state = nm(request)
    run_id = await state.scheduler.run_now(rule_id, dry_run=True)
    with state.session_factory() as s:
        run = s.get(Run, run_id) if run_id else None
        items = s.scalars(select(RunItem).where(RunItem.run_id == run_id)).all() if run_id else []
        return render(request, "partials/dryrun.html", run=run, items=items)


# -- Protokoll --------------------------------------------------------------------------------


@protected.get("/log", response_class=HTMLResponse)
async def log_page(request: Request) -> Response:
    q = request.query_params
    rule_id = int(q["rule"]) if q.get("rule", "").isdigit() else None
    status = q.get("status") or ""
    active_only = q.get("all") != "1"
    page = max(1, int(q["page"])) if q.get("page", "").isdigit() else 1
    with nm(request).session_factory() as s:
        stmt = select(Run, Rule.name).join(Rule, Rule.id == Run.rule_id)
        if rule_id:
            stmt = stmt.where(Run.rule_id == rule_id)
        if status in {st.value for st in RunStatus}:
            stmt = stmt.where(Run.status == RunStatus(status))
        if active_only:
            stmt = stmt.where(
                or_(Run.seen > 0, Run.status != RunStatus.OK, Run.message.is_not(None))
            )
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.execute(
            stmt.order_by(Run.started_at.desc(), Run.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()
        rules = s.execute(select(Rule.id, Rule.name).order_by(Rule.name)).all()
        return render(
            request,
            "log.html",
            active="log",
            rows=rows,
            rules=rules,
            rule_id=rule_id,
            status=status,
            active_only=active_only,
            page=page,
            pages=max(1, math.ceil(total / PAGE_SIZE)),
            total=total,
        )


@protected.get("/log/{run_id}/items", response_class=HTMLResponse)
async def log_items(request: Request, run_id: int) -> Response:
    with nm(request).session_factory() as s:
        items = s.scalars(
            select(RunItem).where(RunItem.run_id == run_id).order_by(RunItem.id)
        ).all()
        run = s.get(Run, run_id)
        return render(request, "partials/run_items.html", items=items, run=run)


# -- Hilfe --------------------------------------------------------------------------------------

REPO_URL = "https://git.wilde-server.de/florian.wilde/netmover"
_IMG_NAME = re.compile(r"^[a-z0-9_-]+\.png$")


def _help_dir() -> Path:
    """Im installierten Paket mitgeliefert (siehe pyproject), sonst docs/ im Repo."""
    packaged = Path(str(files("netmover.web") / "help"))
    if (packaged / "ANLEITUNG.md").is_file():
        return packaged
    return Path(__file__).resolve().parents[3] / "docs"


@lru_cache(maxsize=1)
def _help_html() -> str:
    text = (_help_dir() / "ANLEITUNG.md").read_text(encoding="utf-8")
    html = markdown.markdown(text, extensions=["tables", "attr_list", "fenced_code"])
    html = html.replace('src="img/', 'src="/help/img/')
    return html.replace(
        'href="../README.md"', f'href="{REPO_URL}#readme" target="_blank" rel="noopener"'
    )


@protected.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> Response:
    return render(request, "help.html", active="help", content=_help_html())


@protected.get("/help/img/{name}")
async def help_image(request: Request, name: str) -> Response:
    path = _help_dir() / "img" / name
    if not _IMG_NAME.fullmatch(name) or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/png")


# -- Einstellungen ------------------------------------------------------------------------------


def _monitor_urls(request: Request) -> list[tuple[str, str]]:
    """Adressen für Uptime-Kuma-HTTP-Monitore (Gesamtzustand und Einzel-Endpunkte)."""
    base = str(request.base_url).rstrip("/")
    urls = [("Gesamtzustand", f"{base}/healthz")]
    with nm(request).session_factory() as s:
        urls += [
            (f"Anbindung „{n}“", f"{base}/healthz/mount/{slug}")
            for n, slug in s.execute(select(Mount.name, Mount.slug).order_by(Mount.name))
        ]
        urls += [
            (f"Regel „{n}“", f"{base}/healthz/rule/{rid}")
            for rid, n in s.execute(select(Rule.id, Rule.name).order_by(Rule.name))
        ]
    return urls


@protected.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    cfg = load_settings(nm(request).session_factory)
    return render(
        request,
        "settings.html",
        active="settings",
        cfg=cfg,
        errors={},
        monitor_urls=_monitor_urls(request),
        client_ip=request.client.host if request.client else "",
        loopback_trusted=auth.includes_loopback(cfg.trusted_networks),
    )


@protected.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request) -> Response:
    state = nm(request)
    form = _form_dict(await request.form())
    cfg = load_settings(state.session_factory)
    errors: dict[str, str] = {}
    try:
        cfg.retention_days = int(form.get("retention_days") or 30)
        if not 1 <= cfg.retention_days <= 3650:
            errors["retention_days"] = "Zwischen 1 und 3650 Tagen."
        cfg.notify_after = int(form.get("notify_after") or 3)
        if cfg.notify_after < 1:
            errors["notify_after"] = "Mindestens 1."
    except ValueError:
        errors["retention_days"] = "Bitte ganze Zahlen angeben."
    try:
        cfg.notify_mode = NotifyMode(form.get("notify_mode") or "off")
    except ValueError:
        errors["notify_mode"] = "Ungültig."
    url = (form.get("webhook_url") or "").strip()
    if url != auth_mask(cfg.webhook_url):  # Maske unverändert = URL behalten
        if url and not url.startswith(("http://", "https://")):
            errors["webhook_url"] = "URL muss mit http:// oder https:// beginnen."
        cfg.webhook_url = url
    cfg.email_enabled = form.get("email_enabled") == "on"
    cfg.smtp_host = (form.get("smtp_host") or "").strip()
    cfg.smtp_user = (form.get("smtp_user") or "").strip()
    cfg.smtp_from = (form.get("smtp_from") or "").strip()
    cfg.smtp_to = (form.get("smtp_to") or "").strip()
    cfg.smtp_security = form.get("smtp_security") or "starttls"
    try:
        cfg.smtp_port = int(form.get("smtp_port") or 587)
        if not 1 <= cfg.smtp_port <= 65535:
            raise ValueError
    except ValueError:
        errors["smtp_port"] = "Port zwischen 1 und 65535."
    if cfg.smtp_security not in SECURITY_MODES:
        errors["smtp_security"] = "Ungültig."
    if cfg.smtp_host and not re.fullmatch(r"[A-Za-z0-9.-]+", cfg.smtp_host):
        errors["smtp_host"] = "Hostname oder IP-Adresse."
    if cfg.smtp_from and not valid_address(cfg.smtp_from):
        errors["smtp_from"] = "Keine gültige E-Mail-Adresse."
    try:
        parse_recipients(cfg.smtp_to)
    except MailConfigError as exc:
        errors["smtp_to"] = str(exc)
    if cfg.email_enabled and not (cfg.smtp_host and cfg.smtp_from and cfg.smtp_to):
        errors["smtp_host"] = "Für E-Mail werden Server, Absender und Empfänger benötigt."
    if form.get("smtp_password"):
        cfg.smtp_password_enc = state.secrets.encrypt(form["smtp_password"])
    kuma = (form.get("kuma_push_url") or "").strip()
    if kuma != auth_mask(cfg.kuma_push_url):
        if kuma and not kuma.startswith(("http://", "https://")):
            errors["kuma_push_url"] = "URL muss mit http:// oder https:// beginnen."
        cfg.kuma_push_url = kuma
    cfg.notify_recovery = form.get("notify_recovery") == "on"
    cfg.webhook_template = form.get("webhook_template") or cfg.webhook_template
    cfg.webhook_content_type = (form.get("webhook_content_type") or "application/json").strip()
    if errors:
        return render(
            request, "settings.html", status_code=400, active="settings", cfg=cfg, errors=errors
        )
    save_settings(state.session_factory, cfg)
    flash(request, "Einstellungen gespeichert.")
    return redirect("/settings")


def auth_mask(url: str) -> str:
    """Webhook-URL nur gekürzt anzeigen – sie enthält oft ein Token."""
    if not url:
        return ""
    head, _, _rest = url.partition("://")
    host = _rest.split("/", 1)[0]
    return f"{head}://{host}/••••••"


TEMPLATES.env.globals["mask_url"] = auth_mask


@protected.post("/settings/webhook-test", response_class=HTMLResponse)
async def settings_webhook_test(request: Request) -> Response:
    state = nm(request)
    cfg = load_settings(state.session_factory)
    if not cfg.webhook_url:
        return HTMLResponse('<p class="msg error">Keine Webhook-URL gespeichert.</p>')
    from netmover.notify import http_sender
    from netmover.notify import render as render_body

    body = render_body(
        cfg.webhook_template,
        cfg.webhook_content_type,
        Notification("test", "NetMover: Testnachricht", "Webhook funktioniert."),
    )
    try:
        await run_in_threadpool(http_sender, cfg.webhook_url, cfg.webhook_content_type, body)
    except Exception as exc:
        return HTMLResponse(f'<p class="msg error">Fehlgeschlagen: {type(exc).__name__}</p>')
    return HTMLResponse('<p class="msg ok">Testnachricht gesendet.</p>')


@protected.post("/settings/mail-test", response_class=HTMLResponse)
async def settings_mail_test(request: Request) -> Response:
    state = nm(request)
    cfg = load_settings(state.session_factory)
    if not (cfg.smtp_host and cfg.smtp_from and cfg.smtp_to):
        return HTMLResponse('<p class="msg error">Server, Absender und Empfänger speichern.</p>')
    try:
        await run_in_threadpool(
            state.notifier.send_mail_now,
            "NetMover: Testnachricht",
            "Die E-Mail-Benachrichtigung funktioniert.",
            cfg,
        )
    except Exception as exc:
        detail = escape(str(exc))[:300] or type(exc).__name__
        return HTMLResponse(f'<p class="msg error">Fehlgeschlagen: {detail}</p>')
    return HTMLResponse(f'<p class="msg ok">Gesendet an {escape(cfg.smtp_to)}.</p>')


@protected.post("/settings/kuma-test", response_class=HTMLResponse)
async def settings_kuma_test(request: Request) -> Response:
    state = nm(request)
    url = load_settings(state.session_factory).kuma_push_url
    if not url:
        return HTMLResponse('<p class="msg error">Keine Push-URL gespeichert.</p>')
    ok = await run_in_threadpool(kuma_push, url, await run_in_threadpool(state.health))
    if not ok:
        return HTMLResponse('<p class="msg error">Kuma nicht erreichbar oder URL falsch.</p>')
    return HTMLResponse(
        '<p class="msg ok">Gemeldet – in Kuma sollte der Monitor jetzt grün/rot sein.</p>'
    )


@protected.post("/settings/password", response_class=HTMLResponse)
async def settings_password(request: Request) -> Response:
    state = nm(request)
    form = _form_dict(await request.form())
    if not await run_in_threadpool(
        auth.verify_password, state.session_factory, form.get("current", "")
    ):
        flash(request, "Aktuelles Passwort falsch.", "error")
        return redirect("/settings")
    problem = auth.password_problem(form.get("password", ""), form.get("confirm", ""))
    if problem:
        flash(request, problem, "error")
        return redirect("/settings")
    await run_in_threadpool(auth.set_password, state.session_factory, form["password"])
    auth.login_session(request)
    flash(request, "Passwort geändert.")
    return redirect("/settings")


@protected.post("/settings/auth", response_class=HTMLResponse)
async def settings_auth(request: Request) -> Response:
    """Netze ohne / mit Passwort festlegen. Nur mit dem aktuellen Passwort und nie so,
    dass man sich mit der eigenen Adresse aussperrt."""
    state = nm(request)
    form = _form_dict(await request.form())
    back = redirect("/settings#anmeldung")
    if not await run_in_threadpool(
        auth.verify_password, state.session_factory, form.get("current", "")
    ):
        flash(request, "Aktuelles Passwort falsch – Anmeldung unverändert.", "error")
        return back
    trusted = (form.get("trusted_networks") or "").strip()
    allowed = (form.get("allowed_networks") or "").strip()
    try:
        auth.parse_networks(trusted)
        auth.parse_networks(allowed)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return back
    me = auth.client_host(request)
    if auth.access_decision(me, auth.AUTH_ALWAYS, trusted, allowed) == auth.ACCESS_DENY:
        flash(
            request,
            f"Nicht gespeichert: Damit wärst du selbst ausgesperrt (deine Adresse {me}). "
            "Trage dein Netz bei „mit“ oder „ohne Passwort“ ein.",
            "error",
        )
        return back
    cfg = load_settings(state.session_factory)
    cfg.trusted_networks, cfg.allowed_networks = trusted, allowed
    cfg.auth_mode = auth.AUTH_TRUSTED if trusted else auth.AUTH_ALWAYS
    save_settings(state.session_factory, cfg)
    event(
        log,
        "auth.access_changed",
        level=30,
        without_password=trusted,
        with_password=allowed or "alle übrigen",
    )
    parts = [f"ohne Passwort aus: {trusted}" if trusted else "überall mit Passwort"]
    parts.append(
        f"mit Passwort nur aus: {allowed} – alle anderen Netze abgewiesen"
        if allowed
        else "alle übrigen Netze mit Passwort"
    )
    flash(
        request, "Anmeldung gespeichert: " + "; ".join(parts) + ".", "warn" if trusted else "info"
    )
    return back


@protected.get("/settings/export")
async def settings_export(request: Request) -> Response:
    state = nm(request)
    with state.session_factory() as s:
        data = export_config(s, load_settings(state.session_factory))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="netmover-{stamp}.json"'},
    )


@protected.post("/settings/import")
async def settings_import(request: Request) -> Response:
    state = nm(request)
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        flash(request, "Keine Datei ausgewählt.", "error")
        return redirect("/settings")
    raw = await upload.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        flash(request, "Datei zu groß.", "error")
        return redirect("/settings")
    try:
        data = json.loads(raw)
    except ValueError:
        flash(request, "Keine gültige JSON-Datei.", "error")
        return redirect("/settings")
    with session_scope(state.session_factory) as s:
        report = import_config(s, state.mounts, data)
    if report.settings:
        save_settings(
            state.session_factory,
            apply_imported_settings(load_settings(state.session_factory), report.settings),
        )
    state.scheduler.sync()
    if report.created:
        flash(request, "Importiert: " + ", ".join(report.created))
    if report.skipped:
        flash(request, "Übersprungen: " + ", ".join(report.skipped), "info")
    for err in report.errors:
        flash(request, err, "error")
    if report.created:
        flash(
            request,
            "Zugangsdaten bitte bei den Anbindungen neu eingeben; importierte Regeln "
            "sind deaktiviert.",
            "info",
        )
    return redirect("/settings")


# -- Anwendung ----------------------------------------------------------------------------------


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(settings: Settings | None = None, state: NetMover | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app_state = state or NetMover(settings)
        app.state.nm = app_state
        app.state.setup_code = auth.SetupCode()
        app.state.throttle = auth.LoginThrottle()
        app.state.tasks = set()
        if not auth.password_is_set(app_state.session_factory):
            event(
                log,
                "setup.required",
                level=30,
                code=app.state.setup_code.code,
                hint="Diesen Code bei der Ersteinrichtung im Browser eingeben",
            )
        app_state.start()
        try:
            yield
        finally:
            app_state.stop()

    app = FastAPI(
        title="NetMover",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def network_access(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not (path.startswith("/healthz") or path.startswith("/static/")):
            app_state = getattr(request.app.state, "nm", None)
            if app_state is not None:
                cfg = load_settings(app_state.session_factory)
                decision = auth.access_decision(
                    auth.client_host(request),
                    cfg.auth_mode,
                    cfg.trusted_networks,
                    cfg.allowed_networks,
                )
                if decision == auth.ACCESS_DENY:
                    event(
                        log,
                        "auth.denied_network",
                        level=30,
                        client=auth.client_host(request) or "?",
                    )
                    return HTMLResponse(
                        "<!doctype html><meta charset=utf-8><title>NetMover</title>"
                        "<p>Zugriff aus diesem Netz ist nicht erlaubt.</p>",
                        status_code=403,
                    )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.load_or_create_session_key(settings.session_key_path),
        session_cookie="netmover_session",
        max_age=settings.session_hours * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    @app.exception_handler(auth.LoginRequired)
    async def login_required(request: Request, exc: auth.LoginRequired) -> Response:
        target = "/setup" if exc.setup else f"/login?next={request.url.path}"
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return redirect(target)

    app.mount(
        "/static", StaticFiles(directory=str(files("netmover.web") / "static")), name="static"
    )
    app.include_router(public)
    app.include_router(protected)
    return app


__all__ = ["AppSettings", "create_app"]
