"""Fallstrick 5 (Shell-Injection) und 6 (Mountpoints) – Eingabeprüfung."""

from __future__ import annotations

import pytest

from netmover.models import MountType
from netmover.validation import (
    ValidationError,
    parse_extra_options,
    safe_abspath,
    safe_relpath,
    slugify,
    validate_mode,
    validate_secret,
    validate_server,
    validate_share,
)

INJECTION = [
    "nas;rm -rf /",
    "nas && reboot",
    "nas|id",
    "$(id)",
    "`id`",
    "nas\nid",
    "nas,credentials=/etc/shadow",
    "nas name",
    "-oProxyCommand=x",
    "",
    "nas/../etc",
]


@pytest.mark.parametrize("value", INJECTION)
def test_server_rejects_injection(value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_server(value)
    assert exc.value.field == "server"


@pytest.mark.parametrize("value", ["192.168.1.10", "nas", "nas.fritz.box", "ds-920"])
def test_server_accepts_hosts(value: str) -> None:
    assert validate_server(value) == value


def test_server_rejects_ipv6_with_clear_message() -> None:
    with pytest.raises(ValidationError, match="IPv6"):
        validate_server("fe80::1")


@pytest.mark.parametrize(
    "value",
    ["scans,uid=0", "scans $HOME", "scans`id`", "scans\nfoo", "../etc", "sc ans", "a/../b", "C$"],
)
def test_cifs_share_rejects(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_share(MountType.CIFS, value)


def test_cifs_share_normalises_slashes() -> None:
    assert validate_share(MountType.CIFS, "/scans/eingang/") == "scans/eingang"


@pytest.mark.parametrize("value", ["volume1/scans", "/volume1/scans,rw", "/a b", "/a/../b", "/$x"])
def test_nfs_export_rejects(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_share(MountType.NFS4, value)


def test_nfs_export_accepts() -> None:
    assert validate_share(MountType.NFS3, "/volume1/scans/") == "/volume1/scans"


@pytest.mark.parametrize("key", ["credentials", "username", "password", "pass", "user", "domain"])
def test_forbidden_option_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="gesperrt"):
        parse_extra_options(MountType.CIFS, f"{key}=x")


@pytest.mark.parametrize("key", ["uid", "gid", "file_mode", "dir_mode"])
def test_dedicated_option_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="eigenen Feld"):
        parse_extra_options(MountType.CIFS, f"{key}=1000")


@pytest.mark.parametrize(
    "raw",
    [
        "vers=3.0;id",
        "vers=$(id)",
        "vers=`id`",
        "vers=3 0",
        "iocharset=utf8,x-systemd.automount",
        "exec",  # nicht in der Allowlist
        "VERS=3.0=1",
    ],
)
def test_option_values_and_unknown_keys(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_extra_options(MountType.CIFS, raw)


def test_options_allowlist_per_type() -> None:
    with pytest.raises(ValidationError, match="nicht erlaubt"):
        parse_extra_options(MountType.NFS4, "nolock")
    assert [o.render() for o in parse_extra_options(MountType.NFS3, "nolock, timeo=600")] == [
        "nolock",
        "timeo=600",
    ]


def test_options_parse_ok_and_duplicates() -> None:
    opts = parse_extra_options(MountType.CIFS, "vers=3.0, noserverino,\niocharset=utf8")
    assert [o.render() for o in opts] == ["vers=3.0", "noserverino", "iocharset=utf8"]
    with pytest.raises(ValidationError, match="doppelt"):
        parse_extra_options(MountType.CIFS, "vers=3.0,vers=2.1")


def test_nfs4_vers_restricted() -> None:
    assert parse_extra_options(MountType.NFS4, "vers=4.1")[0].value == "4.1"
    with pytest.raises(ValidationError):
        parse_extra_options(MountType.NFS4, "vers=3")


def test_mode() -> None:
    assert validate_mode("file_mode", "644") == "0644"
    assert validate_mode("file_mode", "") is None
    with pytest.raises(ValidationError):
        validate_mode("file_mode", "0999")


def test_secret_rejects_newlines() -> None:
    assert validate_secret("p@ss,w=rd;$`") == "p@ss,w=rd;$`"
    with pytest.raises(ValidationError):
        validate_secret("pass\nusername=root")


def test_slugify() -> None:
    assert slugify("Synology Scans (Büro)") == "synology-scans-buro"
    assert slugify("Straße/../..") == "strasse"
    with pytest.raises(ValidationError):
        slugify("../..")


def test_relpath() -> None:
    assert safe_relpath("p", "/Scans Max/eingang/") == "Scans Max/eingang"
    assert safe_relpath("p", "") == ""
    for bad in ["../x", "a/../../b", "a/./b", "a\0b"]:
        with pytest.raises(ValidationError):
            safe_relpath("p", bad)


def test_abspath() -> None:
    assert safe_abspath("p", "/opt/consume/") == "/opt/consume"
    with pytest.raises(ValidationError):
        safe_abspath("p", "opt/consume")
    with pytest.raises(ValidationError):
        safe_abspath("p", "/opt/../etc")
