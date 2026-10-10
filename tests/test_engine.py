"""Mover-Engine mit lokalen Zielen – Fallstricke 1, 3, 4, 10, 11, 14 und Quarantäne."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import (
    CollisionPolicy,
    CopiedFile,
    ItemStatus,
    PatternType,
    Rule,
    RuleAction,
    RunStatus,
)
from netmover.mover import destinations as dest_mod
from netmover.mover.destinations import LocalDestination, TransferError
from netmover.mover.engine import Engine, RuleSnapshot, RunResult
from netmover.mover.ledger import Ledger
from netmover.mover.naming import collision_candidates
from netmover.mover.scan import Matcher
from netmover.timeouts import CancelToken
from tests.conftest import MountFactory


@pytest.fixture
def rule_id(make_mount: MountFactory, session_factory: sessionmaker[Session]) -> int:
    mid = make_mount()
    with session_scope(session_factory) as s:
        r = Rule(name="r", src_mount_id=mid, dst_path="/x")
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(session_factory)


Sleeper = Callable[[float], None]


@pytest.fixture
def hooks() -> list[Callable[[], None]]:
    """Aktionen, die während des Stabilitätsfensters ausgeführt werden."""
    return []


@pytest.fixture
def engine(ledger: Ledger, hooks: list[Callable[[], None]]) -> Engine:
    def sleep(_: float) -> None:
        for h in hooks:
            h()

    return Engine(ledger, sleep=sleep)


@pytest.fixture
def cross_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """tmp_path liegt auf einem Gerät – für Kopier-Tests den Schnellweg abschalten."""
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "src"
    p.mkdir()
    return p


@pytest.fixture
def dst(tmp_path: Path) -> Path:
    p = tmp_path / "dst"
    p.mkdir()
    return p


def snap(rule_id: int, **kw: object) -> RuleSnapshot:
    return RuleSnapshot(id=rule_id, name="r", **kw)  # type: ignore[arg-type]


def run(
    engine: Engine, rule: RuleSnapshot, src: Path, dst: Path | LocalDestination, **kw: object
) -> RunResult:
    d = dst if isinstance(dst, LocalDestination) else LocalDestination(dst)
    return engine.execute(rule, src, d, CancelToken(), **kw)  # type: ignore[arg-type]


def write(path: Path, data: str = "inhalt") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def statuses(result: RunResult) -> dict[str, ItemStatus]:
    return {i.src: i.status for i in result.items}


# -- Fallstrick 1: halb geschriebene Dateien --------------------------------------------


def test_growing_file_is_not_touched(
    engine: Engine, rule_id: int, src: Path, dst: Path, hooks: list[Callable[[], None]]
) -> None:
    write(src / "fertig.pdf", "a" * 100)
    growing = write(src / "wachsend.pdf", "b" * 10)
    hooks.append(lambda: growing.write_text("b" * 20))  # wächst während des Fensters
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"fertig.pdf": ItemStatus.MOVED, "wachsend.pdf": ItemStatus.SKIPPED}
    assert growing.read_text() == "b" * 20
    assert not (dst / "wachsend.pdf").exists()
    assert (dst / "fertig.pdf").read_text() == "a" * 100


def test_mtime_change_alone_counts_as_unstable(
    engine: Engine, rule_id: int, src: Path, dst: Path, hooks: list[Callable[[], None]]
) -> None:
    f = write(src / "scan.pdf")
    st = f.stat()
    hooks.append(lambda: os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000)))
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"scan.pdf": ItemStatus.SKIPPED}
    assert f.exists()


def test_stability_window_is_waited(ledger: Ledger, rule_id: int, src: Path, dst: Path) -> None:
    waited: list[float] = []
    eng = Engine(ledger, sleep=waited.append)
    write(src / "a.pdf")
    run(eng, snap(rule_id, stability_seconds=7), src, dst)
    assert waited == [7]
    waited.clear()
    run(eng, snap(rule_id, stability_seconds=7), src, dst)  # nichts mehr da → kein Warten
    assert waited == []


@pytest.mark.parametrize(
    "name",
    [".versteckt.pdf", "scan.pdf.part", "x.tmp", "dl.crdownload", "f.filepart", "t.!ut", "A.PART"],
)
def test_hidden_and_partial_files_are_ignored(
    engine: Engine, rule_id: int, src: Path, dst: Path, name: str
) -> None:
    write(src / name)
    result = run(engine, snap(rule_id), src, dst)
    assert result.seen == 0
    assert (src / name).exists()


def test_min_age_and_max_size(engine: Engine, rule_id: int, src: Path, dst: Path) -> None:
    write(src / "neu.pdf")
    old = write(src / "alt.pdf")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    write(src / "gross.pdf", "x" * (2 * 1024 * 1024))
    os.utime(src / "gross.pdf", (time.time() - 3600, time.time() - 3600))
    result = run(engine, snap(rule_id, min_age_seconds=600, max_size_mb=1), src, dst)
    assert statuses(result) == {
        "alt.pdf": ItemStatus.MOVED,
        "gross.pdf": ItemStatus.SKIPPED,
        "neu.pdf": ItemStatus.SKIPPED,
    }


# -- Fallstrick 3: Atomarität ---------------------------------------------------------------


@pytest.mark.usefixtures("cross_device")
def test_final_name_never_shows_partial_content(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = "x" * (3 * dest_mod.CHUNK + 17)
    write(src / "gross.pdf", data)
    seen_during_write: list[list[str]] = []
    original = LocalDestination._upload

    def spy(self: LocalDestination, *a: object, **kw: object) -> list[str]:
        notes = original(self, *a, **kw)  # type: ignore[arg-type]
        seen_during_write.append(sorted(p.name for p in dst.iterdir()))
        return notes

    monkeypatch.setattr(LocalDestination, "_upload", spy)
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"gross.pdf": ItemStatus.MOVED}
    # während/nach dem Schreiben existiert nur die .part-Datei, nicht der Endname
    (names,) = seen_during_write
    assert len(names) == 1 and names[0].startswith(".netmover-") and names[0].endswith(".part")
    assert (dst / "gross.pdf").read_text() == data
    assert not (src / "gross.pdf").exists()


STAGES = ["_upload", "_apply_meta", "_link_noreplace", "verify"]


@pytest.mark.usefixtures("cross_device")
@pytest.mark.parametrize("stage", STAGES)
def test_abort_at_any_stage_keeps_source_and_leaves_no_part(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    write(src / "scan.pdf", "wichtig")
    if stage == "verify":
        # Quelle wird zwischen Kopie und Umbenennen verändert
        original = LocalDestination._apply_meta

        def touch(self: LocalDestination, rel: str, meta: object) -> list[str]:
            (src / "scan.pdf").write_text("wichtig!!")
            return original(self, rel, meta)  # type: ignore[arg-type]

        monkeypatch.setattr(LocalDestination, "_apply_meta", touch)
    else:

        def boom(self: object, *a: object, **kw: object) -> None:
            raise OSError(errno.EIO if stage == "_upload" else errno.ENOSPC, "Fehler")

        monkeypatch.setattr(LocalDestination, stage, boom)
    result = run(engine, snap(rule_id), src, dst)
    assert (src / "scan.pdf").exists()
    assert list(dst.iterdir()) == []
    item = result.items[0] if result.items else None
    if stage == "_upload":  # EIO → Anbindung gestört, ganzer Lauf abgebrochen
        assert result.status is RunStatus.ERROR
    elif stage == "verify":
        assert item is not None and item.status is ItemStatus.SKIPPED
    else:
        assert item is not None and item.status is ItemStatus.FAILED


@pytest.mark.parametrize("path", ["same-device", "copy"])
def test_source_not_deletable_keeps_target_and_prevents_duplicate(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    if path == "copy":
        monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)
    else:
        # Dateisystem ohne renameat2 → link + unlink, unlink scheitert
        monkeypatch.setattr("netmover.mover.destinations.rename_noreplace", lambda *a: False)
    write(src / "scan.pdf")
    real_unlink = os.unlink

    def deny(path: object, *a: object, **kw: object) -> None:
        if str(path).startswith(str(src)):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        real_unlink(path)  # type: ignore[arg-type]

    monkeypatch.setattr("netmover.mover.sources.os.unlink", deny)
    first = run(engine, snap(rule_id), src, dst)
    assert first.items[0].status is ItemStatus.FAILED
    assert "nicht löschbar" in (first.items[0].message or "")
    assert (dst / "scan.pdf").exists() and (src / "scan.pdf").exists()
    second = run(engine, snap(rule_id), src, dst)
    assert second.items == [] and second.message == "1 bereits übertragen"
    assert sorted(p.name for p in dst.iterdir()) == ["scan.pdf"]


def test_stale_part_files_are_cleaned(engine: Engine, rule_id: int, src: Path, dst: Path) -> None:
    old = write(dst / ".netmover-dead.part", "rest")
    young = write(dst / ".netmover-busy.part", "rest")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    run(engine, snap(rule_id), src, dst)
    assert not old.exists()
    assert young.exists()


def test_same_device_move_uses_link_without_copy(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(src / "a.pdf", "x")
    ino = (src / "a.pdf").stat().st_ino

    def no_copy(*a: object, **kw: object) -> None:
        raise AssertionError("darf auf demselben Gerät nicht kopieren")

    monkeypatch.setattr(LocalDestination, "_upload", no_copy)
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"a.pdf": ItemStatus.MOVED}
    assert (dst / "a.pdf").stat().st_ino == ino
    assert not (src / "a.pdf").exists()


def test_cross_device_path_is_used_when_devices_differ(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)
    write(src / "a.pdf", "x")
    ino = (src / "a.pdf").stat().st_ino
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"a.pdf": ItemStatus.MOVED}
    assert (dst / "a.pdf").stat().st_ino != ino


def test_filesystem_without_hardlinks_falls_back_to_rename(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)

    def no_link(*a: object, **kw: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("netmover.mover.destinations.os.link", no_link)
    write(src / "a.pdf")
    write(dst / "a.pdf", "alt")
    result = run(engine, snap(rule_id), src, dst)
    assert result.items[0].dst == str(dst / "a-1.pdf")
    assert (dst / "a.pdf").read_text() == "alt"


# -- Fallstrick 4: Namenskollisionen ---------------------------------------------------------


def test_collision_candidates() -> None:
    gen = collision_candidates("sub/scan001.pdf")
    assert [next(gen) for _ in range(3)] == [
        "sub/scan001.pdf",
        "sub/scan001-1.pdf",
        "sub/scan001-2.pdf",
    ]
    gen = collision_candidates("README")
    assert [next(gen) for _ in range(2)] == ["README", "README-1"]


def test_rename_counts_up_against_filesystem(
    engine: Engine, rule_id: int, src: Path, dst: Path
) -> None:
    write(dst / "scan001.pdf", "alt")
    for i in range(3):
        write(src / "scan001.pdf", f"neu{i}")
        run(engine, snap(rule_id), src, dst)
    assert sorted(p.name for p in dst.iterdir()) == [
        "scan001-1.pdf",
        "scan001-2.pdf",
        "scan001-3.pdf",
        "scan001.pdf",
    ]
    assert (dst / "scan001.pdf").read_text() == "alt"
    assert (dst / "scan001-3.pdf").read_text() == "neu2"


def test_rename_rechecks_filesystem_each_time(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zwischen Plan und Umbenennen belegt ein Dritter den Namen – kein Überschreiben."""
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)
    write(dst / "scan.pdf", "alt")
    write(src / "scan.pdf", "neu")
    original = LocalDestination._apply_meta

    def intruder(self: LocalDestination, rel: str, meta: object) -> list[str]:
        write(dst / "scan-1.pdf", "fremd")
        return original(self, rel, meta)  # type: ignore[arg-type]

    monkeypatch.setattr(LocalDestination, "_apply_meta", intruder)
    result = run(engine, snap(rule_id), src, dst)
    assert result.items[0].dst == str(dst / "scan-2.pdf")
    assert (dst / "scan-1.pdf").read_text() == "fremd"
    assert (dst / "scan-2.pdf").read_text() == "neu"


def test_skip_policy(engine: Engine, rule_id: int, src: Path, dst: Path) -> None:
    write(dst / "a.pdf", "alt")
    write(src / "a.pdf", "neu")
    result = run(engine, snap(rule_id, on_collision=CollisionPolicy.SKIP), src, dst)
    assert statuses(result) == {"a.pdf": ItemStatus.SKIPPED}
    assert (dst / "a.pdf").read_text() == "alt"
    assert (src / "a.pdf").exists()


def test_overwrite_policy(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)
    write(dst / "a.pdf", "alt")
    write(src / "a.pdf", "neu")
    result = run(engine, snap(rule_id, on_collision=CollisionPolicy.OVERWRITE), src, dst)
    assert statuses(result) == {"a.pdf": ItemStatus.MOVED}
    assert (dst / "a.pdf").read_text() == "neu"


# -- Fallstrick 11: Kopiermodus mit Gedächtnis ---------------------------------------------


def test_copy_mode_copies_each_version_once(
    engine: Engine, rule_id: int, src: Path, dst: Path, session_factory: sessionmaker[Session]
) -> None:
    rule = snap(rule_id, action=RuleAction.COPY)
    f = write(src / "scan.pdf", "v1")
    for _ in range(3):
        run(engine, rule, src, dst)
    assert sorted(p.name for p in dst.iterdir()) == ["scan.pdf"]
    assert f.exists()

    # neue Version → läuft durch die Kollisionsregel
    f.write_text("version 2")
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 5_000_000_000))
    result = run(engine, rule, src, dst)
    assert statuses(result) == {"scan.pdf": ItemStatus.COPIED}
    run(engine, rule, src, dst)
    assert sorted(p.name for p in dst.iterdir()) == ["scan-1.pdf", "scan.pdf"]
    assert (dst / "scan-1.pdf").read_text() == "version 2"

    # Quelle verschwindet → Buchführung wird bereinigt
    f.unlink()
    run(engine, rule, src, dst)
    with session_factory() as s:
        assert s.scalars(select(CopiedFile)).all() == []


def test_copy_mode_preserves_mtime(engine: Engine, rule_id: int, src: Path, dst: Path) -> None:
    f = write(src / "a.pdf")
    os.utime(f, ns=(1_600_000_000_000_000_000, 1_600_000_000_123_456_789))
    run(engine, snap(rule_id, action=RuleAction.COPY), src, dst)
    assert (dst / "a.pdf").stat().st_mtime_ns == 1_600_000_000_123_456_789


# -- Fallstrick 14: Platz ---------------------------------------------------------------------


def test_insufficient_space_skips_file_without_partial_copy(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(src / "a.pdf", "x" * 1000)
    monkeypatch.setattr(LocalDestination, "free_bytes", lambda self: 1040)  # < 1000 * 1.05
    result = run(engine, snap(rule_id), src, dst)
    assert result.items[0].status is ItemStatus.FAILED
    assert "zu wenig Platz" in (result.items[0].message or "")
    assert list(dst.iterdir()) == []
    assert (src / "a.pdf").exists()


def test_metadata_failure_is_only_a_hint(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)

    def deny(*a: object, **kw: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("netmover.mover.destinations.os.utime", deny)
    write(src / "a.pdf")
    result = run(engine, snap(rule_id), src, dst)
    assert result.items[0].status is ItemStatus.MOVED
    assert "Änderungszeit nicht übernommen" in (result.items[0].message or "")


# -- Fallstrick 10: Berechtigungen ---------------------------------------------------------


def test_set_mode_applied(engine: Engine, rule_id: int, src: Path, dst: Path) -> None:
    write(src / "a.pdf").chmod(0o600)
    run(engine, snap(rule_id, set_mode=0o640), src, dst)
    assert (dst / "a.pdf").stat().st_mode & 0o777 == 0o640


def test_unchanged_mode_is_copied_from_source(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)
    write(src / "a.pdf").chmod(0o604)
    run(engine, snap(rule_id), src, dst)
    assert (dst / "a.pdf").stat().st_mode & 0o777 == 0o604


def test_chown_without_permission_is_clear_message(
    engine: Engine, rule_id: int, src: Path, dst: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("als root ist chown erlaubt")
    write(src / "a.pdf")
    result = run(engine, snap(rule_id, set_owner=(0, 0)), src, dst)
    assert result.items[0].status is ItemStatus.MOVED
    assert "keine Berechtigung" in (result.items[0].message or "")


@pytest.mark.parametrize(("kind", "squash"), [("cifs", None), ("nfs", True), ("nfs", None)])
def test_chown_skipped_on_cifs_and_squashed_nfs_warns_once(
    engine: Engine,
    rule_id: int,
    src: Path,
    dst: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    squash: bool | None,
) -> None:
    called: list[object] = []
    monkeypatch.setattr("netmover.mover.destinations.os.chown", lambda *a: called.append(a))
    write(src / "a.pdf")
    write(src / "b.pdf")
    d = LocalDestination(dst, kind)  # type: ignore[arg-type]
    result = run(engine, snap(rule_id, set_owner=(1000, 1000), dst_root_squash=squash), src, d)
    assert called == []
    msgs = [i.message for i in result.items if i.message and "chown übersprungen" in i.message]
    assert len(msgs) == 1  # einmalig warnen


def test_chown_on_nfs_without_squash_is_tried(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[object] = []
    monkeypatch.setattr("netmover.mover.destinations.os.chown", lambda *a: called.append(a))
    write(src / "a.pdf")
    run(
        engine,
        snap(rule_id, set_owner=(1000, 100), dst_root_squash=False),
        src,
        LocalDestination(dst, "nfs"),
    )
    assert called and called[0][1:] == (1000, 100)  # type: ignore[index]


# -- Filter, Rekursion, Probelauf, Quarantäne ---------------------------------------------


def test_matcher() -> None:
    m = Matcher("*.pdf; *.JPG", PatternType.GLOB)
    assert m("a/Scan.PDF") and m("b.jpg") and not m("c.txt")
    r = Matcher(r"^rechnungen/.*\.pdf$", PatternType.REGEX)
    assert r("rechnungen/2026.pdf") and not r("scan/2026.pdf")


def test_recursive_preserves_structure_and_skips_synology_dirs(
    engine: Engine, rule_id: int, src: Path, dst: Path
) -> None:
    write(src / "max" / "a.pdf")
    write(src / "@eaDir" / "a.pdf" / "SYNOFILE_THUMB_M.jpg")
    write(src / "#recycle" / "alt.pdf")
    write(src / "top.pdf")
    flat = run(engine, snap(rule_id), src, dst)
    assert statuses(flat) == {"top.pdf": ItemStatus.MOVED}
    rec = run(engine, snap(rule_id, recursive=True), src, dst)
    assert statuses(rec) == {"max/a.pdf": ItemStatus.MOVED}
    assert (dst / "max" / "a.pdf").exists()
    assert (src / "#recycle" / "alt.pdf").exists()


def test_symlinks_are_ignored(
    engine: Engine, rule_id: int, src: Path, dst: Path, tmp_path: Path
) -> None:
    secret = write(tmp_path / "geheim.txt")
    (src / "link.txt").symlink_to(secret)
    result = run(engine, snap(rule_id), src, dst)
    assert result.seen == 0 and secret.exists()


def test_dry_run_touches_nothing(
    engine: Engine, rule_id: int, src: Path, dst: Path, session_factory: sessionmaker[Session]
) -> None:
    write(src / "a.pdf")
    write(dst / "a.pdf", "alt")
    write(src / "b.pdf")
    result = run(engine, snap(rule_id, action=RuleAction.COPY), src, dst, dry_run=True)
    assert {i.src: (i.status, i.dst) for i in result.items} == {
        "a.pdf": (ItemStatus.PLANNED, str(dst / "a-1.pdf")),
        "b.pdf": (ItemStatus.PLANNED, str(dst / "b.pdf")),
    }
    assert sorted(p.name for p in dst.iterdir()) == ["a.pdf"]
    assert sorted(p.name for p in src.iterdir()) == ["a.pdf", "b.pdf"]
    with session_factory() as s:
        assert s.scalars(select(CopiedFile)).all() == []


def test_quarantine_after_repeated_failures(
    engine: Engine,
    rule_id: int,
    src: Path,
    dst: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)

    def broken(self: object, *a: object, **kw: object) -> None:
        raise TransferError("kaputt")

    monkeypatch.setattr(LocalDestination, "_link_noreplace", broken)
    write(src / "defekt.pdf")
    quarantine = src / "quarantaene"
    rule = snap(rule_id, quarantine_after=3, recursive=True)
    msgs = []
    for _ in range(3):
        r = run(engine, rule, src, dst, quarantine_root=quarantine)
        msgs.append(r.items[0].message)
    assert msgs[-1] == "kaputt (Versuch 3)"
    r = run(engine, rule, src, dst, quarantine_root=quarantine)
    assert statuses(r) == {"defekt.pdf": ItemStatus.QUARANTINED}
    assert (quarantine / "defekt.pdf").exists()
    # Quarantäne liegt in der Quelle, wird aber trotz Rekursion nicht erneut gescannt
    r = run(engine, rule, src, dst, quarantine_root=quarantine)
    assert r.seen == 0


def test_mount_error_mid_run_aborts_instead_of_failing_every_file(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)

    def stale(self: object, *a: object, **kw: object) -> None:
        raise OSError(errno.ESTALE, "Stale file handle")

    monkeypatch.setattr(LocalDestination, "_upload", stale)
    write(src / "a.pdf")
    write(src / "b.pdf")
    result = run(engine, snap(rule_id), src, dst)
    assert result.status is RunStatus.ERROR
    assert "gestört" in (result.message or "")
    assert (src / "a.pdf").exists() and (src / "b.pdf").exists()


def test_cancel_stops_before_next_file(engine: Engine, rule_id: int, src: Path, dst: Path) -> None:
    write(src / "a.pdf")
    token = CancelToken()
    token.cancel()
    result = engine.execute(snap(rule_id), src, LocalDestination(dst), token)
    assert result.status is RunStatus.ERROR
    assert (src / "a.pdf").exists()


def test_missing_source_dir(engine: Engine, rule_id: int, tmp_path: Path, dst: Path) -> None:
    result = run(engine, snap(rule_id), tmp_path / "fehlt", dst)
    assert result.status is RunStatus.ERROR and "existiert nicht" in (result.message or "")


def test_local_destination_must_exist(
    engine: Engine, rule_id: int, src: Path, tmp_path: Path
) -> None:
    write(src / "a.pdf")
    result = run(engine, snap(rule_id), src, LocalDestination(tmp_path / "fehlt"))
    assert result.status is RunStatus.ERROR
    assert (src / "a.pdf").exists()


@pytest.mark.usefixtures("cross_device")
def test_fsync_before_final_rename(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmover.mover import fsops

    events: list[str] = []
    real_fsync, real_rename = os.fsync, fsops.rename_noreplace

    def fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def rename(a: object, b: object) -> bool:
        events.append(f"rename:{Path(str(b)).name}")
        return real_rename(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr("netmover.mover.destinations.os.fsync", fsync)
    monkeypatch.setattr("netmover.mover.destinations.rename_noreplace", rename)
    write(src / "a.pdf")
    run(engine, snap(rule_id), src, dst)
    assert events.index("fsync") < events.index("rename:a.pdf")


@pytest.mark.usefixtures("cross_device")
def test_name_taken_between_check_and_link_is_never_overwritten(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enges Wettlauf-Fenster: exists() sagt „frei“, link() meldet EEXIST."""
    write(dst / "scan.pdf", "fremd")
    write(src / "scan.pdf", "neu")
    real_exists = LocalDestination.exists
    lied: list[str] = []

    def stale_exists(self: LocalDestination, rel: str) -> bool:
        if rel == "scan.pdf" and not lied and self.root == dst:
            lied.append(rel)
            return False  # veraltete Antwort
        return real_exists(self, rel)

    monkeypatch.setattr(LocalDestination, "plan", lambda self, rel, policy: rel)
    monkeypatch.setattr(LocalDestination, "exists", stale_exists)
    result = run(engine, snap(rule_id), src, dst)
    assert lied
    assert result.items[0].dst == str(dst / "scan-1.pdf")
    assert (dst / "scan.pdf").read_text() == "fremd"
    assert (dst / "scan-1.pdf").read_text() == "neu"


# -- renameat2(RENAME_NOREPLACE) ----------------------------------------------------------


def test_rename_noreplace(tmp_path: Path) -> None:
    from netmover.mover.fsops import rename_noreplace

    a, b = write(tmp_path / "a", "neu"), write(tmp_path / "b", "alt")
    ino = a.stat().st_ino
    if not rename_noreplace(a, tmp_path / "c"):
        pytest.skip("Dateisystem des Testrechners kann kein renameat2")
    assert (tmp_path / "c").stat().st_ino == ino and not a.exists()  # echtes rename
    with pytest.raises(FileExistsError):
        rename_noreplace(tmp_path / "c", b)
    assert b.read_text() == "alt" and (tmp_path / "c").exists()


@pytest.mark.usefixtures("cross_device")
@pytest.mark.parametrize("supported", [True, False])
def test_final_step_is_a_real_rename_when_possible(
    engine: Engine,
    rule_id: int,
    src: Path,
    dst: Path,
    monkeypatch: pytest.MonkeyPatch,
    supported: bool,
) -> None:
    """Watcher wie Paperless sehen ein rename (IN_MOVED_TO); ohne Unterstützung
    bleibt es beim link-Rückfallweg – überschrieben wird in keinem Fall."""
    links: list[object] = []
    real_link = os.link

    def spy_link(a: object, b: object, *args: object, **kw: object) -> None:
        links.append(b)
        real_link(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr("netmover.mover.destinations.os.link", spy_link)
    if not supported:
        monkeypatch.setattr("netmover.mover.destinations.rename_noreplace", lambda *a: False)
    write(dst / "a.pdf", "alt")
    write(src / "a.pdf", "neu")
    result = run(engine, snap(rule_id), src, dst)
    assert result.items[0].dst == str(dst / "a-1.pdf")
    assert (dst / "a.pdf").read_text() == "alt" and (dst / "a-1.pdf").read_text() == "neu"
    assert bool(links) is not supported
    assert [p.name for p in dst.iterdir() if p.name.endswith(".part")] == []


# -- Praxisbefund: veralteter Attribut-Cache (CIFS/NFS) -------------------------------------


def test_stability_uses_fresh_attributes_not_stat_cache(
    engine: Engine,
    rule_id: int,
    src: Path,
    dst: Path,
    hooks: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auf dem Testhost lieferte CIFS per stat() sekundenlang eine veraltete Größe,
    während die Datei wuchs. Simuliert: os.stat liefert für die Datei immer den
    alten Stand (Cache), nur öffnen + fstat sieht die Wahrheit."""
    growing = write(src / "langsam.pdf", "a" * 10)
    stale = os.stat(growing)
    real_stat = os.stat

    def cached_stat(path: object, *a: object, **kw: object) -> os.stat_result:
        if str(path) == str(growing):
            return stale
        return real_stat(path, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", cached_stat)
    hooks.append(lambda: growing.write_text("a" * 20))
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"langsam.pdf": ItemStatus.SKIPPED}
    assert "wird noch geschrieben" in (result.items[0].message or "")
    assert growing.exists() and not (dst / "langsam.pdf").exists()


def test_locked_file_counts_as_in_use(
    engine: Engine, rule_id: int, src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = write(src / "gesperrt.pdf")
    real_open = os.open

    def locked(path: object, flags: int, *a: object, **kw: object) -> int:
        if str(path) == str(f):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("netmover.mover.scan.os.open", locked)
    result = run(engine, snap(rule_id), src, dst)
    assert statuses(result) == {"gesperrt.pdf": ItemStatus.SKIPPED}
    assert result.items[0].message == "Datei ist gerade in Benutzung"
    assert f.exists()
