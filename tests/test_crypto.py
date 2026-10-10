"""Fallstrick 7: Schlüsseldatei, Verschlüsselung, Schwärzung, SSH-Schlüssel."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import paramiko
import pytest
from cryptography.fernet import Fernet

from netmover.crypto import (
    REDACTED,
    SecretBox,
    SecretError,
    generate_ssh_keypair,
    load_or_create_key,
    redact,
)


def test_key_created_with_0600_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "lib" / "secret.key"
    key1 = load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_key(path) == key1


def test_key_mode_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    path.write_bytes(Fernet.generate_key())
    path.chmod(0o644)
    load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_key_file(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    path.write_text("kaputt")
    path.chmod(0o600)
    with pytest.raises(SecretError):
        load_or_create_key(path)


def test_roundtrip_and_wrong_key() -> None:
    box = SecretBox(Fernet.generate_key())
    token = box.encrypt("geheim")
    assert "geheim" not in token
    assert box.decrypt(token) == "geheim"
    with pytest.raises(SecretError, match=r"secret\.key"):
        SecretBox(Fernet.generate_key()).decrypt(token)


def test_redact() -> None:
    text = "mount error: user scanner pw Sommer2026! failed (Sommer2026)"
    out = redact(text, ["Sommer2026!", "Sommer2026", None, ""])
    assert "Sommer2026" not in out
    assert out.count(REDACTED) == 2


def test_ssh_keypair_usable_by_paramiko() -> None:
    private, public = generate_ssh_keypair("netmover@test")
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(private))
    assert public.startswith("ssh-ed25519 ")
    assert public.split()[1] == key.get_base64()
    assert public.endswith(" netmover@test")
