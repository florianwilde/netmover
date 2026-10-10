"""Regellauf mit FakeMountBackend – Fallstricke 2, 8 und 13 sowie SFTP-Ziel."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netmover.crypto import SecretBox
from netmover.db import session_scope
from netmover.models import (
    Credential,
    CredentialKind,
    ItemStatus,
    Mount,
    MountType,
    Rule,
    RunStatus,
)
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager
from netmover.mover.runner import RuleRunner
from tests.conftest import MountFactory
from tests.sftp_stub import StubSFTPServer


@pytest.fixture
def setup(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
) -> tuple[int, int, int, Path, Path]:
    scans = fake_backend.export("nas", "scans")
    archiv = fake_backend.export("nas2", "archiv")
    src_id = make_mount("scans", server="nas", share="scans")
    dst_id = make_mount("archiv", server="nas2", share="archiv")
    manager.mount(src_id)
    manager.mount(dst_id)
    with session_scope(session_factory) as s:
        r = Rule(
            name="scans→archiv",
            src_mount_id=src_id,
            dst_mount_id=dst_id,
            dst_path="eingang",
            stability_seconds=1,
        )
        s.add(r)
        s.flush()
        rid = r.id
    return rid, src_id, dst_id, scans, archiv


@pytest.fixture
def runner(session_factory: sessionmaker[Session], manager: MountManager) -> RuleRunner:
    return RuleRunner(session_factory, manager, sleep=lambda _: None)


def test_moves_between_two_mounts(
    runner: RuleRunner, setup: tuple[int, int, int, Path, Path]
) -> None:
    rid, _, _, scans, archiv = setup
    (scans / "scan001.pdf").write_text("x")
    result = runner.run(rid)
    assert result.status is RunStatus.OK
    assert [(i.src, i.status) for i in result.items] == [("scan001.pdf", ItemStatus.MOVED)]
    assert (archiv / "eingang" / "scan001.pdf").read_text() == "x"
    assert not (scans / "scan001.pdf").exists()


# -- Fallstrick 2 ---------------------------------------------------------------------------


def test_unmounted_destination_aborts_and_writes_nothing_locally(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    fake_backend: FakeMountBackend,
    manager: MountManager,
) -> None:
    rid, _, _, scans, _archiv = setup
    (scans / "scan001.pdf").write_text("x")
    mp = manager.mountpoint_for("archiv")
    fake_backend.vanish(mp)  # Freigabe weg, Mountpoint ist jetzt ein leeres lokales Verzeichnis
    result = runner.run(rid)
    assert result.status is RunStatus.ERROR
    assert "nicht eingehängt" in (result.message or "")
    assert list(mp.iterdir()) == []  # nichts auf die lokale Platte geschrieben
    assert (scans / "scan001.pdf").exists()


def test_unmounted_source_aborts(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    fake_backend: FakeMountBackend,
    manager: MountManager,
) -> None:
    rid, *_ = setup
    fake_backend.vanish(manager.mountpoint_for("scans"))
    result = runner.run(rid)
    assert result.status is RunStatus.ERROR and result.items == []


def test_local_destination_under_mount_base_is_refused(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    manager: MountManager,
    session_factory: sessionmaker[Session],
) -> None:
    rid, *_ = setup
    with session_scope(session_factory) as s:
        r = s.get(Rule, rid)
        assert r is not None
        r.dst_mount_id = None
        r.dst_path = str(manager.mountpoint_for("archiv"))
    result = runner.run(rid)
    assert result.status is RunStatus.ERROR
    assert "Anbindung als Ziel" in (result.message or "")


def test_local_destination(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    rid, _, _, scans, _ = setup
    consume = tmp_path / "consume"
    consume.mkdir()
    with session_scope(session_factory) as s:
        r = s.get(Rule, rid)
        assert r is not None
        r.dst_mount_id = None
        r.dst_path = str(consume)
    (scans / "a.pdf").write_text("x")
    assert runner.run(rid).status is RunStatus.OK
    assert (consume / "a.pdf").exists()


# -- Fallstrick 8 / 13: Überlappung und Zeitlimit ---------------------------------------------


@pytest.fixture
def blocking_runner(
    session_factory: sessionmaker[Session], manager: MountManager
) -> Iterator[tuple[RuleRunner, threading.Event, threading.Event]]:
    entered = threading.Event()
    release = threading.Event()

    def sleep(_: float) -> None:  # Stabilitätsfenster „hängt“
        entered.set()
        release.wait(10)

    yield RuleRunner(session_factory, manager, sleep=sleep), entered, release
    release.set()


def test_overlapping_run_is_skipped(
    blocking_runner: tuple[RuleRunner, threading.Event, threading.Event],
    setup: tuple[int, int, int, Path, Path],
) -> None:
    runner, entered, release = blocking_runner
    rid, _, _, scans, archiv = setup
    (scans / "a.pdf").write_text("x")
    results = []
    t = threading.Thread(target=lambda: results.append(runner.run(rid)))
    t.start()
    assert entered.wait(5)
    skipped = runner.run(rid)
    assert skipped.status is RunStatus.SKIPPED
    assert "läuft noch" in (skipped.message or "")
    release.set()
    t.join(5)
    assert results[0].status is RunStatus.OK
    assert (archiv / "eingang" / "a.pdf").exists()


def test_hanging_run_times_out_and_blocks_rule_until_worker_ends(
    blocking_runner: tuple[RuleRunner, threading.Event, threading.Event],
    setup: tuple[int, int, int, Path, Path],
    session_factory: sessionmaker[Session],
) -> None:
    runner, _entered, release = blocking_runner
    rid, _, _, scans, archiv = setup
    with session_scope(session_factory) as s:
        r = s.get(Rule, rid)
        assert r is not None
        r.op_timeout_seconds = 1
    (scans / "a.pdf").write_text("x")

    t0 = time.monotonic()
    result = runner.run(rid)
    assert time.monotonic() - t0 < 5  # Scheduler wird nicht blockiert
    assert result.status is RunStatus.ERROR
    assert "Keine Reaktion" in (result.message or "")
    assert runner.running_since(rid) is not None

    # solange der alte Worker hängt: neue Termine überspringen
    assert runner.run(rid).status is RunStatus.SKIPPED

    # Worker wacht auf → sieht den Abbruch, verschiebt nichts mehr, gibt Lock frei
    release.set()
    for _ in range(50):
        if runner.running_since(rid) is None:
            break
        time.sleep(0.05)
    assert runner.running_since(rid) is None
    assert (scans / "a.pdf").exists()
    assert not (archiv / "eingang" / "a.pdf").exists()


def test_stale_source_is_recovered_before_run(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    fake_backend: FakeMountBackend,
    manager: MountManager,
) -> None:
    import errno

    rid, _, _, scans, archiv = setup
    (scans / "a.pdf").write_text("x")
    fake_backend.inject_fault(manager.mountpoint_for("scans"), errno.ESTALE)
    result = runner.run(rid)
    assert result.status is RunStatus.OK
    assert (archiv / "eingang" / "a.pdf").exists()
    assert ("unmount", True) in [(c.action, c.lazy) for c in fake_backend.calls]


def test_dry_run_flag_from_rule_and_override(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    session_factory: sessionmaker[Session],
) -> None:
    rid, _, _, scans, _archiv = setup
    (scans / "a.pdf").write_text("x")
    with session_scope(session_factory) as s:
        r = s.get(Rule, rid)
        assert r is not None
        r.dry_run = True
    result = runner.run(rid)
    assert result.dry_run and result.items[0].status is ItemStatus.PLANNED
    assert (scans / "a.pdf").exists()
    assert runner.run(rid, dry_run=False).items[0].status is ItemStatus.MOVED


def test_unknown_rule(runner: RuleRunner) -> None:
    assert runner.run(999).status is RunStatus.ERROR


# -- SFTP-Ziel --------------------------------------------------------------------------------


@pytest.fixture
def sftp_server(tmp_path: Path) -> Iterator[StubSFTPServer]:
    root = tmp_path / "sftp-root"
    (root / "consume").mkdir(parents=True)
    srv = StubSFTPServer(root).start()
    yield srv
    srv.stop()


def test_move_to_sftp_destination_with_collision(
    runner: RuleRunner,
    setup: tuple[int, int, int, Path, Path],
    sftp_server: StubSFTPServer,
    session_factory: sessionmaker[Session],
    secretbox: SecretBox,
) -> None:
    rid, _, _, scans, _ = setup
    with session_scope(session_factory) as s:
        c = Credential(
            name="pk",
            kind=CredentialKind.SSH_KEY,
            username=sftp_server.username,
            secret_enc=secretbox.encrypt(sftp_server.client_private_key),
        )
        s.add(c)
        s.flush()
        m = Mount(
            name="paperless",
            slug="paperless",
            type=MountType.SFTP,
            server="127.0.0.1",
            share="/consume",
            port=sftp_server.port,
            cred_id=c.id,
            host_key=sftp_server.host_key_string,
        )
        s.add(m)
        s.flush()
        r = s.get(Rule, rid)
        assert r is not None
        r.dst_mount_id = m.id
        r.dst_path = ""
    consume = sftp_server.root / "consume"
    (consume / "scan.pdf").write_text("alt")
    (scans / "scan.pdf").write_bytes(b"%PDF neu" * 50_000)
    result = runner.run(rid)
    assert result.status is RunStatus.OK, result.message
    assert result.items[0].dst == "paperless:/consume/scan-1.pdf"
    assert (consume / "scan-1.pdf").read_bytes() == b"%PDF neu" * 50_000
    assert (consume / "scan.pdf").read_text() == "alt"
    assert not (scans / "scan.pdf").exists()
    assert sorted(p.name for p in consume.iterdir()) == ["scan-1.pdf", "scan.pdf"]
