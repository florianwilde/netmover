"""Fallstricke 5 und 7 am echten Backend – ``subprocess`` wird nur abgefangen."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from netmover.mounting.backend import CifsCredentials, MountError, MountSpec
from netmover.mounting.linux import LinuxMountBackend, explain_mount_error


class Recorder:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.cred_snapshot: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        for opt in ",".join(argv).split(","):
            if opt.startswith("credentials="):
                p = Path(opt.removeprefix("credentials="))
                self.cred_snapshot = {
                    "path": p,
                    "mode": stat.S_IMODE(p.stat().st_mode),
                    "content": p.read_text(),
                }
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def _spec(tmp_path: Path, password: str = "S3cr3t,pw=x") -> MountSpec:
    return MountSpec(
        fstype="cifs",
        source="//nas/scans",
        mountpoint=tmp_path / "mnt" / "scans",
        options=("vers=3.0", "uid=1000"),
        credentials=CifsCredentials("scanner", password, "WORKGROUP"),
    )


def test_mount_uses_argv_and_credentials_file(tmp_path: Path) -> None:
    rec = Recorder()
    backend = LinuxMountBackend(tmp_path / "run", runner=rec)
    backend.mount(_spec(tmp_path))

    argv, kwargs = rec.calls[0]
    assert "shell" not in kwargs
    assert argv[:4] == ["mount", "-t", "cifs", "-o"]
    assert argv[-2:] == ["//nas/scans", str(tmp_path / "mnt" / "scans")]
    assert argv[4].startswith("vers=3.0,uid=1000,credentials=")
    # Passwort nie in der Prozessliste
    assert not any("S3cr3t" in a for a in argv)
    assert "password" not in argv[4]
    # Credentials-Datei: 0600, Inhalt korrekt, danach gelöscht
    snap = rec.cred_snapshot
    assert snap["mode"] == 0o600
    assert snap["content"] == "username=scanner\npassword=S3cr3t,pw=x\ndomain=WORKGROUP\n"
    assert snap["path"].parent == tmp_path / "run"
    assert not snap["path"].exists()
    assert kwargs["env"]["LC_ALL"] == "C"


def test_credentials_file_removed_on_failure_and_output_redacted(tmp_path: Path) -> None:
    rec = Recorder(returncode=32, stderr="mount error(13): Permission denied for S3cr3t,pw=x")
    backend = LinuxMountBackend(tmp_path / "run", runner=rec)
    with pytest.raises(MountError) as exc:
        backend.mount(_spec(tmp_path))
    assert "S3cr3t" not in str(exc.value)
    assert "Benutzername/Passwort" in str(exc.value)
    assert list((tmp_path / "run").iterdir()) == []


def test_mount_timeout(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    backend = LinuxMountBackend(tmp_path / "run", runner=runner)
    with pytest.raises(MountError, match="keine Antwort nach 5 s"):
        backend.mount(_spec(tmp_path), timeout=5)
    assert list((tmp_path / "run").iterdir()) == []


def test_unmount_lazy_argv(tmp_path: Path) -> None:
    rec = Recorder()
    backend = LinuxMountBackend(tmp_path / "run", runner=rec)
    backend.unmount(Path("/mnt/netmover/scans"), lazy=True)
    assert rec.calls[0][0] == ["umount", "-l", "/mnt/netmover/scans"]


def test_is_mounted_reads_mountinfo(tmp_path: Path) -> None:
    info = tmp_path / "mountinfo"
    info.write_text("98 22 0:52 / /mnt/netmover/scans rw - cifs //nas/scans rw\n")
    backend = LinuxMountBackend(tmp_path / "run", mountinfo_path=info, runner=Recorder())
    assert backend.is_mounted(Path("/mnt/netmover/scans"))
    assert not backend.is_mounted(Path("/mnt/netmover/other"))


@pytest.mark.parametrize(
    ("fstype", "msg", "expect"),
    [
        (
            "cifs",
            "mount: wrong fs type, bad option; you might need a /sbin/mount.<type> helper program",
            "cifs-utils",
        ),
        ("nfs4", "mount.nfs4: access denied by server while mounting nas:/x", "NFS-Berechtigungen"),
        ("cifs", "mount error(112): Host is down", "nicht erreichbar"),
        ("cifs", "mount: only root can do that", "CAP_SYS_ADMIN"),
    ],
)
def test_explain(fstype: str, msg: str, expect: str) -> None:
    assert expect in explain_mount_error(fstype, msg)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    """mount startet Helfer (mount.nfs) – die dürfen nach dem Zeitlimit nicht weiterlaufen."""
    from netmover.mounting.linux import run_process_group

    pidfile = tmp_path / "helper.pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_process_group(["sh", "-c", script], capture_output=True, text=True, timeout=0.5)
    helper = int(pidfile.read_text())
    for _ in range(20):
        if not _alive(helper):
            break
        import time

        time.sleep(0.05)
    assert not _alive(helper)


def test_process_group_runner_returns_output() -> None:
    from netmover.mounting.linux import run_process_group

    r = run_process_group(
        ["sh", "-c", "echo out; echo err >&2; exit 3"], capture_output=True, text=True, timeout=5
    )
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")
