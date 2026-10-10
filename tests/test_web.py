"""Weboberfläche: Einrichtung, Anmeldung, CSRF, Seiten und Aktionen (FakeMountBackend)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from netmover.app_state import NetMover
from netmover.config import Settings
from netmover.db import session_scope
from netmover.models import Credential, Mount, MountState, Rule, Run, RunStatus
from netmover.mounting.fake import FakeMountBackend
from netmover.web.app import create_app

PASSWORD = "sehr-geheim-123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "lib",
        run_dir=tmp_path / "run",
        mount_base=tmp_path / "mnt",
        mount_backend="fake",
    )


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FakeMountBackend]:
    b = FakeMountBackend(tmp_path / "fake")
    yield b
    b.release_hangs()


@pytest.fixture
def state(settings: Settings, backend: FakeMountBackend) -> NetMover:
    return NetMover(settings, backend=backend)


@pytest.fixture
def raw_client(settings: Settings, state: NetMover) -> Iterator[TestClient]:
    with TestClient(create_app(settings, state), follow_redirects=False) as c:
        yield c


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "kein CSRF-Token in der Seite"
    return m.group(1)


def do_setup(c: TestClient) -> str:
    page = c.get("/setup")
    token = csrf_of(page.text)
    code = c.app.state.setup_code.code  # type: ignore[attr-defined]
    r = c.post(
        "/setup",
        data={"csrf_token": token, "code": code, "password": PASSWORD, "confirm": PASSWORD},
    )
    assert r.status_code == 303, r.text
    return csrf_of(c.get("/").text)


@pytest.fixture
def client(raw_client: TestClient) -> TestClient:
    raw_client.headers["X-Test-CSRF"] = do_setup(raw_client)
    return raw_client


def post(c: TestClient, url: str, data: dict[str, str] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    payload = {"csrf_token": c.headers["X-Test-CSRF"], **(data or {})}
    return c.post(url, data=payload, **kw)  # type: ignore[arg-type]


# -- Einrichtung und Anmeldung --------------------------------------------------------------


def test_first_start_forces_setup(raw_client: TestClient) -> None:
    for url in ("/", "/mounts", "/rules", "/log", "/settings", "/login"):
        r = raw_client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/setup", url


def test_setup_requires_code_and_strong_password(raw_client: TestClient) -> None:
    token = csrf_of(raw_client.get("/setup").text)
    code = raw_client.app.state.setup_code.code  # type: ignore[attr-defined]
    bad = [
        {"code": "falsch", "password": PASSWORD, "confirm": PASSWORD},
        {"code": code, "password": "kurz", "confirm": "kurz"},
        {"code": code, "password": PASSWORD, "confirm": PASSWORD + "x"},
    ]
    for data in bad:
        r = raw_client.post("/setup", data={"csrf_token": token, **data})
        assert r.status_code == 400
    assert raw_client.get("/").status_code == 303


def test_setup_only_once(client: TestClient) -> None:
    assert client.get("/setup").headers["location"] == "/login"


def test_login_logout_and_cookie_flags(raw_client: TestClient) -> None:
    do_setup(raw_client)
    token = csrf_of(raw_client.get("/").text)
    raw_client.post("/logout", data={"csrf_token": token})
    assert raw_client.get("/").status_code == 303
    page = raw_client.get("/login")
    r = raw_client.post("/login", data={"csrf_token": csrf_of(page.text), "password": "falsch"})
    assert r.status_code == 401
    r = raw_client.post("/login", data={"csrf_token": csrf_of(page.text), "password": PASSWORD})
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert raw_client.get("/").status_code == 200


def test_login_lockout(raw_client: TestClient) -> None:
    do_setup(raw_client)
    raw_client.cookies.clear()
    token = csrf_of(raw_client.get("/login").text)
    codes = [
        raw_client.post("/login", data={"csrf_token": token, "password": "x"}).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_csrf_required_for_post(client: TestClient) -> None:
    assert client.post("/rules/1/toggle").status_code == 403
    assert client.post("/rules/1/toggle", data={"csrf_token": "falsch"}).status_code == 403
    assert post(client, "/rules/1/toggle").status_code == 303
    r = client.post("/rules/1/toggle", headers={"X-CSRF-Token": client.headers["X-Test-CSRF"]})
    assert r.status_code == 303


def test_htmx_request_without_login_gets_hx_redirect(raw_client: TestClient) -> None:
    do_setup(raw_client)
    raw_client.cookies.clear()
    r = raw_client.get("/partials/status", headers={"HX-Request": "true"})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/login")


def test_security_headers(client: TestClient) -> None:
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_no_inline_scripts_or_styles(client: TestClient) -> None:
    for url in ("/", "/mounts", "/mounts/new", "/rules", "/rules/new", "/log", "/settings"):
        html = client.get(url).text
        assert "style=" not in html, url
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), url


def test_healthz(client: TestClient, state: NetMover) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    with session_scope(state.session_factory) as s:
        s.add(
            Mount(
                name="kaputt",
                slug="kaputt",
                type="cifs",
                server="nas",
                share="x",
                mountpoint="/mnt/netmover/kaputt",
                state=MountState.ERROR,
                last_error="weg",
            )
        )
    r = client.get("/healthz")
    assert r.status_code == 503 and "kaputt" in r.json()["problems"][0]


# -- Anbindungen ------------------------------------------------------------------------------


MOUNT_FORM = {
    "name": "Synology Scans",
    "type": "cifs",
    "server": "nas",
    "share": "scans",
    "username": "scanner",
    "password": "Pa$$w0rd,mit=Zeichen",
    "uid": "1000",
    "gid": "1000",
    "file_mode": "0644",
    "dir_mode": "0755",
    "extra_options": "vers=3.0",
    "auto_mount": "on",
    "enabled": "on",
}


def _mount_id(state: NetMover, name: str) -> int:
    with state.session_factory() as s:
        mid = s.scalar(select(Mount.id).where(Mount.name == name))
        assert mid is not None
        return mid


def test_create_mount_test_mount_unmount_delete(
    client: TestClient, state: NetMover, backend: FakeMountBackend
) -> None:
    backend.export("nas", "scans")
    r = post(client, "/mounts/new", MOUNT_FORM)
    assert r.status_code == 303, r.text
    mid = _mount_id(state, "Synology Scans")
    with state.session_factory() as s:
        m = s.get(Mount, mid)
        assert m is not None and m.slug == "synology-scans" and m.credential is not None
        assert "Pa$$" not in m.credential.secret_enc
        assert state.secrets.decrypt(m.credential.secret_enc) == MOUNT_FORM["password"]

    # Passwort taucht nirgends auf
    for url in ("/mounts", f"/mounts/{mid}/edit"):
        assert MOUNT_FORM["password"] not in client.get(url).text

    r = post(client, f"/mounts/{mid}/test")
    assert r.status_code == 200 and "Quelle mit Löschen" in r.text and "✓" in r.text

    assert post(client, f"/mounts/{mid}/mount").status_code == 303
    assert backend.is_mounted(state.mounts.mountpoint_for("synology-scans"))
    spec = [c for c in backend.calls if c.action == "mount"][-1].spec
    assert spec is not None and spec.credentials is not None
    assert spec.credentials.password == MOUNT_FORM["password"]
    assert "Fehler" not in client.get("/").text

    assert post(client, f"/mounts/{mid}/unmount").status_code == 303
    assert not backend.mounted
    assert post(client, f"/mounts/{mid}/delete").status_code == 303
    with state.session_factory() as s:
        assert s.scalars(select(Mount)).all() == []
        assert s.scalars(select(Credential)).all() == []


def test_empty_password_keeps_secret(client: TestClient, state: NetMover) -> None:
    post(client, "/mounts/new", MOUNT_FORM)
    mid = _mount_id(state, "Synology Scans")
    r = post(client, f"/mounts/{mid}/edit", {**MOUNT_FORM, "password": "", "username": "neu"})
    assert r.status_code == 303
    with state.session_factory() as s:
        m = s.get(Mount, mid)
        assert m is not None and m.credential is not None
        assert m.credential.username == "neu"
        assert state.secrets.decrypt(m.credential.secret_enc) == MOUNT_FORM["password"]


def test_mount_form_validation_shows_errors(client: TestClient, state: NetMover) -> None:
    bad = {**MOUNT_FORM, "server": "nas;reboot", "extra_options": "password=x"}
    r = post(client, "/mounts/new", bad)
    assert r.status_code == 400
    assert "Ungültiger Servername" in r.text and "gesperrt" in r.text
    with state.session_factory() as s:
        assert s.scalars(select(Mount)).all() == []


def test_connection_change_requires_unmount(
    client: TestClient, state: NetMover, backend: FakeMountBackend
) -> None:
    backend.export("nas", "scans")
    post(client, "/mounts/new", MOUNT_FORM)
    mid = _mount_id(state, "Synology Scans")
    post(client, f"/mounts/{mid}/mount")
    r = post(client, f"/mounts/{mid}/edit", {**MOUNT_FORM, "share": "anders", "password": ""})
    assert r.status_code == 400 and "ausgehängt" in r.text
    # Nicht-Verbindungsfelder gehen trotzdem
    r = post(client, f"/mounts/{mid}/edit", {**MOUNT_FORM, "password": "", "auto_mount": ""})
    assert r.status_code == 303


def test_form_test_with_unsaved_data(client: TestClient, backend: FakeMountBackend) -> None:
    backend.export("nas", "scans")
    r = post(client, "/mounts/test", MOUNT_FORM)
    assert r.status_code == 200 and "Einhängen" in r.text and "✓" in r.text
    r = post(client, "/mounts/test", {**MOUNT_FORM, "share": "fehlt"})
    assert ("✗" in r.text and "existiert auf dem Server nicht" not in r.text) or "✗" in r.text


def test_sftp_mount_generates_key(client: TestClient, state: NetMover) -> None:
    r = post(
        client,
        "/mounts/new",
        {
            "name": "paperless",
            "type": "sftp",
            "server": "10.0.0.5",
            "share": "/opt/consume",
            "port": "22",
            "auth": "ssh_key",
            "username": "paperless",
            "enabled": "on",
        },
    )
    assert r.status_code == 303, r.text
    page = client.get("/mounts").text
    assert "ssh-ed25519 " in page and "BEGIN OPENSSH PRIVATE KEY" not in page
    with state.session_factory() as s:
        m = s.scalars(select(Mount)).one()
        assert m.mountpoint is None and m.auto_mount is False


def test_delete_mount_used_by_rule_is_refused(client: TestClient, state: NetMover) -> None:
    post(client, "/mounts/new", MOUNT_FORM)
    mid = _mount_id(state, "Synology Scans")
    with session_scope(state.session_factory) as s:
        s.add(Rule(name="r", src_mount_id=mid, dst_path="/tmp"))
    post(client, f"/mounts/{mid}/delete")
    assert "Wird noch von Regeln benutzt" in client.get("/mounts").text
    assert _mount_id(state, "Synology Scans") == mid


# -- Regeln -----------------------------------------------------------------------------------


@pytest.fixture
def mounted(
    client: TestClient, state: NetMover, backend: FakeMountBackend, tmp_path: Path
) -> tuple[int, Path, Path]:
    remote = backend.export("nas", "scans")
    post(client, "/mounts/new", MOUNT_FORM)
    mid = _mount_id(state, "Synology Scans")
    post(client, f"/mounts/{mid}/mount")
    consume = tmp_path / "consume"
    consume.mkdir()
    return mid, remote, consume


def rule_form(mid: int, consume: Path, **kw: str) -> dict[str, str]:
    data = {
        "name": "Scans → Paperless",
        "enabled": "on",
        "src_mount_id": str(mid),
        "src_subpath": "",
        "dst_kind": "local",
        "dst_path": str(consume),
        "pattern": "*.pdf",
        "pattern_type": "glob",
        "interval_seconds": "30",
        "stability_seconds": "0",
        "min_age_seconds": "0",
        "op_timeout_seconds": "120",
        "action": "move",
        "on_collision": "rename",
        "quarantine_after": "3",
    }
    data.update(kw)
    return data


def test_rule_validation(client: TestClient, mounted: tuple[int, Path, Path]) -> None:
    mid, _, consume = mounted
    r = post(
        client,
        "/rules/new",
        rule_form(
            mid,
            consume,
            interval_seconds="5",
            pattern_type="regex",
            pattern="([",
            set_owner="root",
            dst_path="relativ",
        ),
    )
    assert r.status_code == 400
    for msg in ("mindestens 10", "regulärer Ausdruck", "uid:gid", "absoluten Pfad"):
        assert msg in r.text


def test_rule_create_dryrun_run(
    client: TestClient, state: NetMover, mounted: tuple[int, Path, Path]
) -> None:
    mid, remote, consume = mounted
    (remote / "scan001.pdf").write_text("pdf")
    (remote / "notiz.txt").write_text("txt")
    r = post(client, "/rules/new", rule_form(mid, consume))
    assert r.status_code == 303, r.text
    with state.session_factory() as s:
        rid = s.scalars(select(Rule.id)).one()
    assert state.scheduler.next_run(rid) is not None

    r = post(client, f"/rules/{rid}/dryrun")
    assert r.status_code == 200
    assert "scan001.pdf" in r.text and str(consume / "scan001.pdf") in r.text
    assert "notiz.txt" not in r.text and "nichts verändert" in r.text
    assert (remote / "scan001.pdf").exists()

    result = state.runs.execute(rid)  # „Jetzt ausführen“ synchron für den Test
    assert result is not None
    assert (consume / "scan001.pdf").exists() and not (remote / "scan001.pdf").exists()

    log = client.get("/log").text
    assert "Scans → Paperless" in log
    with state.session_factory() as s:
        run = s.scalars(select(Run).where(Run.dry_run.is_(False))).one()
        assert run.status is RunStatus.OK
    items = client.get(f"/log/{run.id}/items").text
    assert "scan001.pdf" in items and "verschoben" in items


def test_rule_run_now_button(
    client: TestClient, state: NetMover, mounted: tuple[int, Path, Path]
) -> None:
    mid, _, consume = mounted
    post(client, "/rules/new", rule_form(mid, consume))
    with state.session_factory() as s:
        rid = s.scalars(select(Rule.id)).one()
    r = post(client, f"/rules/{rid}/run")
    assert r.status_code == 303
    assert "Lauf gestartet" in client.get("/").text


def test_rule_toggle_and_delete_sync_scheduler(
    client: TestClient, state: NetMover, mounted: tuple[int, Path, Path]
) -> None:
    mid, _, consume = mounted
    post(client, "/rules/new", rule_form(mid, consume))
    with state.session_factory() as s:
        rid = s.scalars(select(Rule.id)).one()
    post(client, f"/rules/{rid}/toggle")
    assert state.scheduler.next_run(rid) is None
    post(client, f"/rules/{rid}/toggle")
    assert state.scheduler.next_run(rid) is not None
    post(client, f"/rules/{rid}/delete")
    assert state.scheduler.next_run(rid) is None


def test_status_page_shows_stale_warning(
    client: TestClient, state: NetMover, mounted: tuple[int, Path, Path]
) -> None:
    from datetime import timedelta

    from netmover.models import utcnow

    mid, _, consume = mounted
    post(client, "/rules/new", rule_form(mid, consume))
    with session_scope(state.session_factory) as s:
        r = s.scalars(select(Rule)).one()
        r.created_at = utcnow() - timedelta(hours=1)
        r.last_success_at = utcnow() - timedelta(minutes=10)
    state.started_at = utcnow() - timedelta(hours=1)
    page = client.get("/").text
    assert "länger als 3 × Intervall" in page and 'class="warn"' in page
    assert client.get("/healthz").status_code == 503


# -- Einstellungen -------------------------------------------------------------------------------


def test_settings_webhook_url_is_masked(client: TestClient, state: NetMover) -> None:
    from netmover.settings_store import load_settings

    url = "https://ntfy.example/geheimes-topic-token"
    r = post(
        client,
        "/settings",
        {
            "retention_days": "14",
            "notify_mode": "every",
            "notify_after": "3",
            "webhook_url": url,
            "webhook_content_type": "text/plain",
            "webhook_template": "$message",
        },
    )
    assert r.status_code == 303
    page = client.get("/settings").text
    assert "geheimes-topic-token" not in page and "ntfy.example" in page
    # Maske unverändert zurückschicken → URL bleibt
    masked = re.search(r'name="webhook_url" value="([^"]*)"', page)
    assert masked
    post(
        client,
        "/settings",
        {
            "retention_days": "14",
            "notify_mode": "every",
            "notify_after": "3",
            "webhook_url": masked.group(1),
            "webhook_content_type": "text/plain",
            "webhook_template": "$message",
        },
    )
    cfg = load_settings(state.session_factory)
    assert cfg.webhook_url == url and cfg.retention_days == 14


def test_change_password(client: TestClient) -> None:
    r = post(
        client,
        "/settings/password",
        {"current": "falsch", "password": "neues-passwort-1", "confirm": "neues-passwort-1"},
    )
    assert "falsch" in client.get("/settings").text
    r = post(
        client,
        "/settings/password",
        {"current": PASSWORD, "password": "neues-passwort-1", "confirm": "neues-passwort-1"},
    )
    assert r.status_code == 303


def test_export_excludes_secrets_and_import_roundtrip(
    client: TestClient,
    state: NetMover,
    mounted: tuple[int, Path, Path],
    tmp_path: Path,
) -> None:
    mid, _, consume = mounted
    post(client, "/rules/new", rule_form(mid, consume))
    r = client.get("/settings/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.text
    assert MOUNT_FORM["password"] not in text and "scanner" not in text and "secret_enc" not in text
    data = json.loads(text)
    assert data["mounts"][0]["name"] == "Synology Scans"
    assert data["rules"][0]["src_mount"] == "Synology Scans"

    # in eine frische Instanz importieren
    settings2 = Settings(
        data_dir=tmp_path / "lib2",
        run_dir=tmp_path / "run2",
        mount_base=tmp_path / "mnt2",
        mount_backend="fake",
    )
    state2 = NetMover(settings2, backend=FakeMountBackend(tmp_path / "fake2"))
    with TestClient(create_app(settings2, state2), follow_redirects=False) as c2:
        c2.headers["X-Test-CSRF"] = do_setup(c2)
        r = c2.post(
            "/settings/import",
            data={"csrf_token": c2.headers["X-Test-CSRF"]},
            files={"file": ("export.json", text, "application/json")},
        )
        assert r.status_code == 303 and r.headers["location"] == "/settings", r.headers
        page = c2.get("/settings").text
        assert "Importiert" in page and "neu eingeben" in page
        with state2.session_factory() as s:
            m = s.scalars(select(Mount)).one()
            assert m.credential is None and m.auto_mount is False
            rule = s.scalars(select(Rule)).one()
            assert rule.enabled is False and rule.src_mount_id == m.id
        # zweiter Import überspringt vorhandene Namen
        c2.post(
            "/settings/import",
            data={"csrf_token": c2.headers["X-Test-CSRF"]},
            files={"file": ("export.json", text, "application/json")},
        )
        assert "Übersprungen" in c2.get("/settings").text
