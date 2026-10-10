"""Zugangsdaten-Verwaltung: gespeicherte Passwörter und SSH-Schlüssel.

Passwörter (SMB und SFTP): Benutzer, Passwort, optional Domäne – einmal gepflegt,
von beliebig vielen Anbindungen genutzt. Ein neues Passwort gilt sofort für alle.

SSH-Schlüssel:

* Erzeugen (Ed25519) oder Importieren (Ed25519, ECDSA, RSA ≥ 2048 Bit; OpenSSH-
  oder PEM-Format, optional mit Passphrase). Eine Passphrase wird beim Import
  einmal benutzt; gespeichert wird der Schlüssel ohne Passphrase, aber mit
  Fernet verschlüsselt – wie alle Geheimnisse.
* Der private Schlüssel verlässt NetMover nie: Die Oberfläche zeigt nur den
  öffentlichen Schlüssel und den Fingerabdruck.
* Ein Schlüssel kann von mehreren Anbindungen genutzt werden; den Benutzernamen
  legt jede Anbindung selbst fest. Löschen geht nur, solange ihn keine nutzt.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from sqlalchemy import select
from sqlalchemy.orm import Session

from netmover.admin import AdminError
from netmover.crypto import SecretBox
from netmover.models import Credential, CredentialKind, Mount

MAX_KEY_BYTES = 16 * 1024
MIN_RSA_BITS = 2048
SFTP_ONLY_PREFIX = 'restrict,command="/usr/lib/openssh/sftp-server" '


@dataclass
class KeyInfo:
    id: int
    name: str
    key_type: str
    fingerprint: str
    public_key: str
    created_at: datetime
    used_by: list[str] = field(default_factory=list)

    @property
    def authorized_keys_line(self) -> str:
        """Zeile für ~/.ssh/authorized_keys, auf SFTP beschränkt."""
        return SFTP_ONLY_PREFIX + self.public_key


def fingerprint(public_key: str) -> str:
    """SHA256-Fingerabdruck wie ``ssh-keygen -lf``."""
    blob = base64.b64decode(public_key.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _validate_name(s: Session, name: str, exclude_id: int | None = None) -> str:
    name = name.strip()
    if not name:
        raise AdminError("Name fehlt.")
    if len(name) > 100:
        raise AdminError("Name höchstens 100 Zeichen.")
    if name.startswith("mount:"):
        raise AdminError("Namen mit „mount:“ sind für NetMover reserviert.")
    dup = s.scalar(select(Credential.id).where(Credential.name == name))
    if dup is not None and dup != exclude_id:
        raise AdminError(f"Zugangsdaten mit dem Namen „{name}“ existieren bereits.")
    return name


def _comment(name: str) -> str:
    return "netmover:" + "".join(c if c.isalnum() or c in "-_." else "-" for c in name)


PrivateKey = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey


def _store(s: Session, secrets: SecretBox, name: str, key: PrivateKey) -> Credential:
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    cred = Credential(
        name=name,
        kind=CredentialKind.SSH_KEY,
        username="",
        secret_enc=secrets.encrypt(private),
        public_key=f"{public} {_comment(name)}",
        managed=True,
    )
    s.add(cred)
    s.flush()
    return cred


def generate_key(s: Session, secrets: SecretBox, name: str) -> Credential:
    return _store(s, secrets, _validate_name(s, name), ed25519.Ed25519PrivateKey.generate())


def import_key(
    s: Session, secrets: SecretBox, name: str, private_key: str, passphrase: str = ""
) -> Credential:
    name = _validate_name(s, name)
    data = private_key.strip().encode()
    if not data:
        raise AdminError("Privaten Schlüssel einfügen.")
    if len(data) > MAX_KEY_BYTES:
        raise AdminError("Schlüssel ist zu groß.")
    if b"PUBLIC KEY" in data or data.startswith((b"ssh-", b"ecdsa-")):
        raise AdminError("Das ist ein öffentlicher Schlüssel – benötigt wird der private.")
    password = passphrase.encode() if passphrase else None
    loaders = (
        [serialization.load_ssh_private_key]
        if b"OPENSSH PRIVATE KEY" in data
        else [serialization.load_pem_private_key]
    )
    try:
        key = loaders[0](data, password)
    except TypeError:
        raise AdminError(
            "Der Schlüssel ist mit einer Passphrase geschützt – bitte Passphrase angeben."
            if password is None
            else "Der Schlüssel hat keine Passphrase – Feld bitte leer lassen."
        ) from None
    except ValueError as exc:
        msg = str(exc).lower()
        if password is not None or "password" in msg or "decrypt" in msg:
            # Bei OpenSSH-Schlüsseln meldet cryptography eine falsche Passphrase
            # als „Corrupt data“ – beides lässt sich nicht unterscheiden.
            raise AdminError("Passphrase falsch (oder Schlüssel beschädigt).") from None
        raise AdminError(
            "Kein lesbarer privater Schlüssel (erwartet OpenSSH- oder PEM-Format)."
        ) from None
    except Exception:  # unbekannte Formate/Algorithmen sauber melden
        raise AdminError("Schlüsselformat wird nicht unterstützt.") from None
    if not isinstance(
        key, ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey
    ):
        raise AdminError("Unterstützt werden Ed25519, ECDSA und RSA.")
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size < MIN_RSA_BITS:
        raise AdminError(f"RSA-Schlüssel mit {key.key_size} Bit ist zu schwach (mind. 2048).")
    return _store(s, secrets, name, key)


def _users(s: Session, key_id: int) -> list[str]:
    # Direkt abfragen statt key.mounts: die Beziehung ist nach dem Löschen von
    # Anbindungen in derselben Session veraltet (passive_deletes).
    return sorted(s.scalars(select(Mount.name).where(Mount.cred_id == key_id)))


# -- Passwörter -------------------------------------------------------------------------------


@dataclass
class PasswordInfo:
    id: int
    name: str
    username: str
    domain: str | None
    created_at: datetime
    used_by: list[str] = field(default_factory=list)


def _validate_login(username: str, password: str | None, domain: str) -> None:
    from netmover.validation import ValidationError, validate_secret, validate_username

    try:
        validate_username(username)
        if domain:
            validate_username(domain)
        if password is not None:
            validate_secret(password)
    except ValidationError as exc:
        raise AdminError(str(exc)) from None


def create_password(
    s: Session, secrets: SecretBox, name: str, username: str, password: str, domain: str = ""
) -> Credential:
    name = _validate_name(s, name)
    username, domain = username.strip(), domain.strip()
    if not password:
        raise AdminError("Passwort fehlt.")
    _validate_login(username, password, domain)
    cred = Credential(
        name=name,
        kind=CredentialKind.PASSWORD,
        username=username,
        domain=domain or None,
        secret_enc=secrets.encrypt(password),
        managed=True,
    )
    s.add(cred)
    s.flush()
    return cred


def update_password(
    s: Session, secrets: SecretBox, cred_id: int, username: str, password: str, domain: str = ""
) -> Credential:
    """Benutzer/Domäne ändern; leeres Passwort = unverändert."""
    cred = managed_password(s, cred_id)
    username, domain = username.strip(), domain.strip()
    _validate_login(username, password or None, domain)
    cred.username = username
    cred.domain = domain or None
    if password:
        cred.secret_enc = secrets.encrypt(password)
    return cred


def managed_password(s: Session, cred_id: int) -> Credential:
    cred = s.get(Credential, cred_id)
    if cred is None or not cred.managed or cred.kind is not CredentialKind.PASSWORD:
        raise AdminError("Zugangsdaten existieren nicht.")
    return cred


def list_passwords(s: Session) -> list[PasswordInfo]:
    rows = s.scalars(
        select(Credential)
        .where(Credential.managed.is_(True), Credential.kind == CredentialKind.PASSWORD)
        .order_by(Credential.name)
    ).all()
    return [
        PasswordInfo(c.id, c.name, c.username, c.domain, c.created_at, _users(s, c.id))
        for c in rows
    ]


def delete_credential(s: Session, cred_id: int) -> str:
    """Gespeicherte Zugangsdaten (Passwort oder Schlüssel) löschen – nur unbenutzt."""
    cred = s.get(Credential, cred_id)
    if cred is None or not cred.managed:
        raise AdminError("Zugangsdaten existieren nicht.")
    users = _users(s, cred_id)
    if users:
        raise AdminError(f"„{cred.name}“ wird noch benutzt von: {', '.join(users)}")
    name = cred.name
    s.delete(cred)
    s.flush()
    return name


def rename_credential(s: Session, cred_id: int, name: str) -> None:
    cred = s.get(Credential, cred_id)
    if cred is None or not cred.managed:
        raise AdminError("Zugangsdaten existieren nicht.")
    cred.name = _validate_name(s, name, exclude_id=cred_id)


# -- SSH-Schlüssel ------------------------------------------------------------------------------


def list_keys(s: Session) -> list[KeyInfo]:
    keys = s.scalars(
        select(Credential)
        .where(Credential.managed.is_(True), Credential.kind == CredentialKind.SSH_KEY)
        .order_by(Credential.name)
    ).all()
    result = []
    for k in keys:
        pub = k.public_key or ""
        result.append(
            KeyInfo(
                id=k.id,
                name=k.name,
                key_type=pub.split()[0] if pub else "?",
                fingerprint=fingerprint(pub) if pub else "?",
                public_key=pub,
                created_at=k.created_at,
                used_by=_users(s, k.id),
            )
        )
    return result


def managed_key(s: Session, key_id: int) -> Credential:
    key = s.get(Credential, key_id)
    if key is None or not key.managed or key.kind is not CredentialKind.SSH_KEY:
        raise AdminError("Schlüssel existiert nicht.")
    return key


def rename_key(s: Session, key_id: int, name: str) -> None:
    key = managed_key(s, key_id)
    key.name = _validate_name(s, name, exclude_id=key_id)


def delete_key(s: Session, key_id: int) -> str:
    key = managed_key(s, key_id)
    users = _users(s, key_id)
    if users:
        raise AdminError(f"Schlüssel „{key.name}“ wird noch benutzt von: {', '.join(users)}")
    name = key.name
    s.delete(key)
    s.flush()
    return name
