"""0.9.0: Regeln kopieren – vorausgefüllt, zunächst deaktiviert."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from netmover.db import session_scope
from netmover.models import (
    ArchiveKind,
    ArchiveLayout,
    CopiedFile,
    Mount,
    MountType,
    Rule,
    RuleAction,
    Run,
)
from tests.webutil import post


def _setup(state: Any, tmp_path: Path) -> tuple[int, int]:
    consume = tmp_path / "consume"
    consume.mkdir()
    with session_scope(state.session_factory) as s:
        m = Mount(
            name="NAS",
            slug="nas",
            type=MountType.CIFS,
            server="nas",
            share="scans",
            mountpoint=str(tmp_path / "mnt" / "nas"),
        )
        a = Mount(
            name="Archiv",
            slug="archiv",
            type=MountType.CIFS,
            server="nas",
            share="archiv",
            mountpoint=str(tmp_path / "mnt" / "archiv"),
        )
        s.add_all([m, a])
        s.flush()
        r = Rule(
            name="Scans → Paperless",
            src_mount_id=m.id,
            src_subpath="max",
            dst_path=str(consume),
            pattern="*.pdf;*.jpg",
            recursive=True,
            interval_seconds=45,
            stability_seconds=7,
            action=RuleAction.COPY,
            set_mode="0640",
            quarantine_path="_q",
            archive_kind=ArchiveKind.MOUNT,
            archive_mount_id=a.id,
            archive_path="scans",
            archive_layout=ArchiveLayout.DATE,
        )
        s.add(r)
        s.flush()
        s.add(Run(rule_id=r.id))
        s.add(CopiedFile(rule_id=r.id, src_relpath="a.pdf", size=1, mtime_ns=1, dst_path="x"))
        return r.id, m.id


def _rule(state: Any, name: str) -> Rule:
    with state.session_factory() as s:
        r = s.scalars(select(Rule).where(Rule.name == name)).one()
        s.expunge(r)
        return r


def _form_from_page(html: str) -> dict[str, str]:
    """Wie ein Browser: alle Felder des Formulars mit ihren vorausgefüllten Werten."""
    import re

    form: dict[str, str] = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', html):
        tag, name = m.group(0), m.group(1)
        if 'type="checkbox"' in tag:
            if " checked" in tag:
                form[name] = "on"
        else:
            v = re.search(r'value="([^"]*)"', tag)
            form[name] = v.group(1) if v else ""
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        sel = re.search(r'<option value="([^"]*)" selected>', m.group(2))
        form[m.group(1)] = sel.group(1) if sel else ""
    return form


def test_copy_form_is_prefilled_and_disabled(
    client: TestClient, state: Any, tmp_path: Path
) -> None:
    rid, _ = _setup(state, tmp_path)
    assert f'href="/rules/{rid}/copy"' in client.get("/rules").text
    html = client.get(f"/rules/{rid}/copy").text
    assert "Kopie von „Scans → Paperless“" in html and "deaktiviert" in html
    form = _form_from_page(html)
    assert form["name"] == "Scans → Paperless (Kopie)"
    assert "enabled" not in form  # Häkchen „Aktiv“ nicht gesetzt
    assert form["pattern"] == "*.pdf;*.jpg" and form["src_subpath"] == "max"
    assert form["archive_kind"] == "mount" and form["archive_layout"] == "date"


def test_copy_saves_new_rule_with_all_settings(
    client: TestClient, state: Any, tmp_path: Path
) -> None:
    rid, _ = _setup(state, tmp_path)
    form = _form_from_page(client.get(f"/rules/{rid}/copy").text)
    form["src_subpath"] = "erika"  # typische Anpassung
    r = post(client, f"/rules/{rid}/copy", form)
    assert r.status_code == 303, r.text
    assert "Die Kopie ist deaktiviert" in client.get("/rules").text

    orig, copy = _rule(state, "Scans → Paperless"), _rule(state, "Scans → Paperless (Kopie)")
    assert copy.id != orig.id and copy.enabled is False and orig.enabled is True
    assert copy.src_subpath == "erika" and orig.src_subpath == "max"
    for f in (
        "src_mount_id",
        "dst_path",
        "pattern",
        "recursive",
        "interval_seconds",
        "stability_seconds",
        "action",
        "set_mode",
        "quarantine_path",
        "archive_kind",
        "archive_mount_id",
        "archive_path",
        "archive_layout",
    ):
        assert getattr(copy, f) == getattr(orig, f), f
    # Protokoll und Buchführung beginnen neu
    with state.session_factory() as s:
        assert s.scalars(select(Run).where(Run.rule_id == copy.id)).all() == []
        assert s.scalars(select(CopiedFile).where(CopiedFile.rule_id == copy.id)).all() == []
    assert state.scheduler.next_run(copy.id) is None  # deaktiviert → kein Termin


def test_copy_names_count_up(client: TestClient, state: Any, tmp_path: Path) -> None:
    rid, _ = _setup(state, tmp_path)
    for expected in ("Scans → Paperless (Kopie)", "Scans → Paperless (Kopie 2)"):
        form = _form_from_page(client.get(f"/rules/{rid}/copy").text)
        assert form["name"] == expected
        post(client, f"/rules/{rid}/copy", form)


def test_copy_validation_error_keeps_context(
    client: TestClient, state: Any, tmp_path: Path
) -> None:
    rid, _ = _setup(state, tmp_path)
    form = _form_from_page(client.get(f"/rules/{rid}/copy").text)
    form["name"] = "Scans → Paperless"  # schon vergeben
    r = post(client, f"/rules/{rid}/copy", form)
    assert r.status_code == 400 and "bereits vergeben" in r.text and "Kopie von" in r.text
    with state.session_factory() as s:
        assert len(s.scalars(select(Rule)).all()) == 1


def test_copy_of_missing_rule_redirects(client: TestClient) -> None:
    assert client.get("/rules/4711/copy").headers["location"] == "/rules"
