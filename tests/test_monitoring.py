"""Uptime Kuma: /healthz, Einzel-Endpunkte, Push; Hilfe-Seite und Menü."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from netmover.db import session_scope
from netmover.health import Check, Health
from netmover.kuma import push, push_url
from netmover.models import Mount, MountState, MountType, Rule, utcnow
from netmover.settings_store import AppSettings, load_settings, save_settings
from tests.webutil import post

# -- push_url / push ---------------------------------------------------------------------------


def _health(ok: bool, *problems: str) -> Health:
    h = Health()
    h.add(Check("scheduler", "Scheduler", True, "läuft"))
    for p in problems:
        h.add(Check("mount", "x", False, p))
    assert h.ok is ok
    return h


def test_push_url_replaces_kuma_defaults() -> None:
    url = push_url(
        "https://kuma.lan/api/push/AbC?status=up&msg=OK&ping=", _health(False, "NAS weg")
    )
    parsed = httpx.URL(url)
    assert parsed.path == "/api/push/AbC"
    assert parsed.params["status"] == "down" and parsed.params["msg"] == "NAS weg"
    assert push_url("https://kuma.lan/api/push/AbC", _health(True)).endswith(
        "status=up&msg=OK&ping="
    )


def test_push_message_is_truncated() -> None:
    url = push_url("https://k/api/push/x", _health(False, "x" * 1000))
    assert len(httpx.URL(url).params["msg"]) == 250


def test_push_swallows_errors_and_reports_result() -> None:
    calls: list[str] = []

    def ok_get(url: str, **kw: Any) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    def bad_get(url: str, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("weg")

    assert push("https://k/api/push/x", _health(True), getter=ok_get) is True
    assert calls and "status=up" in calls[0]
    assert push("https://k/api/push/x", _health(True), getter=bad_get) is False


# -- Endpunkte ----------------------------------------------------------------------------------


def _add_mount(
    state: Any, name: str, slug: str, mstate: MountState, error: str | None = None
) -> int:
    with session_scope(state.session_factory) as s:
        m = Mount(
            name=name,
            slug=slug,
            type=MountType.CIFS,
            server="nas",
            share=slug,
            mountpoint=f"/mnt/netmover/{slug}",
            state=mstate,
            last_error=error,
        )
        s.add(m)
        s.flush()
        return m.id


def test_healthz_details_and_single_endpoints(client: TestClient, state: Any) -> None:
    ok_id = _add_mount(state, "NAS", "nas", MountState.MOUNTED)
    _add_mount(state, "Archiv", "archiv", MountState.ERROR, "Host is down")
    with session_scope(state.session_factory) as s:
        r = Rule(name="Scans", src_mount_id=ok_id, dst_path="/tmp", consecutive_failures=3)
        s.add(r)
        s.flush()
        rid = r.id

    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error" and body["version"]
    assert body["scheduler"]["running"] is True
    kinds = {(c["kind"], c["name"], c["ok"]) for c in body["checks"]}
    assert ("mount", "NAS", True) in kinds and ("mount", "Archiv", False) in kinds
    assert ("rule", "Scans", False) in kinds  # drei Fehlschläge in Folge

    assert client.get("/healthz/mount/nas").status_code == 200
    r = client.get("/healthz/mount/archiv")
    assert r.status_code == 503 and "Host is down" in r.json()["message"]
    r = client.get(f"/healthz/rule/{rid}")
    assert r.status_code == 503 and "3 fehlgeschlagene Läufe" in r.json()["message"]
    assert client.get("/healthz/mount/gibtsnicht").status_code == 404
    assert client.get("/healthz/rule/4711").status_code == 404


def test_healthz_needs_no_login(raw_client: TestClient) -> None:
    assert raw_client.get("/healthz").status_code == 200
    assert raw_client.get("/healthz/mount/x").status_code == 404


def test_disabled_rule_and_degraded_mount_are_not_alarms(client: TestClient, state: Any) -> None:
    mid = _add_mount(state, "NAS", "nas", MountState.DEGRADED)
    with session_scope(state.session_factory) as s:
        s.add(
            Rule(
                name="aus",
                src_mount_id=mid,
                dst_path="/tmp",
                enabled=False,
                created_at=utcnow() - timedelta(days=3),
            )
        )
    assert client.get("/healthz").status_code == 200


def test_kuma_push_job_uses_saved_url(state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "netmover.kuma.httpx.get",
        lambda url, **kw: (
            sent.append(url),
            httpx.Response(200, request=httpx.Request("GET", url)),
        )[1],
    )
    state.push_health()  # ohne URL: nichts
    assert sent == []
    save_settings(
        state.session_factory, AppSettings(kuma_push_url="https://kuma.lan/api/push/T0k3n")
    )
    state.push_health()
    assert len(sent) == 1 and sent[0].startswith("https://kuma.lan/api/push/T0k3n?status=")


def test_settings_kuma_url_masked_and_monitor_urls_listed(client: TestClient, state: Any) -> None:
    _add_mount(state, "NAS", "nas", MountState.MOUNTED)
    post(
        client,
        "/settings",
        {
            "retention_days": "30",
            "notify_mode": "off",
            "notify_after": "3",
            "webhook_url": "",
            "webhook_content_type": "application/json",
            "webhook_template": "$message",
            "kuma_push_url": "https://kuma.lan/api/push/GeHeIm?status=up",
        },
    )
    assert load_settings(state.session_factory).kuma_push_url.endswith("GeHeIm?status=up")
    page = client.get("/settings").text
    assert "GeHeIm" not in page and "kuma.lan" in page
    assert "/healthz/mount/nas" in page and "/healthz" in page


# -- Hilfe und Menü ---------------------------------------------------------------------------


def test_help_page_renders_guide_with_anchors(client: TestClient) -> None:
    html = client.get("/help").text
    for anchor in (
        "begriffe",
        "anbindungen",
        "schluessel",
        "regeln",
        "regel-sicherheit",
        "probelauf",
        "status",
        "protokoll",
        "uptime-kuma",
        "fragen",
    ):
        assert f'id="{anchor}"' in html, anchor
    assert "<table>" in html and "{#" not in html
    assert 'src="/help/img/' in html


def test_help_images_and_path_safety(client: TestClient) -> None:
    from netmover.web.app import _help_dir

    if (_help_dir() / "img" / "status.png").is_file():
        r = client.get("/help/img/status.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    for bad in ("../ANLEITUNG.md", "..%2F..%2Fpyproject.toml", "x.svg"):
        assert client.get(f"/help/img/{bad}").status_code == 404


def test_navigation_groups_health_and_help_links(client: TestClient, state: Any) -> None:
    _add_mount(state, "Archiv", "archiv", MountState.ERROR, "weg")
    html = client.get("/mounts").text
    for group in ("Übersicht", "Verbindungen", "Automatisierung", "System"):
        assert group in html
    assert 'class="health bad"' in html and "1 Problem" in html
    assert "1 gestört" in html
    assert 'href="/help#anbindungen"' in html  # Hilfe zu dieser Seite
    assert 'aria-current="page"' in html and 'class="menu-toggle"' in html
    form = client.get("/rules/new").text
    assert 'href="/help#regel-sicherheit"' in form


def test_logged_out_pages_have_no_menu(raw_client: TestClient) -> None:
    html = raw_client.get("/setup").text
    assert 'id="sidebar"' not in html
