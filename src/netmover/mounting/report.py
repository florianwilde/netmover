"""Ergebnis eines Mount-Tests: drei getrennte Schritte plus abgeleitete Rollen.

Fallstrick 12: Der Schreibtest erkennt Root-Squash auf NFS und nennt Ursache
und Abhilfe im Klartext.
"""

from __future__ import annotations

import errno
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from netmover.models import MountType


class Role(StrEnum):
    SOURCE_COPY = "nur Quelle, kopieren"
    SOURCE_MOVE = "Quelle mit Löschen"
    TARGET = "Ziel"


SYNOLOGY_SQUASH_HINT = (
    "Synology: Systemsteuerung → Gemeinsamer Ordner → Ordner wählen → Bearbeiten → "
    "NFS-Berechtigungen → Regel bearbeiten → Squash: „Keine Zuordnung“ "
    "(No mapping) oder „Alle Benutzer zu admin zuordnen“."
)


@dataclass
class CheckStep:
    name: str  # "mount" | "read" | "write" (bei sftp: "connect")
    ok: bool | None  # None = nicht ausgeführt
    message: str = ""


@dataclass
class CheckReport:
    steps: list[CheckStep] = field(default_factory=list)
    can_read: bool = False
    can_write: bool = False
    can_delete: bool = False
    root_squash: bool | None = None
    owner: str | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    hints: list[str] = field(default_factory=list)
    host_key: str | None = None  # sftp: festgestellter Hostschlüssel
    host_key_fingerprint: str | None = None

    @property
    def ok(self) -> bool:
        return all(s.ok is not False for s in self.steps) and self.can_read

    @property
    def roles(self) -> list[Role]:
        roles: list[Role] = []
        if self.can_read:
            roles.append(Role.SOURCE_MOVE if self.can_delete else Role.SOURCE_COPY)
        if self.can_write:
            roles.append(Role.TARGET)
        return roles

    def step(self, name: str, ok: bool | None, message: str = "") -> CheckStep:
        s = CheckStep(name, ok, message)
        self.steps.append(s)
        return s

    def to_json(self) -> str:
        data = asdict(self)
        data["roles"] = [r.value for r in self.roles]
        data["ok"] = self.ok
        return json.dumps(data, ensure_ascii=False)


@dataclass
class WriteAssessment:
    write_ok: bool
    delete_ok: bool
    root_squash: bool | None
    message: str
    hints: list[str]


def assess_write(
    mount_type: MountType,
    euid: int,
    write_error: OSError | None,
    created_uid: int | None,
    delete_error: OSError | None,
) -> WriteAssessment:
    """Reine Auswertung des Schreibtests – getrennt von den Dateizugriffen, damit
    sich Root-Squash ohne root und ohne NAS testen lässt."""
    is_nfs = mount_type in (MountType.NFS3, MountType.NFS4)
    hints: list[str] = []

    if write_error is not None:
        denied = write_error.errno in (errno.EACCES, errno.EPERM)
        if is_nfs and euid == 0 and denied:
            hints.append(
                "Schreiben als root wurde abgelehnt – vermutlich Root-Squash: Der NFS-Server "
                "bildet root auf „nobody“ ab, und „nobody“ darf hier nicht schreiben. "
                + SYNOLOGY_SQUASH_HINT
            )
            return WriteAssessment(False, False, True, "Schreiben verweigert (Root-Squash?)", hints)
        if write_error.errno == errno.EROFS:
            return WriteAssessment(False, False, None, "Freigabe ist schreibgeschützt", hints)
        if denied:
            hints.append("Keine Schreibrechte – als Quelle nur zum Kopieren geeignet.")
        return WriteAssessment(
            False, False, None, f"Schreiben fehlgeschlagen: {write_error.strerror}", hints
        )

    root_squash: bool | None = None
    message = "Datei angelegt und gelöscht"
    if is_nfs and euid == 0 and created_uid is not None:
        root_squash = created_uid != 0
        if root_squash:
            message = f"Schreiben möglich, Datei gehört aber uid {created_uid} (Root-Squash)"
            hints.append(
                "Root-Squash aktiv: Dateien gehören nicht root, „chown“ nach dem Verschieben "
                "ist nicht möglich und wird übersprungen. Wer Eigentümer setzen will: "
                + SYNOLOGY_SQUASH_HINT
            )

    if delete_error is not None:
        return WriteAssessment(
            True,
            False,
            root_squash,
            f"Angelegt, aber Löschen fehlgeschlagen: {delete_error.strerror}",
            [*hints, "Testdatei konnte nicht gelöscht werden – bitte von Hand entfernen."],
        )
    return WriteAssessment(True, True, root_squash, message, hints)
