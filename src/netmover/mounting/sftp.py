"""SFTP-Anbindung für Ziele, die nur per SSH erreichbar sind (z. B. ein LXC).

Kein Kernel-Mount – daher kein Hängen im Kernel, aber auch kein ``ismount``.
Der Hostschlüssel wird beim ersten Test festgehalten (Trust on first use) und ist
danach verbindlich; eine Abweichung bricht jede Verbindung ab.

Bricht der Server eine Verbindung schon beim Aufbau ab („Error reading SSH protocol
banner“, „No existing session“ – typisch für sshd ``MaxStartups``, wenn mehrere
Verbindungen gleichzeitig ankommen), versucht :func:`connect` es nach kurzer Pause
erneut, statt die Regel sofort zu pausieren. Abgelehnte Anmeldung, unbekannter oder
geänderter Hostschlüssel und ein nicht erreichbarer Host werden nicht wiederholt.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import posixpath
import socket
import stat
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from types import TracebackType

import paramiko
from paramiko.sftp import CMD_EXTENDED, CMD_EXTENDED_REPLY

from netmover.crypto import redact
from netmover.logging_setup import event, get_logger
from netmover.mounting.report import CheckReport

log = get_logger(__name__)

# Pausen vor dem 2. und 3. Versuch, wenn der Server den Aufbau abbricht
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)


class SftpError(RuntimeError):
    pass


class HostKeyMismatch(SftpError):
    pass


class HostKeyUnknown(SftpError):
    pass


class SftpHandshakeError(SftpError):
    """Server hat die Verbindung beim Aufbau abgebrochen – ein neuer Versuch lohnt."""


@dataclass(frozen=True)
class SftpConfig:
    host: str
    port: int
    username: str
    base_path: str
    password: str | None = None
    private_key: str | None = None
    host_key: str | None = None  # "<typ> <base64>"

    def __repr__(self) -> str:
        return (
            f"SftpConfig(host={self.host!r}, port={self.port}, username={self.username!r}, "
            f"base_path={self.base_path!r})"
        )

    @property
    def secrets(self) -> list[str | None]:
        return [self.password, self.private_key]


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def host_key_string(key: paramiko.PKey) -> str:
    return f"{key.get_name()} {key.get_base64()}"


def load_private_key(pem: str) -> paramiko.PKey:
    errors: list[str] = []
    key_classes: list[type[paramiko.PKey]] = [
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
    ]
    for cls in key_classes:
        try:
            return cls.from_private_key(io.StringIO(pem))
        except (paramiko.SSHException, ValueError) as exc:
            errors.append(f"{cls.__name__}: {exc}")
    raise SftpError("Privater Schlüssel nicht lesbar (Ed25519, ECDSA oder RSA ohne Passphrase)")


class SftpSession:
    def __init__(self, transport: paramiko.Transport, client: paramiko.SFTPClient) -> None:
        self.transport = transport
        self.client = client
        key = transport.get_remote_server_key()
        self.host_key = host_key_string(key)
        self.fingerprint = fingerprint(key)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.client.close()
        with contextlib.suppress(Exception):
            self.transport.close()

    def __enter__(self) -> SftpSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- Erweiterungen von OpenSSH (paramiko bietet sie nicht direkt an) -----------

    def statvfs(self, path: str) -> tuple[int, int] | None:
        """(gesamt, frei für Nutzer) in Bytes über ``statvfs@openssh.com``, sonst None."""
        try:
            t, msg = self.client._request(  # type: ignore[attr-defined]
                CMD_EXTENDED, "statvfs@openssh.com", path
            )
        except (OSError, paramiko.SSHException):
            return None
        if t != CMD_EXTENDED_REPLY or msg is None:
            return None
        _bsize, frsize, blocks, _bfree, bavail = (msg.get_int64() for _ in range(5))
        return blocks * frsize, bavail * frsize

    def fsync(self, handle: paramiko.SFTPFile) -> bool:
        """``fsync@openssh.com`` auf eine offene Datei; False, wenn nicht unterstützt."""
        try:
            self.client._request(  # type: ignore[attr-defined]
                CMD_EXTENDED, "fsync@openssh.com", handle.handle
            )
        except (OSError, paramiko.SSHException):
            return False
        return True


def connect(
    cfg: SftpConfig, timeout: float = 20, allow_unknown_host_key: bool = False
) -> SftpSession:
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        started = time.monotonic()
        try:
            return _connect_once(cfg, timeout, allow_unknown_host_key)
        except SftpHandshakeError as exc:
            # Nur schnelle Abbrüche wiederholen (aktiv abgewiesen). Ein Server, der gar
            # nicht antwortet, würde sonst das Zeitlimit mehrfach ausschöpfen.
            if delay is None or time.monotonic() - started > timeout / 2:
                raise
            event(
                log,
                "sftp.retry",
                level=30,
                host=cfg.host,
                attempt=attempt,
                delay=delay,
                error=str(exc),
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _close(transport: paramiko.Transport, sock: socket.socket) -> None:
    # Transport.close() schließt den Socket nur, wenn der Aufbau schon lief – nach einem
    # sehr frühen Abbruch bliebe er sonst offen.
    transport.close()
    with contextlib.suppress(OSError):
        sock.close()


def _connect_once(cfg: SftpConfig, timeout: float, allow_unknown_host_key: bool) -> SftpSession:
    try:
        sock = socket.create_connection((cfg.host, cfg.port), timeout=timeout)
    except OSError as exc:
        raise SftpError(f"Verbindung zu {cfg.host}:{cfg.port} fehlgeschlagen: {exc}") from None
    transport = paramiko.Transport(sock)
    transport.banner_timeout = timeout
    transport.auth_timeout = timeout
    try:
        transport.start_client(timeout=timeout)
        server_key = transport.get_remote_server_key()
        seen = host_key_string(server_key)
        if cfg.host_key is None:
            if not allow_unknown_host_key:
                raise HostKeyUnknown(
                    "Hostschlüssel noch nicht bestätigt – bitte die Anbindung zuerst testen."
                )
        elif cfg.host_key.split()[:2] != seen.split()[:2]:
            raise HostKeyMismatch(
                f"Hostschlüssel von {cfg.host} hat sich geändert "
                f"(jetzt {fingerprint(server_key)}). "
                "Wurde der Server neu aufgesetzt? Dann Hostschlüssel in der Anbindung "
                "zurücksetzen und neu testen – sonst nicht verbinden!"
            )
        if cfg.private_key:
            transport.auth_publickey(cfg.username, load_private_key(cfg.private_key))
        elif cfg.password is not None:
            transport.auth_password(cfg.username, cfg.password)
        else:
            raise SftpError("Keine Zugangsdaten hinterlegt")
        client = paramiko.SFTPClient.from_transport(transport)
        if client is None:
            raise SftpError("SFTP-Subsystem nicht verfügbar")
        client.get_channel().settimeout(timeout)  # type: ignore[union-attr]
        return SftpSession(transport, client)
    except paramiko.AuthenticationException:
        _close(transport, sock)
        raise SftpError(
            "Anmeldung abgelehnt – Benutzername prüfen und den öffentlichen Schlüssel in "
            "~/.ssh/authorized_keys des Zielbenutzers eintragen."
        ) from None
    except (paramiko.SSHException, OSError, EOFError) as exc:
        _close(transport, sock)
        raise SftpHandshakeError(redact(f"SSH-Fehler: {exc}", cfg.secrets)) from None
    except SftpError:
        _close(transport, sock)
        raise


@contextlib.contextmanager
def session(cfg: SftpConfig, timeout: float = 20) -> Iterator[SftpSession]:
    s = connect(cfg, timeout=timeout)
    try:
        yield s
    finally:
        s.close()


def check_connection(cfg: SftpConfig, timeout: float = 20) -> CheckReport:
    """Verbinden, Basisordner lesen, Testdatei schreiben/umbenennen/löschen."""
    report = CheckReport()
    try:
        sess = connect(cfg, timeout=timeout, allow_unknown_host_key=True)
    except SftpError as exc:
        report.step("connect", False, str(exc))
        report.step("read", None)
        report.step("write", None)
        return report
    with sess:
        report.host_key = sess.host_key
        report.host_key_fingerprint = sess.fingerprint
        new = " (neu – bitte Fingerabdruck prüfen)" if cfg.host_key is None else ""
        report.step("connect", True, f"Hostschlüssel {sess.fingerprint}{new}")

        try:
            attrs = sess.client.stat(cfg.base_path)
            if attrs.st_mode is None or not stat.S_ISDIR(attrs.st_mode):
                raise SftpError(f"{cfg.base_path} ist kein Verzeichnis")
            count = len(sess.client.listdir(cfg.base_path))
            report.can_read = True
            report.step("read", True, f"{count} Einträge")
        except (OSError, SftpError) as exc:
            report.step("read", False, f"{cfg.base_path}: {exc}")
            report.step("write", None)
            return report

        token = uuid.uuid4().hex
        part = posixpath.join(cfg.base_path, f".netmover-test-{token}.part")
        final = posixpath.join(cfg.base_path, f".netmover-test-{token}.tmp")
        try:
            with sess.client.open(part, "wb") as fh:
                fh.write(b"netmover")
                fh.flush()
                synced = sess.fsync(fh)
            sess.client.posix_rename(part, final)
            st = sess.client.stat(final)
            report.owner = f"{st.st_uid}:{st.st_gid}"
            report.can_write = True
        except OSError as exc:
            report.step("write", False, f"Schreiben fehlgeschlagen: {exc}")
            with contextlib.suppress(OSError):
                sess.client.remove(part)
            return report
        try:
            sess.client.remove(final)
            report.can_delete = True
            report.step(
                "write",
                True,
                "Datei angelegt, atomar umbenannt und gelöscht"
                + ("" if synced else " (Server unterstützt kein fsync – Hinweis)"),
            )
        except OSError as exc:
            report.step("write", True, f"Angelegt, aber Löschen fehlgeschlagen: {exc}")
        usage = sess.statvfs(cfg.base_path)
        if usage:
            report.total_bytes, report.free_bytes = usage
        else:
            report.hints.append(
                "Server meldet keinen freien Speicher (statvfs) – Platzprüfung wird übersprungen."
            )
    return report
