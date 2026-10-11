"""Anmeldung je Netz: ohne Passwort / mit Passwort / abgewiesen."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from netmover.settings_store import load_settings, save_settings
from netmover.web.auth import (
    ACCESS_DENY,
    ACCESS_NOPW,
    ACCESS_PW,
    access_decision,
    includes_loopback,
    parse_networks,
)
from tests.webutil import PASSWORD, csrf_of


def _other(client: TestClient, host: str) -> TestClient:
    """Weiterer Client für dieselbe App (ohne eigenen Lebenszyklus) mit fester Adresse."""
    return TestClient(client.app, follow_redirects=False, client=(host, 51000))


def _save(c: TestClient, trusted: str = "", allowed: str = "", current: str = PASSWORD) -> Any:
    token = (
        csrf_of(c.get("/settings").text)
        if "X-Test-CSRF" not in c.headers
        else c.headers["X-Test-CSRF"]
    )
    return c.post(
        "/settings/auth",
        data={
            "csrf_token": token,
            "trusted_networks": trusted,
            "allowed_networks": allowed,
            "current": current,
        },
    )


def _set(state: Any, **kw: str) -> None:
    cfg = load_settings(state.session_factory)
    for k, v in kw.items():
        setattr(cfg, k, v)
    save_settings(state.session_factory, cfg)


# -- Entscheidung ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "trusted", "allowed", "expected"),
    [
        ("10.0.10.50", "", "", ACCESS_PW),  # Standard: überall mit Passwort
        ("10.0.10.50", "10.0.10.0/24", "", ACCESS_NOPW),
        ("10.1.5.9", "10.0.10.0/24", "", ACCESS_PW),  # übrige Netze mit Passwort
        ("10.1.5.9", "10.0.10.0/24", "10.1.5.0/24", ACCESS_PW),
        ("192.168.5.5", "10.0.10.0/24", "10.1.5.0/24", ACCESS_DENY),
        ("10.0.10.50", "10.0.10.0/24", "10.1.5.0/24", ACCESS_NOPW),  # ohne PW schlägt Liste
        ("2001:db8::5", "0.0.0.0/0, ::/0", "", ACCESS_NOPW),  # Anmeldung ganz aus
        ("testclient", "", "10.0.0.0/8", ACCESS_DENY),  # keine IP-Adresse
    ],
)
def test_access_decision(host: str, trusted: str, allowed: str, expected: str) -> None:
    assert access_decision(host, "always", trusted, allowed) == expected


def test_legacy_off_mode_still_works() -> None:
    assert access_decision("192.168.5.5", "off", "", "10.1.5.0/24") == ACCESS_NOPW


# -- Oberfläche --------------------------------------------------------------------------------


def test_default_requires_login(client: TestClient) -> None:
    assert _other(client, "10.0.10.50").get("/").status_code == 303


def test_change_needs_current_password(client: TestClient, state: Any) -> None:
    _save(client, trusted="10.0.10.0/24", current="falsch")
    assert load_settings(state.session_factory).trusted_networks == ""
    assert "Aktuelles Passwort falsch" in client.get("/settings").text


def test_without_password_from_trusted_network(client: TestClient, state: Any) -> None:
    _save(client, trusted="10.0.10.0/24")
    lan = _other(client, "10.0.10.50")
    r = lan.get("/")
    assert r.status_code == 200 and "ohne Anmeldung (vertrautes Netz)" in r.text
    assert "Abmelden" not in r.text and lan.get("/login").headers["location"] == "/"
    assert lan.post("/maintenance").status_code == 403  # CSRF gilt weiterhin
    assert (
        lan.post("/maintenance", data={"csrf_token": csrf_of(lan.get("/").text)}).status_code == 303
    )
    assert _other(client, "10.1.5.9").get("/").status_code == 303  # übrige mit Passwort


def test_other_networks_are_rejected_when_password_list_is_set(
    client: TestClient, state: Any
) -> None:
    _save(client, trusted="0.0.0.0/0")  # damit der Test-Client (keine IP) weiterkommt …
    _set(state, trusted_networks="10.0.10.0/24", allowed_networks="10.1.5.0/24")  # … dann scharf
    assert _other(client, "10.0.10.50").get("/").status_code == 200
    assert _other(client, "10.1.5.9").get("/").headers["location"].startswith("/login")
    blocked = _other(client, "192.168.5.5")
    for url in ("/", "/login", "/setup", "/mounts"):
        assert blocked.get(url).status_code == 403, url
    assert blocked.get("/healthz").status_code == 200  # Überwachung bleibt erreichbar
    assert blocked.get("/static/app.css").status_code == 200


def test_cannot_lock_yourself_out(client: TestClient, state: Any) -> None:
    lan = _other(client, "10.0.10.50")
    _save(client, trusted="10.0.10.0/24")
    # aus 10.0.10.50 eine Liste speichern, in der das eigene Netz fehlt
    r = _save(lan, trusted="", allowed="10.1.5.0/24")
    assert r.status_code == 303
    assert "ausgesperrt" in lan.get("/settings").text
    cfg = load_settings(state.session_factory)
    assert cfg.trusted_networks == "10.0.10.0/24" and cfg.allowed_networks == ""
    # eigenes Netz bei „mit Passwort“ – erlaubt
    _save(lan, trusted="10.0.10.0/24", allowed="10.1.5.0/24")
    assert load_settings(state.session_factory).allowed_networks == "10.1.5.0/24"


def test_everything_without_password(client: TestClient) -> None:
    _save(client, trusted="0.0.0.0/0, ::/0")
    r = _other(client, "192.168.77.1").get("/rules")
    assert r.status_code == 200 and ">ohne Anmeldung<" in r.text


def test_setup_still_required_without_password(raw_client: TestClient, state: Any) -> None:
    _set(state, trusted_networks="0.0.0.0/0")
    assert raw_client.get("/").headers["location"] == "/setup"


def test_validation_and_loopback_hint(client: TestClient, state: Any) -> None:
    _save(client, trusted="10.0.10.0/24, quatsch")
    assert "kein gültiges Netz" in client.get("/settings").text
    assert load_settings(state.session_factory).trusted_networks == ""
    _save(client, trusted="127.0.0.1")
    assert "jede</strong> Anfrage über den Proxy" in client.get("/settings").text


def test_network_parsing() -> None:
    nets = parse_networks("10.0.10.0/24; 192.168.1.20 fe80::/10")
    assert [str(n) for n in nets] == ["10.0.10.0/24", "192.168.1.20/32", "fe80::/10"]
    assert includes_loopback("10.0.0.0/8, 127.0.0.0/8") and not includes_loopback("10.0.0.0/8")
    with pytest.raises(ValueError):
        parse_networks("10.0.0.0/33")


def test_reset_password_turns_login_back_on(
    state: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmover import cli
    from netmover.web.auth import set_password

    set_password(state.session_factory, PASSWORD)
    _set(state, auth_mode="off", trusted_networks="0.0.0.0/0", allowed_networks="10.1.5.0/24")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["reset-password"]) == 0
    cfg = load_settings(state.session_factory)
    assert cfg.auth_mode == "always" and cfg.trusted_networks == "" and cfg.allowed_networks == ""
