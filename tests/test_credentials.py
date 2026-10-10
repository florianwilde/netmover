"""Schlüsselverwaltung: erzeugen, importieren, auswählen, schützen."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.admin import AdminError, MountInput, delete_mount, save_mount
from netmover.credentials import (
    delete_key,
    fingerprint,
    generate_key,
    import_key,
    list_keys,
    rename_key,
)
from netmover.crypto import SecretBox, generate_ssh_keypair
from netmover.db import session_scope
from netmover.models import Credential, CredentialKind, Mount, MountType
from netmover.mounting.manager import MountConfig, MountManager
from tests.sftp_stub import StubSFTPServer


def _sftp_input(**kw: object) -> MountInput:
    data: dict[str, object] = {
        "name": "paperless",
        "type": MountType.SFTP,
        "server": "127.0.0.1",
        "share": "/consume",
        "port": 22,
        "username": "paperless",
        "auth": CredentialKind.SSH_KEY,
    }
    data.update(kw)
    return MountInput(**data)  # type: ignore[arg-type]


# -- Modul --------------------------------------------------------------------------------------


def test_generate_key(session: Session, secretbox: SecretBox) -> None:
    key = generate_key(session, secretbox, "paperless-lxc")
    assert key.managed and key.kind is CredentialKind.SSH_KEY
    assert key.public_key and key.public_key.startswith("ssh-ed25519 ")
    assert key.public_key.endswith(" netmover:paperless-lxc")
    assert "PRIVATE" not in key.public_key
    assert "OPENSSH PRIVATE KEY" in secretbox.decrypt(key.secret_enc)
    info = list_keys(session)[0]
    assert info.fingerprint.startswith("SHA256:") and info.used_by == []
    assert info.authorized_keys_line.startswith(
        'restrict,command="/usr/lib/openssh/sftp-server" ssh-ed25519 '
    )


def test_fingerprint_matches_ssh_keygen(tmp_path: Path) -> None:
    _, pub = generate_ssh_keypair("x")
    f = tmp_path / "k.pub"
    f.write_text(pub + "\n")
    try:
        out = subprocess.run(
            ["ssh-keygen", "-lf", str(f)],  # noqa: S607 – Abgleich mit OpenSSH
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        pytest.skip("ssh-keygen nicht installiert")
    assert fingerprint(pub) == out.split()[1]


@pytest.mark.parametrize(
    "kind", ["ed25519-openssh-pass", "rsa-pem", "ecdsa-pem-pass", "rsa-openssh"]
)
def test_import_formats(session: Session, secretbox: SecretBox, kind: str) -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if kind.startswith("ed25519"):
        key = ed25519.Ed25519PrivateKey.generate()
    elif kind.startswith("rsa"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # type: ignore[assignment]
    else:
        key = ec.generate_private_key(ec.SECP256R1())  # type: ignore[assignment]
    enc = (
        serialization.BestAvailableEncryption(b"geheim")
        if kind.endswith("pass")
        else serialization.NoEncryption()
    )
    fmt = (
        serialization.PrivateFormat.OpenSSH
        if "openssh" in kind
        else serialization.PrivateFormat.PKCS8
    )
    pem = key.private_bytes(serialization.Encoding.PEM, fmt, enc).decode()
    cred = import_key(session, secretbox, kind, pem, "geheim" if kind.endswith("pass") else "")
    expected_pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    assert cred.public_key is not None and cred.public_key.startswith(expected_pub)
    stored = secretbox.decrypt(cred.secret_enc)
    assert "OPENSSH PRIVATE KEY" in stored
    # ohne Passphrase gespeichert → von paramiko direkt nutzbar
    from netmover.mounting.sftp import load_private_key

    assert load_private_key(stored).get_base64() == expected_pub.split()[1]


def test_import_errors(session: Session, secretbox: SecretBox) -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    protected = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.BestAvailableEncryption(b"richtig"),
    ).decode()
    with pytest.raises(AdminError, match="Passphrase angeben"):
        import_key(session, secretbox, "a", protected, "")
    with pytest.raises(AdminError, match="Passphrase falsch"):
        import_key(session, secretbox, "a", protected, "falsch")
    _, pub = generate_ssh_keypair()
    with pytest.raises(AdminError, match="öffentlicher Schlüssel"):
        import_key(session, secretbox, "a", pub)
    with pytest.raises(AdminError, match="Kein lesbarer"):
        import_key(
            session, secretbox, "a", "-----BEGIN PRIVATE KEY-----\nMüll\n-----END PRIVATE KEY-----"
        )
    weak = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505 – absichtlich schwach
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(AdminError, match="zu schwach"):
        import_key(session, secretbox, "a", weak)
    assert list_keys(session) == []


def test_names(session: Session, secretbox: SecretBox) -> None:
    generate_key(session, secretbox, "eins")
    with pytest.raises(AdminError, match="existieren bereits"):
        generate_key(session, secretbox, "eins")
    with pytest.raises(AdminError, match="reserviert"):
        generate_key(session, secretbox, "mount:x")
    zwei = generate_key(session, secretbox, "zwei")
    rename_key(session, zwei.id, "drei")
    assert [k.name for k in list_keys(session)] == ["drei", "eins"]
    with pytest.raises(AdminError):
        rename_key(session, zwei.id, "eins")


# -- Zusammenspiel mit Anbindungen ----------------------------------------------------------


def test_mount_uses_shared_key_with_own_username(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    key = generate_key(session, secretbox, "lxc")
    a, err = save_mount(session, secretbox, manager, _sftp_input(key_id=str(key.id)))
    assert not err and a is not None
    b, err = save_mount(
        session,
        secretbox,
        manager,
        _sftp_input(name="anderer", server="10.0.0.9", username="root", key_id=str(key.id)),
    )
    assert not err and b is not None
    assert a.credential is key and b.credential is key
    assert (a.username, b.username) == ("paperless", "root")
    assert key.username == ""  # der Schlüssel selbst bleibt unverändert
    assert list_keys(session)[0].used_by == ["anderer", "paperless"]

    # Schlüssel in Benutzung → nicht löschbar
    with pytest.raises(AdminError, match="noch benutzt"):
        delete_key(session, key.id)
    # Anbindung löschen lässt den gespeicherten Schlüssel stehen
    delete_mount(session, manager, a.id)
    delete_mount(session, manager, b.id)
    assert session.get(Credential, key.id) is not None
    delete_key(session, key.id)
    assert session.get(Credential, key.id) is None


def test_switching_between_own_and_shared_key(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    m, _ = save_mount(session, secretbox, manager, _sftp_input())  # eigener Schlüssel
    assert m is not None and m.credential is not None and not m.credential.managed
    own_id = m.credential.id
    key = generate_key(session, secretbox, "geteilt")
    m, _ = save_mount(session, secretbox, manager, _sftp_input(key_id=str(key.id)), m.id)
    assert m is not None and m.credential is key
    assert session.get(Credential, own_id) is None  # eigener Schlüssel aufgeräumt
    # Wechsel auf Passwort darf den gespeicherten Schlüssel nicht verändern
    secret_before = key.secret_enc
    m, _ = save_mount(
        session,
        secretbox,
        manager,
        _sftp_input(auth=CredentialKind.PASSWORD, password="pw123"),
        m.id,
    )
    assert m is not None and m.credential is not None and m.credential is not key
    assert not m.credential.managed and m.credential.username == "paperless"
    assert m.username is None
    assert key.secret_enc == secret_before and key.kind is CredentialKind.SSH_KEY


def test_unknown_or_foreign_key_id_is_rejected(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    own, _ = save_mount(session, secretbox, manager, _sftp_input(name="a"))
    assert own is not None and own.credential is not None
    for bad in ("4711", str(own.credential.id)):  # nicht vorhanden / kein gespeicherter Schlüssel
        m, err = save_mount(session, secretbox, manager, _sftp_input(name="b", key_id=bad))
        assert m is None and "key_id" in err


@pytest.fixture
def server(tmp_path: Path) -> Iterator[StubSFTPServer]:
    root = tmp_path / "sftp-root"
    (root / "consume").mkdir(parents=True)
    srv = StubSFTPServer(root, username="paperless").start()
    yield srv
    srv.stop()


def test_manager_connects_with_shared_key_and_mount_username(
    server: StubSFTPServer,
    session_factory: sessionmaker[Session],
    secretbox: SecretBox,
    manager: MountManager,
) -> None:
    with session_scope(session_factory) as s:
        key = import_key(s, secretbox, "lxc", server.client_private_key)
        m, err = save_mount(
            s, secretbox, manager, _sftp_input(port=server.port, key_id=str(key.id))
        )
        assert not err and m is not None
        mid = m.id
    with session_factory() as s:
        mm = s.get(Mount, mid)
        assert mm is not None
        cfg = MountConfig.from_model(mm)
    from netmover.admin import credential_for_test, mount_input_from_model

    with session_factory() as s:
        mm = s.get(Mount, mid)
        assert mm is not None
        data = mount_input_from_model(mm)
        assert data.key_id == str(mm.cred_id) and data.username == "paperless"
        cred = credential_for_test(data, mm, secretbox, s)
    assert cred is not None and cred.username == "paperless"
    assert manager.check(cfg, cred, mount_id=mid).ok
    assert manager.check_alive(mid) is None  # Regellauf-Weg: Benutzer der Anbindung

    # Gegenprobe: falscher Benutzer an der Anbindung → Anmeldung abgelehnt
    with session_scope(session_factory) as s:
        mm = s.get(Mount, mid)
        assert mm is not None
        mm.username = "jemand"
    from netmover.mounting.manager import MountUnavailable

    with pytest.raises(MountUnavailable, match="Anmeldung abgelehnt"):
        manager.check_alive(mid)


def test_list_keys_ignores_per_mount_credentials(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    save_mount(session, secretbox, manager, _sftp_input())
    assert list_keys(session) == []
    assert session.scalars(select(Credential)).one().managed is False


# -- Gespeicherte Passwörter (0.5.0) ----------------------------------------------------------


def _cifs(**kw: object) -> MountInput:
    data: dict[str, object] = {
        "name": "nas",
        "type": MountType.CIFS,
        "server": "10.0.10.6",
        "share": "scans",
    }
    data.update(kw)
    return MountInput(**data)  # type: ignore[arg-type]


def test_stored_password_shared_by_mounts(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    from netmover.credentials import create_password, list_passwords, update_password

    cred = create_password(session, secretbox, "NAS-Scanner", "scanner", "Pa$$,wort", "WG")
    a, err = save_mount(session, secretbox, manager, _cifs(cred_id=str(cred.id)))
    assert not err and a is not None
    b, err = save_mount(
        session, secretbox, manager, _cifs(name="archiv", share="archiv", cred_id=str(cred.id))
    )
    assert not err and b is not None
    assert a.credential is cred and b.credential is cred and a.username is None
    assert list_passwords(session)[0].used_by == ["archiv", "nas"]

    # ein neues Passwort gilt für beide
    update_password(session, secretbox, cred.id, "scanner", "neu!", "WG")
    from netmover.admin import credential_for_test, mount_input_from_model

    for m in (a, b):
        data = mount_input_from_model(m)
        assert data.cred_id == str(cred.id) and data.username == "scanner"
        c = credential_for_test(data, m, secretbox, session)
        assert c is not None and c.secret == "neu!" and c.domain == "WG"

    # Anbindung löschen lässt die gespeicherten Zugangsdaten stehen
    delete_mount(session, manager, a.id)
    from netmover.credentials import delete_credential

    with pytest.raises(AdminError, match="noch benutzt"):
        delete_credential(session, cred.id)
    delete_mount(session, manager, b.id)
    assert delete_credential(session, cred.id) == "NAS-Scanner"


def test_switch_from_own_to_stored_password_cleans_up(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    from netmover.credentials import create_password

    m, _ = save_mount(session, secretbox, manager, _cifs(username="alt", password="pw"))
    assert m is not None and m.credential is not None
    own_id = m.credential.id
    cred = create_password(session, secretbox, "geteilt", "scanner", "x")
    m, err = save_mount(session, secretbox, manager, _cifs(cred_id=str(cred.id)), m.id)
    assert not err and m is not None and m.credential is cred
    assert session.get(Credential, own_id) is None
    # zurück auf eigene Angaben: gespeicherter Eintrag bleibt unverändert
    m, err = save_mount(session, secretbox, manager, _cifs(username="eigen", password="pw2"), m.id)
    assert not err and m is not None and not m.credential.managed  # type: ignore[union-attr]
    assert cred.username == "scanner" and secretbox.decrypt(cred.secret_enc) == "x"


def test_stored_password_validation(session: Session, secretbox: SecretBox) -> None:
    from netmover.credentials import create_password, update_password

    with pytest.raises(AdminError, match="Passwort fehlt"):
        create_password(session, secretbox, "a", "u", "")
    with pytest.raises(AdminError):
        create_password(session, secretbox, "a", "u", "zeile\numbruch")
    cred = create_password(session, secretbox, "a", "u", "pw")
    update_password(session, secretbox, cred.id, "u2", "")  # leer = unverändert
    assert cred.username == "u2" and secretbox.decrypt(cred.secret_enc) == "pw"


def test_stored_password_mount_actually_mounts(
    session_factory: sessionmaker[Session],
    secretbox: SecretBox,
    manager: MountManager,
    fake_backend: object,
) -> None:
    from netmover.credentials import create_password

    fake_backend.export("10.0.10.6", "scans")  # type: ignore[attr-defined]
    with session_scope(session_factory) as s:
        cred = create_password(s, secretbox, "NAS", "scanner", "Pa$$wort", "WG")
        m, _ = save_mount(s, secretbox, manager, _cifs(cred_id=str(cred.id)))
        assert m is not None
        mid = m.id
    manager.mount(mid)
    spec = fake_backend.calls[-1].spec  # type: ignore[attr-defined]
    assert spec.credentials.username == "scanner" and spec.credentials.password == "Pa$$wort"
    assert spec.credentials.domain == "WG"
