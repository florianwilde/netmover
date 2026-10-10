"""Validierung aller Eingaben, die in einen ``mount``-Aufruf oder einen Pfad fließen.

Fallstrick 5 (Shell-Injection): Server, Freigabe und Zusatzoptionen kommen aus
einem Webformular. Sie werden hier gegen strikte Muster geprüft; Optionen nur
gegen eine Allowlist erlaubter Schlüssel. Zusätzlich ruft das Backend ``mount``
ausschließlich mit Argumentliste auf – beides zusammen, nicht eins von beiden.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from netmover.models import MountType


class ValidationError(ValueError):
    """Eingabe ungültig. ``field`` benennt das Formularfeld, ``str(exc)`` den Grund."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
# Freigabename, optional mit Unterpfad: "scans" oder "scans/eingang"
_CIFS_SHARE_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
# Absoluter Exportpfad bzw. entfernter Basispfad: "/volume1/scans"
_ABS_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+/?)*$")
_OPT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_OPT_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/@-]+$")
_MODE_RE = re.compile(r"^0?[0-7]{3}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@$\\-]{1,255}$")

# Schlüssel, die ausschließlich das Tool setzt.
FORBIDDEN_OPTION_KEYS = frozenset(
    {"credentials", "cred", "username", "user", "password", "password2", "pass", "domain"}
)
# Schlüssel mit eigenem Formularfeld.
DEDICATED_OPTION_KEYS = frozenset({"uid", "gid", "file_mode", "dir_mode"})

_COMMON_FLAGS = frozenset({"ro", "rw", "noexec", "nosuid", "nodev", "hard", "soft", "noatime"})

ALLOWED_OPTIONS: dict[MountType, frozenset[str]] = {
    MountType.CIFS: _COMMON_FLAGS
    | {
        "vers", "sec", "iocharset", "noperm", "nounix", "serverino", "noserverino",
        "cache", "actimeo", "rsize", "wsize", "seal", "nobrl", "mfsymlinks",
        "forceuid", "forcegid", "noacl", "echo_interval", "handlecache",
        "nohandlecache", "nocase", "port", "max_credits", "retrans",
    },
    MountType.NFS4: _COMMON_FLAGS
    | {
        "vers", "proto", "port", "timeo", "retrans", "rsize", "wsize", "ac", "noac",
        "actimeo", "lookupcache", "nconnect", "sec", "softerr", "fsc", "clientaddr",
    },
    MountType.NFS3: _COMMON_FLAGS
    | {
        "proto", "port", "timeo", "retrans", "rsize", "wsize", "ac", "noac",
        "actimeo", "lookupcache", "nconnect", "sec", "nolock", "local_lock",
        "mountport", "mountproto", "softerr", "fsc",
    },
    MountType.SFTP: frozenset(),
}  # fmt: skip

_NFS4_VERSIONS = frozenset({"4", "4.0", "4.1", "4.2"})


def validate_server(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError("server", "Server fehlt.")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 6:
            raise ValidationError(
                "server", "IPv6-Adressen werden nicht unterstützt – bitte Hostname verwenden."
            )
        return value
    if not _HOSTNAME_RE.fullmatch(value):
        raise ValidationError(
            "server", "Ungültiger Servername – erlaubt sind Hostname oder IPv4-Adresse."
        )
    return value


def _no_dot_segments(field: str, value: str) -> None:
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValidationError(field, "„.“ und „..“ sind als Pfadbestandteil nicht erlaubt.")


def validate_share(mount_type: MountType, value: str) -> str:
    value = value.strip()
    if mount_type is MountType.CIFS:
        value = value.strip("/")
        if not _CIFS_SHARE_RE.fullmatch(value):
            raise ValidationError(
                "share",
                "Ungültige Freigabe – erlaubt sind Buchstaben, Ziffern, „.“, „_“, „-“ und "
                "„/“ als Trenner (z. B. „scans“ oder „scans/eingang“).",
            )
    else:
        if len(value) > 1:
            value = value.rstrip("/")
        if not _ABS_PATH_RE.fullmatch(value):
            raise ValidationError(
                "share",
                "Ungültiger Pfad – absolut, nur Buchstaben, Ziffern, „.“, „_“, „-“ "
                "(z. B. „/volume1/scans“).",
            )
    _no_dot_segments("share", value)
    return value


def validate_mode(field: str, value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    if not _MODE_RE.fullmatch(value):
        raise ValidationError(field, "Modus bitte oktal angeben, z. B. „0644“.")
    return value.rjust(4, "0")


def validate_id(field: str, value: int | str | None) -> int | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ValidationError(field, "Bitte eine ganze Zahl angeben.") from None
    if not 0 <= num <= 2**32 - 2:
        raise ValidationError(field, "Wert außerhalb des gültigen Bereichs.")
    return num


def validate_port(value: int | str | None) -> int | None:
    num = validate_id("port", value)
    if num is not None and not 1 <= num <= 65535:
        raise ValidationError("port", "Port muss zwischen 1 und 65535 liegen.")
    return num


def validate_username(value: str) -> str:
    value = value.strip()
    if not _USERNAME_RE.fullmatch(value):
        raise ValidationError("username", "Ungültiger Benutzername.")
    return value


def validate_secret(value: str) -> str:
    """Passwörter dürfen alles enthalten außer Zeilenumbrüchen und NUL – sie landen
    zeilenweise in der Credentials-Datei von ``mount.cifs``."""
    if any(c in value for c in "\r\n\0"):
        raise ValidationError("password", "Passwort darf keine Zeilenumbrüche enthalten.")
    return value


@dataclass(frozen=True)
class MountOption:
    key: str
    value: str | None = None

    def render(self) -> str:
        return self.key if self.value is None else f"{self.key}={self.value}"


def parse_extra_options(mount_type: MountType, raw: str) -> list[MountOption]:
    """Zerlegt „vers=3.0,noserverino“ und prüft jede Option gegen die Allowlist."""
    options: list[MountOption] = []
    seen: set[str] = set()
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        key = key.strip().lower()
        val: str | None = value.strip() if sep else None
        if not _OPT_KEY_RE.fullmatch(key):
            raise ValidationError("extra_options", f"Ungültiger Optionsname: {key!r}")
        if key in FORBIDDEN_OPTION_KEYS:
            raise ValidationError(
                "extra_options",
                f"Option „{key}“ ist gesperrt – Zugangsdaten setzt NetMover selbst.",
            )
        if key in DEDICATED_OPTION_KEYS:
            raise ValidationError("extra_options", f"Option „{key}“ bitte im eigenen Feld angeben.")
        if key not in ALLOWED_OPTIONS[mount_type]:
            raise ValidationError(
                "extra_options",
                f"Option „{key}“ ist für {mount_type.value} nicht erlaubt.",
            )
        if val is not None and not _OPT_VALUE_RE.fullmatch(val):
            raise ValidationError(
                "extra_options",
                f"Ungültiger Wert für „{key}“ – erlaubt sind A–Z, 0–9 und . _ : / @ -",
            )
        if key == "vers" and mount_type is MountType.NFS4 and val not in _NFS4_VERSIONS:
            raise ValidationError("extra_options", "Für nfs4 ist nur vers=4, 4.0, 4.1, 4.2 gültig.")
        if key in seen:
            raise ValidationError("extra_options", f"Option „{key}“ ist doppelt angegeben.")
        seen.add(key)
        options.append(MountOption(key, val))
    return options


def slugify(name: str) -> str:
    """Erzeugt den Verzeichnisnamen unter dem Mount-Basisverzeichnis."""
    text = name.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    text = text[:48].strip("-")
    if not text:
        raise ValidationError(
            "name", "Name muss mindestens einen Buchstaben oder eine Ziffer haben."
        )
    return text


def safe_relpath(field: str, value: str) -> str:
    """Relativer Unterpfad für Regeln. Leerzeichen sind erlaubt (die Pfade gehen nie
    durch eine Shell), Ausbrüche per ``..`` oder absolute Pfade nicht."""
    value = value.strip().strip("/")
    if "\0" in value:
        raise ValidationError(field, "Ungültiges Zeichen im Pfad.")
    if value == "":
        return ""
    _no_dot_segments(field, value)
    return str(PurePosixPath(value))


def safe_abspath(field: str, value: str) -> str:
    value = value.strip()
    if "\0" in value or not value.startswith("/"):
        raise ValidationError(field, "Bitte einen absoluten Pfad angeben.")
    _no_dot_segments(field, value)
    return str(PurePosixPath(value))
