"""0.5.0: SFTP als Quelle – dieselben Garantien wie bei eingehängten Freigaben."""

from __future__ import annotations

import errno
import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netmover.crypto import SecretBox
from netmover.db import session_scope
from netmover.models import (
    CopiedFile,
    Credential,
    CredentialKind,
    ItemStatus,
    Mount,
    MountType,
    PatternType,
    Rule,
    RuleAction,
    RunStatus,
)
from netmover.mounting.manager import MountManager
from netmover.mounting.sftp import SftpConfig, connect
from netmover.mover.destinations import LocalDestination, SftpDestination
from netmover.mover.engine import Engine, RuleSnapshot, RunResult
from netmover.mover.ledger import Ledger
from netmover.mover.runner import RuleRunner
from netmover.mover.scan import Matcher
from netmover.mover.sources import SftpSource
from netmover.timeouts import CancelToken
from tests.conftest import MountFactory
from tests.sftp_stub import StubSFTPServer


@pytest.fixture
def server(tmp_path: Path) -> Iterator[StubSFTPServer]:
    root = tmp_path / "sftp-root"
    for d in ("eingang", "ziel", "consume"):
        (root / d).mkdir(parents=True)
    srv = StubSFTPServer(root).start()
    yield srv
    srv.stop()


def _cfg(server: StubSFTPServer) -> SftpConfig:
    return SftpConfig(
        "127.0.0.1",
        server.port,
        server.username,
        "/",
        private_key=server.client_private_key,
        host_key=server.host_key_string,
    )


@pytest.fixture
def source(server: StubSFTPServer) -> Iterator[SftpSource]:
    s = SftpSource(connect(_cfg(server)), "/", "eingang", label="nas2")
    yield s
    s.close()


@pytest.fixture
def rule_id(make_mount: MountFactory, session_factory: sessionmaker[Session]) -> int:
    mid = make_mount()
    with session_scope(session_factory) as s:
        r = Rule(name="r", src_mount_id=mid, dst_path="/x")
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def hooks() -> list[Callable[[], None]]:
    return []


@pytest.fixture
def engine(session_factory: sessionmaker[Session], hooks: list[Callable[[], None]]) -> Engine:
    def sleep(_: float) -> None:
        for h in hooks:
            h()

    return Engine(Ledger(session_factory), sleep=sleep)


def snap(rule_id: int, **kw: object) -> RuleSnapshot:
    return RuleSnapshot(id=rule_id, name="r", stability_seconds=2, **kw)  # type: ignore[arg-type]


def write(path: Path, data: bytes = b"inhalt") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run(
    engine: Engine, rule: RuleSnapshot, src: SftpSource, dest: object, **kw: object
) -> RunResult:
    return engine.execute(rule, src, dest, CancelToken(), **kw)  # type: ignore[arg-type]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# -- Scan ----------------------------------------------------------------------------------


def test_scan_filters_like_local_sources(source: SftpSource, server: StubSFTPServer) -> None:
    root = server.root / "eingang"
    for name in ("a.pdf", "b.PDF", "notiz.txt", ".versteckt.pdf", "halb.pdf.part", "x.tmp"):
        write(root / name)
    write(root / "max" / "c.pdf")
    write(root / "@eaDir" / "a.pdf" / "thumb.pdf")
    write(root / "quarantaene" / "d.pdf")
    m = Matcher("*.pdf", PatternType.GLOB)
    assert [c.relpath for c in source.scan(m, recursive=False)] == ["a.pdf", "b.PDF"]
    rec = [
        c.relpath
        for c in source.scan(m, recursive=True, exclude=frozenset({"eingang/quarantaene"}))
    ]
    assert rec == ["a.pdf", "b.PDF", "max/c.pdf"]
    c = source.scan(m, recursive=False)[0]
    assert c.path is None and c.state.size == len(b"inhalt")


def test_missing_source_dir_aborts(
    engine: Engine, rule_id: int, server: StubSFTPServer, tmp_path: Path
) -> None:
    src = SftpSource(connect(_cfg(server)), "/", "gibtsnicht")
    try:
        result = run(
            engine, snap(rule_id), src, LocalDestination(tmp_path / "dst", create_root=True)
        )
    finally:
        src.close()
    assert result.status is RunStatus.ERROR and "existiert nicht" in (result.message or "")


# -- Verschieben / Kopieren ----------------------------------------------------------------


def test_move_sftp_to_local(
    engine: Engine, rule_id: int, source: SftpSource, server: StubSFTPServer, tmp_path: Path
) -> None:
    f = write(server.root / "eingang" / "scan001.pdf", os.urandom(300_000))
    digest = sha(f)
    dst = tmp_path / "dst"
    dst.mkdir()
    write(dst / "scan001.pdf", b"alt")
    result = run(engine, snap(rule_id), source, LocalDestination(dst))
    assert result.status is RunStatus.OK, result.message
    assert [(i.src, i.status) for i in result.items] == [("scan001.pdf", ItemStatus.MOVED)]
    assert sha(dst / "scan001-1.pdf") == digest and (dst / "scan001.pdf").read_bytes() == b"alt"
    assert not f.exists()
    assert sorted(p.name for p in dst.iterdir()) == ["scan001-1.pdf", "scan001.pdf"]


def test_move_sftp_to_sftp(
    engine: Engine, rule_id: int, source: SftpSource, server: StubSFTPServer
) -> None:
    f = write(server.root / "eingang" / "a.pdf", os.urandom(50_000))
    digest = sha(f)
    dest = SftpDestination(connect(_cfg(server)), "/ziel", label="nas3")
    try:
        result = run(engine, snap(rule_id), source, dest)
    finally:
        dest.close()
    assert result.items[0].status is ItemStatus.MOVED and result.items[0].dst == "nas3:/ziel/a.pdf"
    assert sha(server.root / "ziel" / "a.pdf") == digest and not f.exists()
    assert list((server.root / "ziel").glob(".netmover-*")) == []


def test_copy_mode_remembers_versions(
    engine: Engine,
    rule_id: int,
    source: SftpSource,
    server: StubSFTPServer,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    f = write(server.root / "eingang" / "a.pdf", b"v1")
    dst = tmp_path / "dst"
    dst.mkdir()
    rule = snap(rule_id, action=RuleAction.COPY)
    for _ in range(3):
        run(engine, rule, source, LocalDestination(dst))
    assert sorted(p.name for p in dst.iterdir()) == ["a.pdf"] and f.exists()
    f.write_bytes(b"version 2")  # andere Größe → neue Version
    run(engine, rule, source, LocalDestination(dst))
    assert sorted(p.name for p in dst.iterdir()) == ["a-1.pdf", "a.pdf"]
    with session_factory() as s:
        assert len(s.scalars(select(CopiedFile)).all()) == 2


def test_mtime_is_carried_over(
    engine: Engine, rule_id: int, source: SftpSource, server: StubSFTPServer, tmp_path: Path
) -> None:
    f = write(server.root / "eingang" / "a.pdf")
    os.utime(f, (1_600_000_000, 1_600_000_123))
    dst = tmp_path / "dst"
    dst.mkdir()
    run(engine, snap(rule_id, action=RuleAction.COPY), source, LocalDestination(dst))
    assert int((dst / "a.pdf").stat().st_mtime) == 1_600_000_123


# -- Fallstricke ---------------------------------------------------------------------------


def test_growing_file_is_not_touched(
    engine: Engine,
    rule_id: int,
    source: SftpSource,
    server: StubSFTPServer,
    tmp_path: Path,
    hooks: list[Callable[[], None]],
) -> None:
    growing = write(server.root / "eingang" / "wachsend.pdf", b"x" * 10)
    hooks.append(lambda: growing.write_bytes(b"x" * 20))
    dst = tmp_path / "dst"
    dst.mkdir()
    result = run(engine, snap(rule_id), source, LocalDestination(dst))
    assert result.items[0].status is ItemStatus.SKIPPED and growing.exists()
    assert list(dst.iterdir()) == []


def test_source_changed_during_transfer_keeps_source(
    engine: Engine,
    rule_id: int,
    source: SftpSource,
    server: StubSFTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = write(server.root / "eingang" / "a.pdf", b"original")
    real = LocalDestination._apply_meta

    def change(self: LocalDestination, rel: str, meta: object) -> list[str]:
        f.write_bytes(b"geaendert!!")
        return real(self, rel, meta)  # type: ignore[arg-type]

    monkeypatch.setattr(LocalDestination, "_apply_meta", change)
    dst = tmp_path / "dst"
    dst.mkdir()
    result = run(engine, snap(rule_id), source, LocalDestination(dst))
    assert result.items[0].status is ItemStatus.SKIPPED and f.exists()
    assert list(dst.iterdir()) == []


def test_source_not_deletable_prevents_duplicates(
    engine: Engine,
    rule_id: int,
    source: SftpSource,
    server: StubSFTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(server.root / "eingang" / "a.pdf")

    def deny(*a: object, **kw: object) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(source.client, "remove", deny)
    dst = tmp_path / "dst"
    dst.mkdir()
    first = run(engine, snap(rule_id), source, LocalDestination(dst))
    assert first.items[0].status is ItemStatus.FAILED and "nicht löschbar" in (
        first.items[0].message or ""
    )
    second = run(engine, snap(rule_id), source, LocalDestination(dst))
    assert second.items == [] and sorted(p.name for p in dst.iterdir()) == ["a.pdf"]


def test_quarantine_on_sftp_source(
    engine: Engine,
    rule_id: int,
    source: SftpSource,
    server: StubSFTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from netmover.mover.destinations import TransferError

    def broken(*a: object, **kw: object) -> None:
        raise TransferError("kaputt")

    monkeypatch.setattr(LocalDestination, "_link_noreplace", broken)
    write(server.root / "eingang" / "defekt.pdf")
    dst = tmp_path / "dst"
    dst.mkdir()
    rule = snap(rule_id, quarantine_after=2)
    for _ in range(2):
        run(engine, rule, source, LocalDestination(dst), quarantine_rel="eingang/_q")
    r = run(engine, rule, source, LocalDestination(dst), quarantine_rel="eingang/_q")
    assert r.items[0].status is ItemStatus.QUARANTINED
    assert r.items[0].dst == "nas2:/eingang/_q/defekt.pdf"
    assert (server.root / "eingang" / "_q" / "defekt.pdf").exists()


def test_connection_loss_aborts_run(
    engine: Engine,
    rule_id: int,
    source: SftpSource,
    server: StubSFTPServer,
    tmp_path: Path,
    hooks: list[Callable[[], None]],
) -> None:
    write(server.root / "eingang" / "a.pdf")
    write(server.root / "eingang" / "b.pdf")
    hooks.append(lambda: source.session.transport.close())  # Verbindung bricht weg
    dst = tmp_path / "dst"
    dst.mkdir()
    result = run(engine, snap(rule_id), source, LocalDestination(dst))
    assert result.status is RunStatus.ERROR
    assert (server.root / "eingang" / "a.pdf").exists() and list(dst.iterdir()) == []


# -- Regellauf mit SFTP-Quelle ------------------------------------------------------------


def test_runner_with_sftp_source(
    server: StubSFTPServer,
    manager: MountManager,
    session_factory: sessionmaker[Session],
    secretbox: SecretBox,
    tmp_path: Path,
) -> None:
    write(server.root / "eingang" / "scan.pdf", b"%PDF")
    consume = tmp_path / "consume"
    consume.mkdir()
    with session_scope(session_factory) as s:
        c = Credential(
            name="k",
            kind=CredentialKind.SSH_KEY,
            username=server.username,
            secret_enc=secretbox.encrypt(server.client_private_key),
        )
        s.add(c)
        s.flush()
        m = Mount(
            name="Scanner-Server",
            slug="scanner",
            type=MountType.SFTP,
            server="127.0.0.1",
            share="/",
            port=server.port,
            cred_id=c.id,
            host_key=server.host_key_string,
        )
        s.add(m)
        s.flush()
        r = Rule(
            name="SFTP → lokal",
            src_mount_id=m.id,
            src_subpath="eingang",
            dst_path=str(consume),
            stability_seconds=2,
        )
        s.add(r)
        s.flush()
        rid = r.id
    result = RuleRunner(session_factory, manager, sleep=lambda _: None).run(rid)
    assert result.status is RunStatus.OK, result.message
    assert result.items[0].dst == str(consume / "scan.pdf")
    assert (consume / "scan.pdf").read_bytes() == b"%PDF"
    assert not (server.root / "eingang" / "scan.pdf").exists()


def test_rule_form_accepts_sftp_source_with_min_stability(
    session_factory: sessionmaker[Session],
) -> None:
    from netmover.admin import parse_rule_form

    with session_scope(session_factory) as s:
        m = Mount(name="sftp", slug="sftp", type=MountType.SFTP, server="10.0.0.1", share="/in")
        s.add(m)
        s.flush()
        form = {
            "name": "x",
            "src_mount_id": str(m.id),
            "dst_kind": "local",
            "dst_path": "/tmp",
            "interval_seconds": "30",
            "stability_seconds": "1",
            "op_timeout_seconds": "120",
        }
        _, errors = parse_rule_form(s, form)
        assert "src_mount_id" not in errors and "mindestens 2 s" in errors["stability_seconds"]
        _, errors = parse_rule_form(s, {**form, "stability_seconds": "3"})
        assert errors == {}
