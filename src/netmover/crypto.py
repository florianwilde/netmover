"""Geheimnisse (Fallstrick 7): Fernet-Verschlüsselung, SSH-Schlüssel, Schwärzung."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from netmover.logging_setup import event, get_logger

log = get_logger(__name__)

REDACTED = "••••••"


class SecretError(RuntimeError):
    pass


def load_or_create_key(path: Path) -> bytes:
    """Liest den Fernet-Schlüssel oder legt ihn beim ersten Start mit Modus 0600 an."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
            event(log, "secret_key.mode_fixed", path=str(path), old_mode=oct(mode))
        key = path.read_bytes().strip()
        try:
            Fernet(key)
        except ValueError as exc:
            raise SecretError(f"{path} enthält keinen gültigen Schlüssel") from exc
        return key
    key = Fernet.generate_key()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    event(log, "secret_key.created", path=str(path))
    return key


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretError(
                "Gespeichertes Geheimnis lässt sich nicht entschlüsseln – "
                "wurde secret.key ausgetauscht?"
            ) from exc


def generate_ssh_keypair(comment: str = "netmover") -> tuple[str, str]:
    """Erzeugt ein Ed25519-Schlüsselpaar: (privat im OpenSSH-Format, öffentlich)."""
    key = Ed25519PrivateKey.generate()
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
    return private, f"{public} {comment}"


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Ersetzt bekannte Geheimnisse, bevor Ausgaben protokolliert oder angezeigt werden."""
    # Längste zuerst, damit Teilstrings eines längeren Geheimnisses nicht stehen bleiben.
    for secret in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text
