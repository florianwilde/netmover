"""Mount-Verwaltung mit FakeMountBackend – Fallstricke 2, 6, 7 und 13."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netmover.models import CredentialKind, Mount, MountState, MountType
from netmover.mounting.backend import MountError
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import (
    CredentialData,
    MountConfig,
    MountManager,
    MountUnavailable,
    build_spec,
)
from netmover.validation import ValidationError
from tests.conftest import MountFactory


def _state(sf: sessionmaker[Session], mount_id: int) -> Mount:
    with sf() as s:
        m = s.get(Mount, mount_id)
        assert m is not None
        s.expunge(m)
        return m


# -- build_spec -------------------------------------------------------------------


def test_build_spec_cifs(tmp_path: Path) -> None:
    cfg = MountConfig(
        "scans",
        MountType.CIFS,
        "nas",
        "scans",
        uid=1000,
        gid=100,
        file_mode="0644",
        extra_options="vers=3.0",
    )
    cred = CredentialData(CredentialKind.PASSWORD, "scanner", "pw", "WG")
    spec = build_spec(cfg, tmp_path, cred)
    assert spec.fstype == "cifs"
    assert spec.source == "//nas/scans"
    assert spec.options == ("vers=3.0", "uid=1000", "gid=100", "file_mode=0644")
    assert spec.credentials is not None and spec.credentials.password == "pw"
    assert "pw" not in repr(spec)


def test_build_spec_nfs() -> None:
    nfs3 = build_spec(MountConfig("a", MountType.NFS3, "nas", "/volume1/a"), Path("/x"), None)
    assert (nfs3.fstype, nfs3.source, nfs3.options) == ("nfs", "nas:/volume1/a", ("vers=3",))
    nfs4 = build_spec(MountConfig("a", MountType.NFS4, "nas", "/volume1/a"), Path("/x"), None)
    assert (nfs4.fstype, nfs4.options) == ("nfs4", ())


def test_build_spec_guest_and_revalidation() -> None:
    spec = build_spec(MountConfig("a", MountType.CIFS, "nas", "pub"), Path("/x"), None)
    assert spec.options == ("guest",)
    # Auch Werte „aus der Datenbank“ laufen erneut durch die Validierung
    with pytest.raises(ValidationError):
        build_spec(MountConfig("a", MountType.CIFS, "nas;id", "pub"), Path("/x"), None)
    with pytest.raises(ValidationError):
        build_spec(
            MountConfig("a", MountType.CIFS, "nas", "pub", extra_options="password=x"),
            Path("/x"),
            None,
        )


# -- Einhängen / Aushängen (Fallstrick 6) --------------------------------------------


def test_mount_creates_mountpoint_under_base_and_unmount_removes_it(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    mount_base: Path,
    session_factory: sessionmaker[Session],
) -> None:
    remote = fake_backend.export("nas", "scans")
    (remote / "a.pdf").write_text("x")
    mid = make_mount()
    manager.mount(mid)
    mp = mount_base.resolve() / "scans"
    assert fake_backend.is_mounted(mp)
    assert (mp / "a.pdf").read_text() == "x"
    m = _state(session_factory, mid)
    assert m.state is MountState.MOUNTED and m.last_mounted_at is not None
    # Zugangsdaten wurden entschlüsselt übergeben
    spec = fake_backend.calls[0].spec
    assert spec is not None and spec.credentials is not None
    assert spec.credentials.password == "geheim123"

    manager.unmount(mid)
    assert not mp.exists()
    assert _state(session_factory, mid).state is MountState.UNMOUNTED


def test_mount_refuses_non_empty_mountpoint(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    mount_base: Path,
    session_factory: sessionmaker[Session],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    mp = mount_base.resolve() / "scans"
    mp.mkdir(parents=True)
    (mp / "verirrt.pdf").write_text("lokal geschrieben")
    with pytest.raises(MountError, match="nicht leer"):
        manager.mount(mid)
    assert not fake_backend.calls
    assert (mp / "verirrt.pdf").exists()
    assert _state(session_factory, mid).state is MountState.ERROR


def test_failed_mount_cleans_up_and_records_error(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    mount_base: Path,
    session_factory: sessionmaker[Session],
) -> None:
    mid = make_mount()  # nicht exportiert → „No such file or directory“
    with pytest.raises(MountError):
        manager.mount(mid)
    assert not (mount_base.resolve() / "scans").exists()
    m = _state(session_factory, mid)
    assert m.state is MountState.ERROR
    assert m.last_error and "No such file" in m.last_error


def test_mountpoint_for_rejects_escape(manager: MountManager) -> None:
    for bad in ["../etc", "a/b", "", "Scans"]:
        with pytest.raises(ValidationError):
            manager.mountpoint_for(bad)


def test_sftp_is_never_mounted(manager: MountManager, make_mount: MountFactory) -> None:
    mid = make_mount("paperless", type=MountType.SFTP, share="/consume")
    with pytest.raises(MountError, match="SFTP"):
        manager.mount(mid)


def test_restore_all_only_auto_mount(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    notifications: list[tuple[str, str, str]],
) -> None:
    fake_backend.export("nas", "a")
    fake_backend.export("nas", "b")
    a = make_mount("a", share="a")
    make_mount("b", share="b", auto_mount=False)
    make_mount("c", share="fehlt")
    make_mount("d", share="a", enabled=False)
    results = {r.name: r.ok for r in manager.restore_all()}
    assert results == {"a": True, "c": False}
    assert fake_backend.is_mounted(manager.mountpoint_for("a"))
    assert notifications and notifications[0][:2] == ("mount.restore_failed", "c")
    # zweiter Aufruf ist idempotent
    manager.mount(a)
    assert (
        sum(1 for c in fake_backend.calls if c.action == "mount" and c.mountpoint.name == "a") == 1
    )


# -- Test (drei Schritte) -----------------------------------------------------------


def test_check_unsaved_config_mounts_temporarily(
    manager: MountManager, fake_backend: FakeMountBackend, mount_base: Path
) -> None:
    fake_backend.export("nas", "scans")
    cfg = MountConfig("neu", MountType.CIFS, "nas", "scans")
    cred = CredentialData(CredentialKind.PASSWORD, "u", "pw")
    report = manager.check(cfg, cred)
    assert [(s.name, s.ok) for s in report.steps] == [
        ("mount", True),
        ("read", True),
        ("write", True),
    ]
    assert report.can_write and report.can_delete
    assert report.free_bytes is not None
    assert [r.value for r in report.roles] == ["Quelle mit Löschen", "Ziel"]
    # temporärer Mountpoint wieder weg, Testdatei gelöscht
    assert list(mount_base.iterdir()) == []
    assert list(fake_backend.remote_root.joinpath("nas", "scans").iterdir()) == []
    assert not fake_backend.mounted


def test_check_mount_failure_reports_steps(manager: MountManager, mount_base: Path) -> None:
    report = manager.check(MountConfig("x", MountType.CIFS, "nas", "fehlt"), None)
    assert [(s.name, s.ok) for s in report.steps] == [
        ("mount", False),
        ("read", None),
        ("write", None),
    ]
    assert not report.ok
    assert list(mount_base.iterdir()) == []


def test_check_read_only_share_is_source_copy_only(
    manager: MountManager, fake_backend: FakeMountBackend
) -> None:
    remote = fake_backend.export("nas", "ro")
    remote.chmod(0o555)
    try:
        report = manager.check(MountConfig("ro", MountType.CIFS, "nas", "ro"), None)
    finally:
        remote.chmod(0o755)
    assert report.can_read and not report.can_write
    assert [r.value for r in report.roles] == ["nur Quelle, kopieren"]
    assert report.steps[2].ok is False


def test_check_stores_result_on_saved_mount(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    m = _state(session_factory, mid)
    report = manager.check(MountConfig.from_model(m), None, mount_id=mid)
    assert report.steps[0].message == "bereits eingehängt"
    m = _state(session_factory, mid)
    assert m.write_ok is True and m.last_test_result and '"ok": true' in m.last_test_result
    assert fake_backend.is_mounted(manager.mountpoint_for("scans"))  # bleibt eingehängt


# -- Fallstrick 2: nicht eingehängt ---------------------------------------------------


def test_check_alive_refuses_unmounted_mountpoint(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    mp = manager.check_alive(mid)
    assert mp is not None
    fake_backend.vanish(mp)
    assert mp.is_dir() and list(mp.iterdir()) == []  # sieht aus wie eine leere Freigabe
    with pytest.raises(MountUnavailable, match="nicht eingehängt"):
        manager.check_alive(mid)
    m = _state(session_factory, mid)
    assert m.state is MountState.ERROR
    # kein Wiederherstellungsversuch, kein Schreiben
    assert [c.action for c in fake_backend.calls] == ["mount"]
    assert list(mp.iterdir()) == []


# -- Fallstrick 13: gestörte und hängende Mounts ---------------------------------------


@pytest.mark.parametrize("code", [errno.ESTALE, errno.EIO, errno.ENOTCONN])
def test_stale_mount_is_lazily_unmounted_and_remounted(
    code: int,
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    mp = manager.mountpoint_for("scans")
    fake_backend.inject_fault(mp, code)
    assert manager.check_alive(mid) == mp
    actions = [(c.action, c.lazy) for c in fake_backend.calls]
    assert actions == [("mount", False), ("unmount", True), ("mount", False)]
    m = _state(session_factory, mid)
    assert m.state is MountState.MOUNTED and m.last_error is None


def test_recovery_gives_up_after_three_attempts_with_growing_delay(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
    sleeps: list[float],
    notifications: list[tuple[str, str, str]],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    mp = manager.mountpoint_for("scans")
    fake_backend.inject_fault(mp, errno.ESTALE)
    fake_backend.fail_next_mount(times=3)
    with pytest.raises(MountUnavailable, match="3 Versuchen"):
        manager.check_alive(mid)
    assert sleeps == [1, 2]  # 1. Versuch sofort, dann wachsender Abstand
    assert sum(1 for c in fake_backend.calls if c.action == "mount") == 4
    m = _state(session_factory, mid)
    assert m.state is MountState.ERROR
    assert notifications[-1][0] == "mount.error"


def test_hanging_mount_times_out_and_is_recovered(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    mp = manager.mountpoint_for("scans")
    fake_backend.inject_fault(mp, "hang")
    assert manager.check_alive(mid) == mp  # blockiert nicht, Zeitlimit 0.5 s
    assert ("unmount", True) in [(c.action, c.lazy) for c in fake_backend.calls]
    assert _state(session_factory, mid).state is MountState.MOUNTED


def test_hanging_mount_without_recovery_shows_since(
    manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    mp = manager.mountpoint_for("scans")
    fake_backend.heal_on_remount = False
    fake_backend.inject_fault(mp, "hang")
    with pytest.raises(MountUnavailable):
        manager.check_alive(mid)
    m = _state(session_factory, mid)
    assert m.state is MountState.ERROR
    assert m.state_since is not None
    assert m.last_error and "keine Antwort" in m.last_error


def test_non_recoverable_error_does_not_remount(
    manager: MountManager, fake_backend: FakeMountBackend, make_mount: MountFactory
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    manager.mount(mid)
    fake_backend.inject_fault(manager.mountpoint_for("scans"), errno.EACCES)
    with pytest.raises(MountUnavailable):
        manager.check_alive(mid)
    assert [c.action for c in fake_backend.calls] == ["mount"]


def test_usage(
    manager: MountManager, fake_backend: FakeMountBackend, make_mount: MountFactory
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    assert manager.usage(mid) is None
    manager.mount(mid)
    usage = manager.usage(mid)
    assert usage is not None and usage[0] >= usage[1] > 0


# -- Praxisbefund: NAS-Neustart dauert länger als die Wiederherstellung ----------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def retry_manager(
    session_factory: sessionmaker[Session],
    fake_backend: FakeMountBackend,
    secretbox: object,
    mount_base: Path,
    notifications: list[tuple[str, str, str]],
    clock: Clock,
) -> MountManager:
    return MountManager(
        session_factory,
        fake_backend,
        secretbox,
        mount_base,  # type: ignore[arg-type]
        probe_timeout=0.5,
        recover_backoff=(0, 0, 0),
        sleep=lambda _: None,
        notifier=lambda *a: notifications.append(a),
        retry_interval=60,
        clock=clock,
    )


def test_failed_recovery_retries_every_interval_until_nas_is_back(
    retry_manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    session_factory: sessionmaker[Session],
    notifications: list[tuple[str, str, str]],
    clock: Clock,
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    retry_manager.mount(mid)
    mp = retry_manager.mountpoint_for("scans")
    fake_backend.inject_fault(mp, errno.ESTALE)
    fake_backend.fail_next_mount(times=3)  # NAS startet neu
    with pytest.raises(MountUnavailable, match="alle 60 s"):
        retry_manager.check_alive(mid)
    assert not fake_backend.is_mounted(mp)

    mounts_before = sum(1 for c in fake_backend.calls if c.action == "mount")
    clock.now += 30  # zu früh: kein Versuch, Lauf abgebrochen
    with pytest.raises(MountUnavailable, match="in 30 s"):
        retry_manager.check_alive(mid)
    assert sum(1 for c in fake_backend.calls if c.action == "mount") == mounts_before

    clock.now += 30  # NAS noch nicht zurück
    fake_backend.fail_next_mount()
    with pytest.raises(MountUnavailable, match="weiterhin nicht verfügbar"):
        retry_manager.check_alive(mid)

    clock.now += 60  # NAS wieder da
    assert retry_manager.check_alive(mid) == mp
    assert fake_backend.is_mounted(mp)
    assert _state(session_factory, mid).state is MountState.MOUNTED
    assert notifications[-1][0] == "mount.recovered"


def test_no_retry_without_auto_mount(
    retry_manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    clock: Clock,
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount(auto_mount=False)
    retry_manager.mount(mid)
    fake_backend.inject_fault(retry_manager.mountpoint_for("scans"), errno.ESTALE)
    fake_backend.fail_next_mount(times=3)
    with pytest.raises(MountUnavailable):
        retry_manager.check_alive(mid)
    clock.now += 600
    with pytest.raises(MountUnavailable, match="nicht eingehängt"):
        retry_manager.check_alive(mid)


def test_externally_unmounted_is_still_never_remounted(
    retry_manager: MountManager,
    fake_backend: FakeMountBackend,
    make_mount: MountFactory,
    clock: Clock,
) -> None:
    fake_backend.export("nas", "scans")
    mid = make_mount()
    retry_manager.mount(mid)
    fake_backend.vanish(retry_manager.mountpoint_for("scans"))
    clock.now += 600
    with pytest.raises(MountUnavailable, match="nicht eingehängt"):
        retry_manager.check_alive(mid)
    assert [c.action for c in fake_backend.calls] == ["mount"]


def test_hint_text_is_not_redacted() -> None:
    from netmover.mounting.manager import _redact_output

    msg = "mount error(13): Permission denied falsch\n→ Benutzername/Passwort falsch oder …"
    out = _redact_output(msg, ["falsch"])
    assert out.startswith("mount error(13): Permission denied ••••••")
    assert out.endswith("→ Benutzername/Passwort falsch oder …")
