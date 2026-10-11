"""0.7.0: Archiv – Kopie ans Ziel UND ins Archiv, Quelle erst danach löschen."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import (
    ArchivedFile,
    ArchiveKind,
    CollisionPolicy,
    ItemStatus,
    Rule,
    RuleAction,
    RunStatus,
)
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager
from netmover.mover.destinations import LocalDestination, TransferError
from netmover.mover.engine import Engine, RuleSnapshot, RunResult
from netmover.mover.ledger import Ledger
from netmover.mover.runner import RuleRunner
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


def run(
    engine: Engine, rule: RuleSnapshot, src: Path, dst: Path, arch: Path | None, **kw: object
) -> RunResult:
    archive = LocalDestination(arch) if arch is not None else None
    return engine.execute(rule, src, LocalDestination(dst), CancelToken(), archive=archive, **kw)  # type: ignore[arg-type]


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
    assert item.status is ItemStatus.MOVED and item.message and "archiviert:" in item.message
    assert (dst / "scan001.pdf").read_bytes() == data == (arch / "scan001.pdf").read_bytes()
    assert not (src / "scan001.pdf").exists()
    assert names(dst) == ["scan001.pdf"] and names(arch) == ["scan001.pdf"]  # keine .part-Reste


def test_date_layout(engine: Engine, rule_id: int, dirs: tuple[Path, Path, Path]) -> None:
    src, dst, arch = dirs
    write(src / "max" / "a.pdf")
    run(engine, snap(rule_id, recursive=True, archive_layout="date"), src, dst, arch)
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
        assert len(s.scalars(select(ArchivedFile)).all()) == 1

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
    assert "zu wenig Platz im Archiv" in (result.items[0].message or "")
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
    result = run(engine, snap(rule_id, archive_layout="date"), src, dst, arch, dry_run=True)
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


# -- Regellauf ----------------------------------------------------------------------------------


@pytest.fixture
def setup(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> Iterator[tuple[int, Path, Path, Path]]:
    scans = fake_backend.export("nas", "scans")
    arch_remote = fake_backend.export("nas", "archiv")
    src_id = make_mount("scans", share="scans")
    arch_id = make_mount("archiv", share="archiv")
    manager.mount(src_id)
    manager.mount(arch_id)
    consume = tmp_path / "consume"
    consume.mkdir()
    with session_scope(session_factory) as s:
        r = Rule(
            name="Scans → Paperless + Archiv",
            src_mount_id=src_id,
            dst_path=str(consume),
            stability_seconds=0,
            archive_kind=ArchiveKind.MOUNT,
            archive_mount_id=arch_id,
            archive_path="scans",
        )
        s.add(r)
        s.flush()
        rid = r.id
    yield rid, scans, consume, arch_remote


def test_runner_with_archive_mount(
    setup: tuple[int, Path, Path, Path],
    manager: MountManager,
    session_factory: sessionmaker[Session],
) -> None:
    rid, scans, consume, arch = setup
    write(scans / "doc.pdf", b"%PDF")
    result = RuleRunner(session_factory, manager, sleep=lambda _: None).run(rid)
    assert result.status is RunStatus.OK, result.message
    assert (
        (consume / "doc.pdf").read_bytes() == b"%PDF" == (arch / "scans" / "doc.pdf").read_bytes()
    )
    assert not (scans / "doc.pdf").exists()


def test_unavailable_archive_makes_rule_wait(
    setup: tuple[int, Path, Path, Path],
    manager: MountManager,
    fake_backend: FakeMountBackend,
    session_factory: sessionmaker[Session],
) -> None:
    rid, scans, consume, _arch = setup
    write(scans / "doc.pdf")
    fake_backend.vanish(manager.mountpoint_for("archiv"))
    result = RuleRunner(session_factory, manager, sleep=lambda _: None).run(rid)
    assert result.unavailable and result.status is RunStatus.ERROR
    assert (scans / "doc.pdf").exists() and list(consume.iterdir()) == []


def test_mount_used_as_archive_cannot_be_deleted(
    setup: tuple[int, Path, Path, Path],
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


def test_rule_form_archive_validation(
    session_factory: sessionmaker[Session], make_mount: MountFactory
) -> None:
    from netmover.admin import parse_rule_form, save_rule

    mid = make_mount()
    base = {
        "name": "x",
        "src_mount_id": str(mid),
        "dst_kind": "local",
        "dst_path": "/srv/consume",
        "interval_seconds": "30",
        "stability_seconds": "5",
        "op_timeout_seconds": "120",
    }
    with session_scope(session_factory) as s:
        _, err = parse_rule_form(
            s, {**base, "archive_kind": "local", "archive_path": "/srv/consume"}
        )
        assert "derselbe Ordner" in err["archive_path"]
        _, err = parse_rule_form(s, {**base, "archive_kind": "mount", "archive_path": "a"})
        assert "archive_mount_id" in err
        _, err = parse_rule_form(s, {**base, "archive_kind": "local", "archive_path": "relativ"})
        assert "absoluten Pfad" in err["archive_path"]
        d, err = parse_rule_form(
            s,
            {
                **base,
                "archive_kind": "mount",
                "archive_mount_id": str(mid),
                "archive_path": "archiv/scans",
                "archive_layout": "date",
            },
        )
        assert err == {}
        r, _ = save_rule(s, d)
        assert r is not None and r.archive_kind is ArchiveKind.MOUNT
        assert r.archive_path == "archiv/scans" and r.archive_layout.value == "date"
