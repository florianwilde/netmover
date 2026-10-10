"""SFTP-Ziel gegen einen echten (im Prozess laufenden) SFTP-Server."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netmover.crypto import SecretBox
from netmover.db import session_scope
from netmover.models import Credential, CredentialKind, Mount, MountState, MountType
from netmover.mounting.manager import MountManager, MountUnavailable
from netmover.mounting.sftp import (
    HostKeyMismatch,
    HostKeyUnknown,
    SftpConfig,
    SftpError,
    check_connection,
    connect,
)
from tests.sftp_stub import StubSFTPServer


@pytest.fixture
def server(tmp_path: Path) -> Iterator[StubSFTPServer]:
    root = tmp_path / "sftp-root"
    (root / "consume").mkdir(parents=True)
    srv = StubSFTPServer(root, password="pw").start()
    yield srv
    srv.stop()


def _cfg(server: StubSFTPServer, **kw: object) -> SftpConfig:
    data: dict[str, object] = {
        "host": "127.0.0.1",
        "port": server.port,
        "username": server.username,
        "base_path": "/consume",
        "private_key": server.client_private_key,
    }
    data.update(kw)
    return SftpConfig(**data)  # type: ignore[arg-type]


def test_check_connection_with_key_tofu(server: StubSFTPServer) -> None:
    report = check_connection(_cfg(server))
    assert [(s.name, s.ok) for s in report.steps] == [
        ("connect", True),
        ("read", True),
        ("write", True),
    ]
    assert report.host_key == server.host_key_string
    assert report.host_key_fingerprint and report.host_key_fingerprint.startswith("SHA256:")
    assert "neu" in report.steps[0].message
    assert report.can_write and report.can_delete
    # Stub kennt kein statvfs → Hinweis statt Fehler
    assert report.free_bytes is None
    assert any("statvfs" in h for h in report.hints)
    assert list((server.root / "consume").iterdir()) == []


def test_password_auth(server: StubSFTPServer) -> None:
    report = check_connection(
        _cfg(server, private_key=None, password="pw", host_key=server.host_key_string)
    )
    assert report.ok


def test_wrong_credentials(server: StubSFTPServer) -> None:
    report = check_connection(_cfg(server, private_key=None, password="falsch"))
    assert report.steps[0].ok is False
    assert "authorized_keys" in report.steps[0].message
    assert "falsch" not in report.steps[0].message


def test_unknown_host_key_refused_outside_test(server: StubSFTPServer) -> None:
    with pytest.raises(HostKeyUnknown):
        connect(_cfg(server))


def test_host_key_mismatch(server: StubSFTPServer) -> None:
    other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOmvG9t0hJ7oC2Y3Fq6m0eKZ1x2y3z4A5B6C7D8E9F0G"
    with pytest.raises(HostKeyMismatch, match="geändert"):
        connect(_cfg(server, host_key=other))
    report = check_connection(_cfg(server, host_key=other))
    assert report.steps[0].ok is False


def test_missing_base_path(server: StubSFTPServer) -> None:
    report = check_connection(_cfg(server, base_path="/fehlt"))
    assert [(s.name, s.ok) for s in report.steps] == [
        ("connect", True),
        ("read", False),
        ("write", None),
    ]


def test_unreachable() -> None:
    with pytest.raises(SftpError, match="fehlgeschlagen"):
        connect(SftpConfig("127.0.0.1", 1, "x", "/", password="x"), timeout=2)


def test_manager_pins_host_key_and_checks_alive(
    server: StubSFTPServer,
    manager: MountManager,
    session_factory: sessionmaker[Session],
    secretbox: SecretBox,
) -> None:
    with session_scope(session_factory) as s:
        c = Credential(
            name="paperless-key",
            kind=CredentialKind.SSH_KEY,
            username=server.username,
            secret_enc=secretbox.encrypt(server.client_private_key),
            public_key=server.client_public_key,
        )
        s.add(c)
        s.flush()
        m = Mount(
            name="paperless",
            slug="paperless",
            type=MountType.SFTP,
            server="127.0.0.1",
            share="/consume",
            port=server.port,
            cred_id=c.id,
        )
        s.add(m)
        s.flush()
        mid = m.id

    # vor dem ersten Test: Hostschlüssel unbekannt → kein Lauf möglich
    with pytest.raises(MountUnavailable, match="Hostschlüssel"):
        manager.check_alive(mid)

    from netmover.mounting.manager import CredentialData, MountConfig

    with session_factory() as s:
        mm = s.get(Mount, mid)
        assert mm is not None
        cfg = MountConfig.from_model(mm)
    cred = CredentialData(CredentialKind.SSH_KEY, server.username, server.client_private_key)
    assert manager.check(cfg, cred, mount_id=mid).ok

    with session_factory() as s:
        mm = s.get(Mount, mid)
        assert mm is not None and mm.host_key == server.host_key_string

    assert manager.check_alive(mid) is None
    with session_factory() as s:
        mm = s.get(Mount, mid)
        assert mm is not None and mm.state is MountState.MOUNTED
