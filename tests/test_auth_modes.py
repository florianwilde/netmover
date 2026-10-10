"""0.5.0: Anmeldung abschaltbar – immer / nicht aus vertrauenswürdigen Netzen / nie."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from netmover.settings_store import load_settings
from netmover.web.auth import includes_loopback, parse_networks
from tests.webutil import PASSWORD, csrf_of, post


def _set_mode(client: TestClient, mode: str, nets: str = "", current: str = PASSWORD) -> Any:
    return post(
        client, "/settings/auth", {"auth_mode": mode, "trusted_networks": nets, "current": current}
    )


def _other(client: TestClient, host: str) -> TestClient:
    """Weiterer Client für dieselbe App (ohne eigenen Lebenszyklus) mit fester Adresse."""
    return TestClient(client.app, follow_redirects=False, client=(host, 51000))


@pytest.fixture
def lan_client(client: TestClient) -> TestClient:
    return _other(client, "10.0.10.50")


def test_default_requires_login(client: TestClient, lan_client: TestClient) -> None:
    assert lan_client.get("/").status_code == 303


def test_mode_change_needs_current_password(client: TestClient, state: Any) -> None:
    _set_mode(client, "off", current="falsch")
    assert load_settings(state.session_factory).auth_mode == "always"
    assert "Aktuelles Passwort falsch" in client.get("/settings").text


def test_trusted_networks(
    client: TestClient, lan_client: TestClient, settings: Any, state: Any
) -> None:
    _set_mode(client, "trusted", "10.0.10.0/24")
    r = lan_client.get("/")
    assert r.status_code == 200 and "ohne Anmeldung (vertrautes Netz)" in r.text
    assert "Abmelden" not in r.text
    assert lan_client.get("/login").headers["location"] == "/"
    # CSRF gilt weiterhin
    assert lan_client.post("/maintenance").status_code == 403
    token = csrf_of(lan_client.get("/").text)
    assert lan_client.post("/maintenance", data={"csrf_token": token}).status_code == 303
    # andere Netze brauchen weiter das Passwort
    assert _other(client, "192.168.5.5").get("/").status_code == 303


def test_off_and_back_on(client: TestClient, lan_client: TestClient) -> None:
    _set_mode(client, "off")
    r = lan_client.get("/rules")
    assert r.status_code == 200 and "ohne Anmeldung" in r.text
    token = csrf_of(r.text)
    lan_client.post(
        "/settings/auth",
        data={
            "csrf_token": token,
            "auth_mode": "always",
            "trusted_networks": "",
            "current": PASSWORD,
        },
    )
    assert lan_client.get("/").status_code == 303


def test_setup_still_required_without_password(raw_client: TestClient, state: Any) -> None:
    from netmover.settings_store import save_settings

    cfg = load_settings(state.session_factory)
    cfg.auth_mode = "off"
    save_settings(state.session_factory, cfg)
    assert raw_client.get("/").headers["location"] == "/setup"


def test_validation(client: TestClient, state: Any) -> None:
    _set_mode(client, "trusted", "")
    assert "mindestens ein" in client.get("/settings").text
    _set_mode(client, "trusted", "10.0.10.0/24, quatsch")
    assert "kein gültiges Netz" in client.get("/settings").text
    assert load_settings(state.session_factory).auth_mode == "always"
    _set_mode(client, "trusted", "127.0.0.1")
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
    from netmover.settings_store import save_settings
    from netmover.web.auth import set_password

    set_password(state.session_factory, PASSWORD)
    cfg = load_settings(state.session_factory)
    cfg.auth_mode = "off"
    save_settings(state.session_factory, cfg)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["reset-password"]) == 0
    assert load_settings(state.session_factory).auth_mode == "always"
