"""Minimaler SFTP-Server für Tests (paramiko-Serverseite, auf ein Verzeichnis beschränkt).

Unterstützt ``posix-rename@openssh.com`` (paramiko leitet es an ``posix_rename``
weiter), aber bewusst nicht ``statvfs@openssh.com`` – damit ist auch der
Rückfallweg ohne Platzprüfung abgedeckt.
"""

from __future__ import annotations

import contextlib
import io
import os
import socket
import threading
from pathlib import Path
from typing import Any

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_OK

from netmover.crypto import generate_ssh_keypair


class _Server(paramiko.ServerInterface):
    def __init__(self, username: str, password: str | None, pubkey: paramiko.PKey | None) -> None:
        self.username = username
        self.password = password
        self.pubkey = pubkey

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and self.password is not None and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if username == self.username and self.pubkey is not None and key == self.pubkey:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password,publickey"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Handle(SFTPHandle):
    pass


class _RootedSFTP(SFTPServerInterface):
    def __init__(self, server: Any, *args: Any, root: Path, **kwargs: Any) -> None:
        super().__init__(server, *args, **kwargs)
        self.root = root

    def _real(self, path: str) -> str:
        return str(self.root / self.canonicalize(path).lstrip("/"))

    def list_folder(self, path: str) -> Any:
        try:
            real = self._real(path)
            out = []
            for name in os.listdir(real):
                attr = SFTPAttributes.from_stat(os.stat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path: str) -> Any:
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path: str, flags: int, attr: SFTPAttributes) -> Any:
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        f = os.fdopen(fd, mode)
        h = _Handle(flags)
        h.filename = real
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path: str) -> int:
        try:
            os.remove(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath: str, newpath: str) -> int:
        new = self._real(newpath)
        if os.path.exists(new):
            return SFTP_FAILURE  # SFTPv3-rename überschreibt nicht
        try:
            os.rename(self._real(oldpath), new)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def posix_rename(self, oldpath: str, newpath: str) -> int:
        try:
            os.replace(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def mkdir(self, path: str, attr: SFTPAttributes) -> int:
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path: str) -> int:
        try:
            os.rmdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def chattr(self, path: str, attr: SFTPAttributes) -> int:
        try:
            if attr.st_mode is not None:
                os.chmod(self._real(path), attr.st_mode & 0o7777)
            if attr.st_atime is not None and attr.st_mtime is not None:
                os.utime(self._real(path), (attr.st_atime, attr.st_mtime))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK


def _load_key(pem: str) -> paramiko.PKey:
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem))


class StubSFTPServer:
    def __init__(
        self, root: Path, username: str = "paperless", password: str | None = None
    ) -> None:
        self.root = root
        self.username = username
        self.password = password
        host_pem, _ = generate_ssh_keypair("host")
        self.host_key = _load_key(host_pem)
        self.client_private_key, self.client_public_key = generate_ssh_keypair("netmover")
        self.authorized = _load_key(self.client_private_key)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._transports: list[paramiko.Transport] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def host_key_string(self) -> str:
        return f"{self.host_key.get_name()} {self.host_key.get_base64()}"

    def start(self) -> StubSFTPServer:
        self._thread.start()
        return self

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", SFTPServer, _RootedSFTP, root=self.root)
            self._transports.append(t)
            with contextlib.suppress(Exception):
                t.start_server(server=_Server(self.username, self.password, self.authorized))

    def stop(self) -> None:
        self._stop.set()
        for t in self._transports:
            with contextlib.suppress(Exception):
                t.close()
        self._sock.close()
        self._thread.join(timeout=2)
