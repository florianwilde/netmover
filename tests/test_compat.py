"""Kompatibilitätszusage (README, „Kompatibilität“): Exporte älterer Versionen bleiben
importierbar.

``tests/data/export-v*.json`` sind echte Exporte, erzeugt mit dem Code der jeweiligen
Version. Sie werden nie angepasst – schlägt dieser Test fehl, bricht eine Änderung die
Zusage. Zu jedem Release mit geändertem Export kommt eine neue Datei dazu.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import Mount, MountType, Rule, TargetKind, TargetLayout
from netmover.mounting.manager import MountManager
from netmover.transfer import FORMAT_VERSION, import_config

DATA = Path(__file__).parent / "data"
EXPORTS = sorted(DATA.glob("export-v*.json"))


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_there_are_frozen_exports() -> None:
    assert [p.name for p in EXPORTS] == ["export-v0.10.0.json", "export-v0.11.0.json"]
    assert {_load(p)["format"] for p in EXPORTS} == {FORMAT_VERSION}


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.stem)
def test_old_export_imports_completely(
    path: Path, session_factory: sessionmaker[Session], manager: MountManager
) -> None:
    with session_scope(session_factory) as s:
        report = import_config(s, manager, _load(path))
    assert report.errors == []
    assert report.created == [
        "Anbindung „NAS Archiv“",
        "Anbindung „Paperless“",
        "Anbindung „Synology Scans“",
        "Regel „Scans nach Paperless“ (deaktiviert)",
    ]
    assert report.settings["notify_after"] == 2

    with session_factory() as s:
        mounts = {m.name: m for m in s.scalars(select(Mount))}
        assert {n: m.type for n, m in mounts.items()} == {
            "NAS Archiv": MountType.NFS4,
            "Paperless": MountType.SFTP,
            "Synology Scans": MountType.CIFS,
        }
        rule = s.scalars(select(Rule)).one()
        assert rule.enabled is False  # nach einem Import erst prüfen, dann einschalten
        assert rule.src_mount_id == mounts["Synology Scans"].id
        assert rule.dst_mount_id == mounts["Paperless"].id
        assert (rule.src_subpath, rule.pattern, rule.quarantine_path) == (
            "eingang",
            "*.pdf",
            "_quarantaene",
        )
        # 0.10.0 kannte nur ein Archiv – es wird zum weiteren Ziel „Archiv“
        [target] = rule.targets
        assert (target.name, target.kind, target.mount_id, target.path, target.layout) == (
            "Archiv",
            TargetKind.MOUNT,
            mounts["NAS Archiv"].id,
            "scans",
            TargetLayout.DATE,
        )
