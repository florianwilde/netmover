"""E-Mail-Benachrichtigung gegen einen echten SMTP-Server im Testprozess (aiosmtpd)."""

from __future__ import annotations

import datetime
import socket
import ssl
from collections.abc import Iterator
from email import message_from_bytes
from email.policy import default
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from netmover.crypto import SecretBox
from netmover.mail import MailConfigError, parse_recipients, smtp_send
from netmover.notify import Notification, Notifier
from netmover.settings_store import AppSettings, NotifyMode, load_settings, save_settings
from tests.webutil import post


class Inbox:
    def __init__(self) -> None:
        self.messages: list[tuple[list[str], Any]] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        self.messages.append(
            (envelope.rcpt_tos, message_from_bytes(envelope.content, policy=default))
        )
        return "250 OK"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def _auth(server: Any, session: Any, envelope: Any, mechanism: str, data: Any) -> AuthResult:
    ok = (
        isinstance(data, LoginPassword)
        and data.login == b"netmover"
        and data.password == b"Pa$$,wort"
    )
    return AuthResult(success=ok, handled=False)


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("tls")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return d / "cert.pem", d / "key.pem"


@pytest.fixture
def trust(tls_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Client vertraut unserem Test-Zertifikat (Prüfung bleibt eingeschaltet)."""
    monkeypatch.setattr(
        "netmover.mail.tls_context", lambda: ssl.create_default_context(cafile=str(tls_files[0]))
    )


def _server(mode: str, tls_files: tuple[Path, Path]) -> tuple[Controller, Inbox, int]:
    inbox, port = Inbox(), _free_port()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(*map(str, tls_files))
    kw: dict[str, Any] = {
        "hostname": "localhost",
        "port": port,
        "authenticator": _auth,
        "auth_exclude_mechanism": ["LOGIN"],
    }
    if mode == "starttls":
        kw.update(tls_context=ctx, require_starttls=True)
    elif mode == "ssl":
        # implizites TLS erkennt aiosmtpd nicht als „verschlüsselt“ – AUTH trotzdem anbieten
        kw.update(ssl_context=ctx, auth_require_tls=False)
    else:
        kw.update(auth_require_tls=False)
    controller = Controller(inbox, **kw)
    controller.start()
    return controller, inbox, port


@pytest.fixture(params=["starttls", "ssl", "none"])
def smtp_server(
    request: pytest.FixtureRequest, tls_files: tuple[Path, Path], trust: None
) -> Iterator[tuple[str, Inbox, int]]:
    controller, inbox, port = _server(request.param, tls_files)
    yield request.param, inbox, port
    controller.stop()


def _cfg(mode: str, port: int, **kw: Any) -> AppSettings:
    data: dict[str, Any] = {
        "email_enabled": True,
        "smtp_host": "localhost",
        "smtp_port": port,
        "smtp_security": mode,
        "smtp_user": "netmover",
        "smtp_from": "netmover@example.org",
        "smtp_to": "ich@example.org; admin@example.org",
        "notify_mode": NotifyMode.EVERY,
    }
    data.update(kw)
    return AppSettings(**data)


def test_smtp_send_all_security_modes(smtp_server: tuple[str, Inbox, int]) -> None:
    mode, inbox, port = smtp_server
    smtp_send(_cfg(mode, port), "Pa$$,wort", "NetMover: Anbindung „NAS“ gestört", "Host is down")
    ((rcpts, msg),) = inbox.messages
    assert rcpts == ["ich@example.org", "admin@example.org"]
    assert msg["Subject"] == "NetMover: Anbindung „NAS“ gestört"
    assert msg["From"] == "netmover@example.org"
    body = msg.get_content()
    assert "Host is down" in body and "NetMover auf" in body


def test_wrong_password_raises(smtp_server: tuple[str, Inbox, int]) -> None:
    import smtplib

    mode, inbox, port = smtp_server
    with pytest.raises(smtplib.SMTPAuthenticationError):
        smtp_send(_cfg(mode, port), "falsch", "x", "y")
    assert inbox.messages == []


def test_certificate_is_verified(tls_files: tuple[Path, Path]) -> None:
    """Ohne Vertrauen in das Zertifikat wird abgelehnt – keine stille Herabstufung."""
    controller, inbox, port = _server("starttls", tls_files)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            smtp_send(_cfg("starttls", port), "Pa$$,wort", "x", "y")
    finally:
        controller.stop()
    assert inbox.messages == []


def test_recipients() -> None:
    assert parse_recipients("a@b.de, c@d.org;e@f.net\n") == ["a@b.de", "c@d.org", "e@f.net"]
    with pytest.raises(MailConfigError, match="kein-mail"):
        parse_recipients("a@b.de, kein-mail")


def test_notifier_sends_webhook_and_mail(
    session_factory: sessionmaker[Session], secretbox: SecretBox
) -> None:
    hooks: list[Any] = []
    mails: list[Any] = []
    cfg = _cfg(
        "starttls",
        587,
        smtp_password_enc=secretbox.encrypt("Pa$$,wort"),
        webhook_url="http://hook.invalid/x",
    )
    save_settings(session_factory, cfg)
    n = Notifier(
        session_factory,
        sender=lambda *a: hooks.append(a),
        mailer=lambda *a: mails.append(a),
        secrets=secretbox,
        background=False,
    )
    n.send(Notification("mount.error", "NetMover: Anbindung „NAS“ gestört", "weg"))
    assert len(hooks) == 1 and len(mails) == 1
    _cfg_used, password, subject, body = mails[0]
    assert password == "Pa$$,wort" and subject.endswith("gestört") and body == "weg"


def test_mail_only_and_mode_off(
    session_factory: sessionmaker[Session], secretbox: SecretBox
) -> None:
    mails: list[Any] = []
    n = Notifier(
        session_factory, mailer=lambda *a: mails.append(a), secrets=secretbox, background=False
    )
    save_settings(session_factory, _cfg("starttls", 587, notify_mode=NotifyMode.OFF))
    n.send(Notification("x", "t", "m"))
    save_settings(session_factory, _cfg("starttls", 587, email_enabled=False))
    n.send(Notification("x", "t", "m"))
    assert mails == []
    save_settings(session_factory, _cfg("starttls", 587))  # nur E-Mail, kein Webhook
    n.send(Notification("x", "t", "m"))
    assert len(mails) == 1


def test_mail_failure_is_swallowed(
    session_factory: sessionmaker[Session], secretbox: SecretBox
) -> None:
    def broken(*a: Any) -> None:
        raise ConnectionRefusedError("weg")

    save_settings(session_factory, _cfg("starttls", 587))
    Notifier(session_factory, mailer=broken, secrets=secretbox, background=False).send(
        Notification("x", "t", "m")
    )


# -- Einstellungsseite ------------------------------------------------------------------------


def _form(**kw: str) -> dict[str, str]:
    data = {
        "retention_days": "30",
        "notify_mode": "every",
        "notify_after": "3",
        "webhook_url": "",
        "webhook_content_type": "application/json",
        "webhook_template": "$message",
        "email_enabled": "on",
        "smtp_host": "smtp.example.org",
        "smtp_port": "587",
        "smtp_security": "starttls",
        "smtp_user": "netmover",
        "smtp_password": "Geheim,123!",
        "smtp_from": "netmover@example.org",
        "smtp_to": "ich@example.org",
    }
    data.update(kw)
    return data


def test_settings_store_encrypted_password(client: TestClient, state: Any) -> None:
    assert post(client, "/settings", _form()).status_code == 303
    cfg = load_settings(state.session_factory)
    assert cfg.email_enabled and "Geheim" not in cfg.smtp_password_enc
    assert state.secrets.decrypt(cfg.smtp_password_enc) == "Geheim,123!"
    page = client.get("/settings").text
    assert "Geheim,123!" not in page and 'placeholder="••••••"' in page
    # leer lassen = unverändert
    post(client, "/settings", _form(smtp_password=""))
    assert (
        state.secrets.decrypt(load_settings(state.session_factory).smtp_password_enc)
        == "Geheim,123!"
    )


def test_settings_validation(client: TestClient) -> None:
    r = post(
        client,
        "/settings",
        _form(smtp_to="ich@example.org, quatsch", smtp_port="99999", smtp_from="kein-absender"),
    )
    assert r.status_code == 400
    for msg in ("Ungültige Adresse: quatsch", "Port zwischen", "Keine gültige E-Mail-Adresse"):
        assert msg in r.text


def test_mail_test_button(
    client: TestClient, state: Any, tls_files: tuple[Path, Path], trust: None
) -> None:
    controller, inbox, port = _server("starttls", tls_files)
    try:
        post(
            client,
            "/settings",
            _form(smtp_host="localhost", smtp_port=str(port), smtp_password="Pa$$,wort"),
        )
        r = post(client, "/settings/mail-test")
        assert "Gesendet an ich@example.org" in r.text
        post(
            client,
            "/settings",
            _form(smtp_host="localhost", smtp_port=str(port), smtp_password="falsch"),
        )
        r = post(client, "/settings/mail-test")
        assert "Fehlgeschlagen" in r.text and "falsch" not in r.text
    finally:
        controller.stop()
    assert len(inbox.messages) == 1
