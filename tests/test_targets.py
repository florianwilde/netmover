"""Weitere Ziele (0.11.0, vorher Archiv aus 0.7.0): Kopie an alle Ziele, Quelle erst
danach löschen; nicht erreichbare Ziele werden nachbeliefert."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import (
    CollisionPolicy,
    ItemStatus,
    Rule,
    RuleAction,
    RuleTarget,
    RunStatus,
    TargetFile,
    TargetKind,
)
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager
from netmover.mover.destinations import LocalDestination, TransferError
from netmover.mover.engine import Engine, RuleSnapshot, RunResult, Target
from netmover.mover.ledger import Ledger
from netmover.mover.runner import RuleRunner
from netmover.timeouts import CancelToken
from tests.conftest import MountFactory


@pytest.fixture
def rule_id(make_mount: MountFactory, session_factory: sessionmaker[Session]) -> int:
    mid = make_mount()
    with session_scope(session_factory) as s:
        r = Rule(
            name="r",
            src_mount_id=mid,
            dst_path="/x",
            targets=[
                RuleTarget(position=i, name=n, kind=TargetKind.LOCAL, path=f"/{n}")
                for i, n in enumerate(("Archiv", "Buchhaltung", "Extra"))
            ],
        )
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def tids(rule_id: int, session_factory: sessionmaker[Session]) -> dict[str, int]:
    with session_factory() as s:
        return {t.name: t.id for t in s.scalars(select(RuleTarget))}


FIXED = datetime(2026, 10, 12, 9, 30).timestamp()


@pytest.fixture
def engine(session_factory: sessionmaker[Session]) -> Engine:
    return Engine(Ledger(session_factory), sleep=lambda _: None, clock=lambda: FIXED)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    src, dst, arch = tmp_path / "src", tmp_path / "dst", tmp_path / "archiv"
    for d in (src, dst, arch):
        d.mkdir()
    return src, dst, arch


@pytest.fixture(autouse=True)
def cross_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """tmp_path liegt auf einem Gerät – realistischer Weg über Kopie erzwingen."""
    monkeypatch.setattr(LocalDestination, "move_same_device", lambda *a, **k: None)


def snap(rule_id: int, **kw: object) -> RuleSnapshot:
    return RuleSnapshot(id=rule_id, name="r", stability_seconds=0, **kw)  # type: ignore[arg-type]


LAYOUT: dict[str, str] = {}


def run(
    engine: Engine,
    rule: RuleSnapshot,
    src: Path,
    dst: Path | None,
    arch: Path | None,
    *,
    layout: str = "flat",
    extra: list[Target] | None = None,
    **kw: object,
) -> RunResult:
    """Hauptziel ``dst`` (None = nicht erreichbar) und Archiv ``arch`` (Ziel „Archiv“)."""
    targets = list(extra or [])
    if arch is not None:
        targets.insert(0, Target(_TIDS["Archiv"], "Archiv", LocalDestination(arch), layout))
    dest = LocalDestination(dst) if dst is not None else None
    return engine.execute(rule, src, dest, CancelToken(), targets=targets, **kw)  # type: ignore[arg-type]


_TIDS: dict[str, int] = {}


@pytest.fixture(autouse=True)
def _register_tids(request: pytest.FixtureRequest) -> None:
    if "rule_id" in request.fixturenames:
        _TIDS.clear()
        _TIDS.update(request.getfixturevalue("tids"))


def write(p: Path, data: bytes = b"inhalt") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def names(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())


def test_move_delivers_to_target_and_archive_then_deletes_source(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]
) -> None:
    src, dst, arch = dirs
    data = os.urandom(200_000)
    write(src / "scan001.pdf", data)
    result = run(engine, snap(rule_id), src, dst, arch)
    assert result.status is RunStatus.OK
    item = result.items[0]
    assert item.status is ItemStatus.MOVED and item.message and "Archiv: " in item.message
    assert (dst / "scan001.pdf").read_bytes() == data == (arch / "scan001.pdf").read_bytes()
    assert not (src / "scan001.pdf").exists()
    assert names(dst) == ["scan001.pdf"] and names(arch) == ["scan001.pdf"]  # keine .part-Reste


def test_date_layout(engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]) -> None:
    src, dst, arch = dirs
    write(src / "max" / "a.pdf")
    run(engine, snap(rule_id, recursive=True), src, dst, arch, layout="date")
    assert names(arch) == ["2026/10/max/a.pdf"] and names(dst) == ["max/a.pdf"]


def test_archive_failure_delivers_nothing_and_keeps_source(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst, arch = dirs
    write(src / "a.pdf")
    real = LocalDestination._link_noreplace

    def fail_in_archive(self: LocalDestination, tmp: str, final: str) -> None:
        if self.root == arch:
            raise TransferError("Archiv voll")
        real(self, tmp, final)

    monkeypatch.setattr(LocalDestination, "_link_noreplace", fail_in_archive)
    result = run(engine, snap(rule_id), src, dst, arch)
    item = result.items[0]
    assert item.status is ItemStatus.FAILED and item.message == "Archiv: Archiv voll (Versuch 1)"
    assert (src / "a.pdf").exists() and names(dst) == [] and names(arch) == []


def test_target_failure_after_archive_never_archives_twice(
    engine: Engine,
    rule_id: int,
    dirs: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    src, dst, arch = dirs
    write(src / "a.pdf")
    real = LocalDestination._link_noreplace

    def fail_in_target(self: LocalDestination, tmp: str, final: str) -> None:
        if self.root == dst:
            raise TransferError("Ziel gesperrt")
        real(self, tmp, final)

    monkeypatch.setattr(LocalDestination, "_link_noreplace", fail_in_target)
    first = run(engine, snap(rule_id), src, dst, arch)
    assert first.items[0].status is ItemStatus.FAILED
    assert (src / "a.pdf").exists() and names(arch) == ["a.pdf"] and names(dst) == []
    with session_factory() as s:
        assert len(s.scalars(select(TargetFile)).all()) == 1

    monkeypatch.setattr(LocalDestination, "_link_noreplace", real)  # Ziel wieder frei
    second = run(engine, snap(rule_id), src, dst, arch)
    assert second.items[0].status is ItemStatus.MOVED
    assert names(arch) == ["a.pdf"]  # keine zweite Archivkopie (a-1.pdf)
    assert names(dst) == ["a.pdf"] and not (src / "a.pdf").exists()


def test_archive_never_overwrites(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]
) -> None:
    src, dst, arch = dirs
    write(arch / "scan.pdf", b"alt im Archiv")
    write(src / "scan.pdf", b"neu")
    run(engine, snap(rule_id, on_collision=CollisionPolicy.OVERWRITE), src, dst, arch)
    assert (arch / "scan.pdf").read_bytes() == b"alt im Archiv"
    assert (arch / "scan-1.pdf").read_bytes() == b"neu"


def test_no_space_in_archive_writes_nothing(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst, arch = dirs
    write(src / "a.pdf", b"x" * 1000)
    real = LocalDestination.free_bytes
    monkeypatch.setattr(
        LocalDestination, "free_bytes", lambda self: 10 if self.root == arch else real(self)
    )
    result = run(engine, snap(rule_id), src, dst, arch)
    assert "Archiv: zu wenig Platz" in (result.items[0].message or "")
    assert names(dst) == [] and names(arch) == [] and (src / "a.pdf").exists()


def test_source_changed_during_archive_copy(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst, arch = dirs
    f = write(src / "a.pdf", b"original")
    real = LocalDestination._apply_meta

    def change(self: LocalDestination, rel: str, meta: object) -> list[str]:
        if self.root == arch:
            f.write_bytes(b"geaendert!!")
        return real(self, rel, meta)  # type: ignore[arg-type]

    monkeypatch.setattr(LocalDestination, "_apply_meta", change)
    result = run(engine, snap(rule_id), src, dst, arch)
    assert result.items[0].status is ItemStatus.SKIPPED
    assert names(dst) == [] and names(arch) == [] and f.exists()


def test_copy_mode_with_archive(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]
) -> None:
    src, dst, arch = dirs
    f = write(src / "a.pdf", b"v1")
    rule = snap(rule_id, action=RuleAction.COPY)
    for _ in range(3):
        run(engine, rule, src, dst, arch)
    assert f.exists() and names(dst) == ["a.pdf"] and names(arch) == ["a.pdf"]
    f.write_bytes(b"version 2")
    run(engine, rule, src, dst, arch)
    assert names(dst) == ["a-1.pdf", "a.pdf"] and names(arch) == ["a-1.pdf", "a.pdf"]


def test_dry_run_shows_archive_and_writes_nothing(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]
) -> None:
    src, dst, arch = dirs
    write(src / "a.pdf")
    result = run(engine, snap(rule_id), src, dst, arch, layout="date", dry_run=True)
    assert result.items[0].status is ItemStatus.PLANNED
    assert f"Archiv: {arch / '2026/10/a.pdf'}" in (result.items[0].message or "")
    assert names(dst) == [] and names(arch) == [] and (src / "a.pdf").exists()


def test_without_archive_unchanged(
    engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]
) -> None:
    src, dst, arch = dirs
    write(src / "a.pdf")
    result = run(engine, snap(rule_id), src, dst, None)
    assert result.items[0].message is None and names(arch) == []


# -- mehrere Ziele ------------------------------------------------------------------------------


def _three(tmp_path: Path, rule_id: int, tids: dict[str, int], down: str = "") -> list[Target]:
    """Archiv, Buchhaltung, Extra als lokale Ordner; ``down`` = nicht erreichbar."""
    out = []
    for name in ("Archiv", "Buchhaltung", "Extra"):
        d = tmp_path / name.lower()
        d.mkdir(exist_ok=True)
        dest = None if name == down else LocalDestination(d)
        out.append(Target(tids[name], name, dest, reason=f"„{name}“ nicht erreichbar"))
    return out


def test_four_targets_all_get_the_file_then_source_is_deleted(
    engine: Engine, rule_id: int, tids: dict[str, int], tmp_path: Path
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    data = os.urandom(50_000)
    write(src / "rechnung.pdf", data)
    targets = _three(tmp_path, rule_id, tids)
    result = run(engine, snap(rule_id), src, dst, None, extra=targets)
    assert result.status is RunStatus.OK and result.waiting_on == []
    item = result.items[0]
    assert item.status is ItemStatus.MOVED
    for name in ("Archiv", "Buchhaltung", "Extra"):
        assert f"{name}: " in (item.message or "")
        assert (tmp_path / name.lower() / "rechnung.pdf").read_bytes() == data
    assert (dst / "rechnung.pdf").read_bytes() == data
    assert not (src / "rechnung.pdf").exists()


def test_unreachable_target_others_are_served_and_caught_up_later(
    engine: Engine,
    rule_id: int,
    tids: dict[str, int],
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    write(src / "r1.pdf", b"%PDF-1")
    down = _three(tmp_path, rule_id, tids, down="Buchhaltung")

    first = run(engine, snap(rule_id), src, dst, None, extra=down)
    assert first.waiting_on == ["„Buchhaltung“ nicht erreichbar"]
    item = first.items[0]
    assert item.status is ItemStatus.SKIPPED and "wartet auf: Buchhaltung" in (item.message or "")
    # Hauptziel und erreichbare Ziele haben die Datei, die Quelle bleibt
    assert (dst / "r1.pdf").exists() and (tmp_path / "archiv" / "r1.pdf").exists()
    assert (tmp_path / "extra" / "r1.pdf").exists() and (src / "r1.pdf").exists()
    assert not (tmp_path / "buchhaltung" / "r1.pdf").exists()

    # Weitere Läufe während des Ausfalls: nichts doppelt, kein Protokolleintrag
    again = run(engine, snap(rule_id), src, dst, None, extra=down)
    assert again.items == [] and names(dst) == ["r1.pdf"]
    assert names(tmp_path / "archiv") == ["r1.pdf"]

    # Buchhaltung wieder da → nachbeliefert, erst jetzt Quelle gelöscht
    back = run(engine, snap(rule_id), src, dst, None, extra=_three(tmp_path, rule_id, tids))
    item = back.items[0]
    assert item.status is ItemStatus.MOVED, item.message
    assert "Hauptziel hatte die Datei bereits" in (item.message or "")
    assert names(tmp_path / "buchhaltung") == ["r1.pdf"]
    assert names(dst) == ["r1.pdf"] and names(tmp_path / "archiv") == ["r1.pdf"]
    assert not (src / "r1.pdf").exists()


def test_unreachable_main_target_other_targets_still_served(
    engine: Engine, rule_id: int, tids: dict[str, int], tmp_path: Path
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    write(src / "r.pdf")
    targets = _three(tmp_path, rule_id, tids)
    first = run(engine, snap(rule_id), src, None, None, extra=targets, dest_reason="DMS weg")
    assert first.waiting_on == ["DMS weg"]
    assert "wartet auf: Hauptziel" in (first.items[0].message or "")
    assert names(tmp_path / "archiv") == ["r.pdf"] and (src / "r.pdf").exists()
    second = run(engine, snap(rule_id), src, dst, None, extra=targets)
    assert second.items[0].status is ItemStatus.MOVED
    assert names(dst) == ["r.pdf"] and names(tmp_path / "extra") == ["r.pdf"]
    assert not (src / "r.pdf").exists()


def test_copy_mode_new_target_gets_backfilled(
    engine: Engine, rule_id: int, tids: dict[str, int], tmp_path: Path
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    write(src / "a.pdf")
    rule = snap(rule_id, action=RuleAction.COPY)
    targets = _three(tmp_path, rule_id, tids)
    run(engine, rule, src, dst, None, extra=targets[:1])
    run(engine, rule, src, dst, None, extra=targets[:1])
    assert names(tmp_path / "archiv") == ["a.pdf"] and names(dst) == ["a.pdf"]
    # Ziel hinzugefügt: nur das neue Ziel bekommt die Datei
    result = run(engine, rule, src, dst, None, extra=targets[:2])
    assert result.items[0].status is ItemStatus.COPIED
    assert names(tmp_path / "buchhaltung") == ["a.pdf"]
    assert names(dst) == ["a.pdf"] and names(tmp_path / "archiv") == ["a.pdf"]


def test_dry_run_lists_every_target(
    engine: Engine, rule_id: int, tids: dict[str, int], tmp_path: Path
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    write(src / "a.pdf")
    targets = _three(tmp_path, rule_id, tids, down="Extra")
    result = run(engine, snap(rule_id), src, dst, None, extra=targets, dry_run=True)
    msg = result.items[0].message or ""
    assert (
        "würde verschieben" in msg and "Buchhaltung: " in msg and "Extra: nicht erreichbar" in msg
    )
    assert (src / "a.pdf").exists() and names(dst) == []


# -- Regellauf ----------------------------------------------------------------------------------


@pytest.fixture
def setup(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> Iterator[tuple[int, Path, Path, Path, Path]]:
    scans = fake_backend.export("nas", "scans")
    arch_remote = fake_backend.export("nas", "archiv")
    buha_remote = fake_backend.export("nas", "buha")
    src_id = make_mount("scans", share="scans")
    arch_id = make_mount("archiv", share="archiv")
    buha_id = make_mount("buha", share="buha")
    for mid in (src_id, arch_id, buha_id):
        manager.mount(mid)
    consume = tmp_path / "consume"
    consume.mkdir()
    with session_scope(session_factory) as s:
        r = Rule(
            name="Rechnungen → DMS + Buchhaltung + Archiv",
            src_mount_id=src_id,
            dst_path=str(consume),
            stability_seconds=0,
            targets=[
                RuleTarget(position=0, name="Buchhaltung", kind=TargetKind.MOUNT, mount_id=buha_id),
                RuleTarget(
                    position=1,
                    name="Archiv",
                    kind=TargetKind.MOUNT,
                    mount_id=arch_id,
                    path="rechnungen",
                ),
            ],
        )
        s.add(r)
        s.flush()
        rid = r.id
    yield rid, scans, consume, arch_remote, buha_remote


def test_runner_with_three_targets(
    setup: tuple[int, Path, Path, Path, Path],
    manager: MountManager,
    session_factory: sessionmaker[Session],
) -> None:
    rid, scans, consume, arch, buha = setup
    write(scans / "re.pdf", b"%PDF")
    result = RuleRunner(session_factory, manager, sleep=lambda _: None).run(rid)
    assert result.status is RunStatus.OK, result.message
    for path in (consume / "re.pdf", arch / "rechnungen" / "re.pdf", buha / "re.pdf"):
        assert path.read_bytes() == b"%PDF"
    assert not (scans / "re.pdf").exists()


def test_runner_unavailable_target_waits_without_blocking_others(
    setup: tuple[int, Path, Path, Path, Path],
    manager: MountManager,
    fake_backend: FakeMountBackend,
    session_factory: sessionmaker[Session],
) -> None:
    from netmover.runs import RunService

    rid, scans, consume, arch, buha = setup
    write(scans / "re.pdf")
    fake_backend.vanish(manager.mountpoint_for("buha"))
    fake_backend.fail_next_mount(times=50)
    runner = RuleRunner(session_factory, manager, sleep=lambda _: None)
    service = RunService(session_factory, runner)
    service.execute(rid)
    assert (consume / "re.pdf").exists() and (arch / "rechnungen" / "re.pdf").exists()
    assert (scans / "re.pdf").exists() and not (buha / "re.pdf").exists()
    with session_factory() as s:
        rule = s.get(Rule, rid)
        assert rule is not None and rule.waiting_since is not None
        assert "buha" in (rule.waiting_reason or "")
        runs_before = len(rule.runs)
    # weiterer Lauf während des Ausfalls: kein neuer Protokolleintrag
    assert service.execute(rid) is None
    with session_factory() as s:
        rule = s.get(Rule, rid)
        assert rule is not None and len(rule.runs) == runs_before

    fake_backend.mount_failures.clear()
    with session_factory() as s:
        buha_id = s.scalar(select(RuleTarget.mount_id).where(RuleTarget.name == "Buchhaltung"))
    assert buha_id is not None
    manager.mount(buha_id)  # Anbindung wieder da
    service.execute(rid)
    assert (buha / "re.pdf").exists() and not (scans / "re.pdf").exists()
    assert len(list(consume.iterdir())) == 1  # nicht doppelt geliefert
    with session_factory() as s:
        rule = s.get(Rule, rid)
        assert rule is not None and rule.waiting_since is None


def test_all_targets_unavailable_makes_rule_wait(
    setup: tuple[int, Path, Path, Path, Path],
    manager: MountManager,
    fake_backend: FakeMountBackend,
    session_factory: sessionmaker[Session],
) -> None:
    rid, scans, _consume, _arch, _buha = setup
    with session_scope(session_factory) as s:
        r = s.get(Rule, rid)
        assert r is not None
        r.dst_mount_id = r.targets[0].mount_id  # Hauptziel ebenfalls auf „buha“
        r.dst_path = "dms"
        r.targets = r.targets[:1]
    write(scans / "doc.pdf")
    fake_backend.vanish(manager.mountpoint_for("buha"))
    fake_backend.fail_next_mount(times=50)
    result = RuleRunner(session_factory, manager, sleep=lambda _: None).run(rid)
    assert result.unavailable and result.status is RunStatus.ERROR
    assert (scans / "doc.pdf").exists()


def test_mount_used_as_target_cannot_be_deleted(
    setup: tuple[int, Path, Path, Path, Path],
    session_factory: sessionmaker[Session],
    manager: MountManager,
) -> None:
    from netmover.admin import AdminError, delete_mount
    from netmover.models import Mount

    with session_scope(session_factory) as s:
        arch_id = s.scalar(select(Mount.id).where(Mount.name == "archiv"))
        assert arch_id is not None
        with pytest.raises(AdminError, match="Wird noch von Regeln benutzt"):
            delete_mount(s, manager, arch_id)


# -- Formular ---------------------------------------------------------------------------------


def _base(mid: int) -> dict[str, str]:
    return {
        "name": "x",
        "src_mount_id": str(mid),
        "dst_kind": "local",
        "dst_path": "/srv/consume",
        "interval_seconds": "30",
        "stability_seconds": "5",
        "op_timeout_seconds": "120",
    }


def test_rule_form_target_validation(
    session_factory: sessionmaker[Session], make_mount: MountFactory
) -> None:
    from netmover.admin import parse_rule_form

    mid = make_mount()
    base = _base(mid)
    with session_scope(session_factory) as s:
        _, err = parse_rule_form(
            s, {**base, "target_0_kind": "local", "target_0_path": "/srv/consume"}
        )
        assert "Hauptziel" in err["target_0_path"]
        _, err = parse_rule_form(s, {**base, "target_0_kind": "mount", "target_0_path": "a"})
        assert "target_0_mount_id" in err
        _, err = parse_rule_form(s, {**base, "target_0_kind": "local", "target_0_path": "rel"})
        assert "absoluten Pfad" in err["target_0_path"]
        two = {
            "target_0_kind": "local",
            "target_0_path": "/srv/a",
            "target_1_kind": "local",
            "target_1_path": "/srv/a",
        }
        _, err = parse_rule_form(s, {**base, **two})
        assert "Derselbe Ordner" in err["target_1_path"]
        _, err = parse_rule_form(
            s,
            {
                **base,
                **two,
                "target_1_path": "/srv/b",
                "target_0_name": "X",
                "target_1_name": "X",
            },
        )
        assert "schon vor" in err["target_1_name"]
        _, err = parse_rule_form(
            s, {**base, "target_0_kind": "mount", "target_0_mount_id": str(mid)}
        )
        assert "derselbe Ordner" in err["target_0_path"]  # = Quelle


def test_rule_form_saves_compacts_and_keeps_ledger(
    session_factory: sessionmaker[Session], make_mount: MountFactory
) -> None:
    from netmover.admin import parse_rule_form, save_rule

    mid = make_mount()
    arch = make_mount("archiv", share="archiv")
    form = {
        **_base(mid),
        # Platz 0 leer gelassen, Plätze 1 und 3 belegt → kompakt 0, 1
        "target_1_kind": "mount",
        "target_1_mount_id": str(arch),
        "target_1_path": "rechnungen",
        "target_1_layout": "date",
        "target_3_kind": "local",
        "target_3_name": "Buchhaltung",
        "target_3_path": "/srv/buha",
    }
    with session_scope(session_factory) as s:
        d, err = parse_rule_form(s, form)
        assert err == {}
        r, _ = save_rule(s, d)
        assert r is not None
        rid = r.id
        got = [(t.position, t.name, t.kind.value, t.path, t.layout.value) for t in r.targets]
        assert got == [
            (0, "archiv", "mount", "rechnungen", "date"),  # Name = Anbindung
            (1, "Buchhaltung", "local", "/srv/buha", "flat"),
        ]
        first, second = r.targets[0].id, r.targets[1].id
        for tid in (first, second):
            s.add(TargetFile(target_id=tid, src_relpath="a", size=1, mtime_ns=1, dst_path="x"))
    # erneut speichern: Archiv unverändert, Buchhaltung mit neuem Ordner
    with session_scope(session_factory) as s:
        d, err = parse_rule_form(s, {**form, "target_3_path": "/srv/buha2"})
        assert err == {}
        save_rule(s, d, rid)
    with session_factory() as s:
        r = s.get(Rule, rid)
        assert r is not None and [t.id for t in r.targets] == [first, second]
        kept = {f.target_id for f in s.scalars(select(TargetFile))}
        assert kept == {first}  # Buchführung nur bei geändertem Ordner verworfen
    # Ziel entfernen
    with session_scope(session_factory) as s:
        d, _ = parse_rule_form(s, {**form, "target_3_kind": ""})
        save_rule(s, d, rid)
    with session_factory() as s:
        r = s.get(Rule, rid)
        assert r is not None and [t.name for t in r.targets] == ["archiv"]


def test_rule_form_page_has_nine_target_slots(client: Any, state: Any) -> None:
    html = client.get("/rules/new").text
    assert "Weitere Ziele" in html and 'name="target_8_kind"' in html
    assert 'name="target_9_kind"' not in html
    assert 'data-show-for="target_0_kind:mount local|target_1_kind:mount local"' in html


# -- Export / Import --------------------------------------------------------------------------


def test_export_import_targets_and_legacy_archive(
    setup: tuple[int, Path, Path, Path, Path],
    session_factory: sessionmaker[Session],
    manager: MountManager,
) -> None:
    from netmover.settings_store import AppSettings
    from netmover.transfer import export_config, import_config

    with session_factory() as s:
        data = export_config(s, AppSettings())
    (rule,) = data["rules"]
    assert [(t["name"], t["mount"], t["path"]) for t in rule["targets"]] == [
        ("Buchhaltung", "buha", ""),
        ("Archiv", "archiv", "rechnungen"),
    ]
    new = {**rule, "name": "Kopie"}
    legacy = {k: v for k, v in rule.items() if k != "targets"} | {
        "name": "Alt",
        "archive_kind": "mount",
        "archive_mount": "archiv",
        "archive_path": "alt",
        "archive_layout": "date",
    }
    with session_scope(session_factory) as s:
        report = import_config(s, manager, {**data, "mounts": [], "rules": [new, legacy]})
    assert report.errors == [] and len(report.created) == 2
    with session_factory() as s:
        by_name = {r.name: r for r in s.scalars(select(Rule))}
        assert [t.name for t in by_name["Kopie"].targets] == ["Buchhaltung", "Archiv"]
        (t,) = by_name["Alt"].targets
        assert (t.name, t.path, t.layout.value) == ("Archiv", "alt", "date")
