"""0.8.0: Anbindungen kopieren – inklusive Zugangsdaten."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from netmover.admin import MountInput, copy_name, delete_mount, save_mount
from netmover.crypto import SecretBox
from netmover.models import Credential, CredentialKind, Mount, MountType
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager
from tests.webutil import post


def _cifs(**kw: object) -> MountInput:
    data: dict[str, object] = {
        "name": "NAS",
        "type": MountType.CIFS,
        "server": "10.0.10.6",
        "share": "Scans",
        "username": "scanner",
        "password": "Pa$$wort",
    }
    data.update(kw)
    return MountInput(**data)  # type: ignore[arg-type]


def test_copy_reuses_own_password_as_independent_copy(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    src, _ = save_mount(session, secretbox, manager, _cifs())
    assert src is not None and src.credential is not None
    copy, err = save_mount(
        session,
        secretbox,
        manager,
        _cifs(name="Archiv", share="Archiv", password=""),
        copy_from=src,
    )
    assert not err and copy is not None and copy.credential is not None
    assert copy.credential.id != src.credential.id and not copy.credential.managed
    assert secretbox.decrypt(copy.credential.secret_enc) == "Pa$$wort"
    assert copy.credential.username == "scanner"
    # Vorlage löschen – die Kopie behält ihre Zugangsdaten
    delete_mount(session, manager, src.id)
    session.flush()
    assert secretbox.decrypt(copy.credential.secret_enc) == "Pa$$wort"


def test_copy_with_new_password_uses_new_one(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    src, _ = save_mount(session, secretbox, manager, _cifs())
    copy, _ = save_mount(
        session, secretbox, manager, _cifs(name="B", share="B", password="anders"), copy_from=src
    )
    assert copy is not None and copy.credential is not None
    assert secretbox.decrypt(copy.credential.secret_enc) == "anders"


def test_copy_with_stored_credentials_shares_them(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    from netmover.credentials import create_password

    cred = create_password(session, secretbox, "NAS-Scanner", "scanner", "x")
    src, _ = save_mount(
        session, secretbox, manager, _cifs(username="", password="", cred_id=str(cred.id))
    )
    copy, _ = save_mount(
        session,
        secretbox,
        manager,
        _cifs(name="B", share="B", username="", password="", cred_id=str(cred.id)),
        copy_from=src,
    )
    assert (
        copy is not None and copy.credential is cred and src is not None and src.credential is cred
    )


def test_copy_sftp_keeps_keypair_and_host_key_only_for_same_server(
    session: Session, secretbox: SecretBox, manager: MountManager
) -> None:
    sftp = {
        "type": MountType.SFTP,
        "server": "10.0.10.17",
        "share": "/consume",
        "port": 22,
        "username": "paperless",
        "auth": CredentialKind.SSH_KEY,
        "password": "",
    }
    src, _ = save_mount(session, secretbox, manager, _cifs(name="Paperless", **sftp))
    assert src is not None and src.credential is not None
    src.host_key = "ssh-ed25519 AAAAhost"
    same, _ = save_mount(
        session,
        secretbox,
        manager,
        _cifs(name="Paperless 2", **{**sftp, "share": "/consume2"}),
        copy_from=src,
    )
    other, _ = save_mount(
        session,
        secretbox,
        manager,
        _cifs(name="Anderer", **{**sftp, "server": "10.0.10.99"}),
        copy_from=src,
    )
    assert same is not None and other is not None
    for m in (same, other):
        assert m.credential is not None and m.credential.id != src.credential.id
        assert m.credential.public_key == src.credential.public_key  # gleiches Schlüsselpaar
        assert m.credential.secret_enc == src.credential.secret_enc
    assert same.host_key == "ssh-ed25519 AAAAhost" and other.host_key is None


def test_copy_name(session: Session, secretbox: SecretBox, manager: MountManager) -> None:
    save_mount(session, secretbox, manager, _cifs())
    assert copy_name(session, "NAS") == "NAS (Kopie)"
    save_mount(session, secretbox, manager, _cifs(name="NAS (Kopie)", share="x"))
    assert copy_name(session, "NAS") == "NAS (Kopie 2)"


# -- Oberfläche --------------------------------------------------------------------------------


MOUNT = {
    "name": "NAS",
    "type": "cifs",
    "server": "nas",
    "share": "scans",
    "username": "scanner",
    "password": "Geheim,123",
    "auto_mount": "on",
    "enabled": "on",
}


def _mid(state: Any, name: str) -> int:
    with state.session_factory() as s:
        mid = s.scalar(select(Mount.id).where(Mount.name == name))
        assert mid is not None
        return mid


def test_copy_button_form_test_and_save(
    client: TestClient, state: Any, backend: FakeMountBackend
) -> None:
    backend.export("nas", "scans")
    backend.export("nas", "archiv")
    post(client, "/mounts/new", MOUNT)
    src_id = _mid(state, "NAS")
    assert f'href="/mounts/{src_id}/copy"' in client.get("/mounts").text

    form = client.get(f"/mounts/{src_id}/copy").text
    assert "Kopie von „NAS“" in form and 'value="NAS (Kopie)"' in form
    assert 'value="scanner"' in form and "Geheim,123" not in form
    assert f'name="copy_from" value="{src_id}"' in form

    # Testen mit übernommenem Passwort (Feld leer)
    copy_form = {
        **MOUNT,
        "name": "NAS (Kopie)",
        "share": "archiv",
        "password": "",
        "copy_from": str(src_id),
    }
    r = post(client, "/mounts/test", copy_form)
    assert "Einhängen erfolgreich" in r.text
    spec = [c for c in backend.calls if c.action == "mount"][-1].spec
    assert spec is not None and spec.credentials is not None
    assert spec.credentials.password == "Geheim,123"

    r = post(client, f"/mounts/{src_id}/copy", copy_form)
    assert r.status_code == 303, r.text
    with state.session_factory() as s:
        copy = s.get(Mount, _mid(state, "NAS (Kopie)"))
        assert copy is not None and copy.share == "archiv" and copy.slug == "nas-kopie"
        assert copy.credential is not None
        assert state.secrets.decrypt(copy.credential.secret_enc) == "Geheim,123"
        assert len(s.scalars(select(Credential)).all()) == 2


def test_copy_validation_error_keeps_copy_context(client: TestClient, state: Any) -> None:
    post(client, "/mounts/new", MOUNT)
    src_id = _mid(state, "NAS")
    r = post(client, f"/mounts/{src_id}/copy", {**MOUNT, "name": "NAS", "password": ""})
    assert r.status_code == 400 and "bereits vergeben" in r.text and "Kopie von „NAS“" in r.text
