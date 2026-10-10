"""Echte Implementierung: ruft ``mount``/``umount`` auf.

Fallstrick 5: ``subprocess`` nur mit Argumentliste, nie ``shell=True``.
Fallstrick 7: Zugangsdaten für ``mount.cifs`` nur über eine kurzlebige
Credentials-Datei (0600) unter ``/run/netmover``, nie als ``password=`` in der
Optionszeile – die stünde sonst in der Prozessliste.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from netmover.crypto import redact
from netmover.logging_setup import event, get_logger
from netmover.mounting.backend import CifsCredentials, MountBackend, MountError, MountSpec
from netmover.mounting.mountinfo import MOUNTINFO, find_mount, read_mountinfo

log = get_logger(__name__)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run_process_group(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Wie ``subprocess.run``, aber in eigener Prozessgruppe: Bei Zeitüberschreitung
    wird die ganze Gruppe beendet. ``mount`` startet Hilfsprogramme wie
    ``mount.nfs``; würde nur ``mount`` beendet, liefe ``mount.nfs`` weiter und
    hängte womöglich Minuten später doch noch ein (auf der Test-VM beobachtet)."""
    timeout = kwargs.pop("timeout", None)
    kwargs.pop("check", None)
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with subprocess.Popen(argv, start_new_session=True, **kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


_ENV = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


def explain_mount_error(fstype: str, message: str) -> str:
    """Hängt an typische ``mount``-Meldungen einen Hinweis im Klartext an."""
    low = message.lower()
    hint = ""
    if "helper program" in low or "unknown filesystem type" in low:
        pkg = "cifs-utils" if fstype == "cifs" else "nfs-common"
        hint = f"Das Paket „{pkg}“ fehlt: apt install {pkg}"
    elif "permission denied" in low and fstype == "cifs":
        hint = "Benutzername/Passwort falsch oder der Benutzer hat keine Rechte auf die Freigabe."
    elif "permission denied" in low or "access denied by server" in low:
        hint = (
            "Der NFS-Server erlaubt diesem Host den Zugriff nicht. Synology: Gemeinsamer "
            "Ordner → Bearbeiten → NFS-Berechtigungen → Regel mit der IP dieses Hosts anlegen."
        )
    elif "operation not permitted" in low or "only root can" in low:
        hint = (
            "Keine Berechtigung zum Einhängen. NetMover muss als root mit CAP_SYS_ADMIN laufen "
            "(in unprivilegierten LXC-Containern ist Einhängen nicht möglich)."
        )
    elif "no such file or directory" in low or "mount error(2)" in low:
        hint = "Freigabe bzw. Exportpfad existiert auf dem Server nicht – Schreibweise prüfen."
    elif "host is down" in low or "no route to host" in low or "timed out" in low:
        hint = "Server nicht erreichbar – Adresse, Netzwerk und Firewall prüfen."
    elif "connection refused" in low:
        hint = "Server lehnt die Verbindung ab – ist SMB bzw. NFS am NAS aktiviert?"
    return f"{message}\n→ {hint}" if hint else message


class LinuxMountBackend(MountBackend):
    def __init__(
        self,
        run_dir: Path,
        mountinfo_path: Path = MOUNTINFO,
        runner: Runner = run_process_group,
    ) -> None:
        self.run_dir = run_dir
        self.mountinfo_path = mountinfo_path
        self._runner = runner

    # -- Hilfen ------------------------------------------------------------------

    def _write_credentials(self, creds: CifsCredentials) -> Path:
        self.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.run_dir / f"cred-{uuid.uuid4().hex}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"username={creds.username}\n")
            fh.write(f"password={creds.password}\n")
            if creds.domain:
                fh.write(f"domain={creds.domain}\n")
        return path

    def _run(self, argv: Sequence[str], timeout: float, secrets: Sequence[str]) -> None:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "env": _ENV,
            "check": False,
            "stdin": subprocess.DEVNULL,
        }
        try:
            proc = self._runner(list(argv), **kwargs)
        except subprocess.TimeoutExpired:
            raise MountError(f"{argv[0]}: keine Antwort nach {timeout:g} s") from None
        except FileNotFoundError:
            raise MountError(f"{argv[0]}: Programm nicht gefunden") from None
        if proc.returncode != 0:
            output = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            output = redact(output or f"Exit-Code {proc.returncode}", secrets)
            raise MountError(output)

    # -- Interface -----------------------------------------------------------------

    def mount(self, spec: MountSpec, timeout: float = 60) -> None:
        options = list(spec.options)
        secrets: list[str] = []
        cred_file: Path | None = None
        try:
            if spec.credentials is not None:
                cred_file = self._write_credentials(spec.credentials)
                options.append(f"credentials={cred_file}")
                secrets.append(spec.credentials.password)
            argv = ["mount", "-t", spec.fstype]
            if options:
                argv += ["-o", ",".join(options)]
            argv += [spec.source, str(spec.mountpoint)]
            try:
                self._run(argv, timeout, secrets)
            except MountError as exc:
                raise MountError(explain_mount_error(spec.fstype, str(exc))) from None
        finally:
            if cred_file is not None:
                cred_file.unlink(missing_ok=True)
        event(log, "mount.mounted", source=spec.source, mountpoint=str(spec.mountpoint))

    def unmount(self, mountpoint: Path, lazy: bool = False, timeout: float = 60) -> None:
        argv = ["umount"] + (["-l"] if lazy else []) + [str(mountpoint)]
        self._run(argv, timeout, [])
        event(log, "mount.unmounted", mountpoint=str(mountpoint), lazy=lazy)

    def is_mounted(self, mountpoint: Path) -> bool:
        return find_mount(mountpoint, read_mountinfo(self.mountinfo_path)) is not None
