"""Atomares Umbenennen ohne Überschreiben.

``renameat2(..., RENAME_NOREPLACE)`` ist ein echtes ``rename`` (Watcher sehen
``IN_MOVED_TO`` wie bei ``os.replace``), scheitert aber mit ``EEXIST``, statt
eine vorhandene Datei zu ersetzen. ext4, xfs, btrfs und tmpfs können das; NFS
und CIFS meist nicht – dann liefert :func:`rename_noreplace` ``False`` und der
Aufrufer weicht auf ``link()`` + ``unlink()`` aus.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from collections.abc import Callable
from functools import cache

AT_FDCWD = -100
RENAME_NOREPLACE = 1
_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM})


@cache
def _renameat2() -> Callable[..., int] | None:
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    libc = ctypes.CDLL(name, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    renameat2: Callable[..., int] = fn
    return renameat2


def rename_noreplace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> bool:
    """True = umbenannt. False = vom Dateisystem nicht unterstützt (nichts passiert).
    ``FileExistsError``, wenn ``dst`` existiert; andere Fehler als ``OSError``."""
    fn = _renameat2()
    if fn is None:
        return False
    res = fn(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE)
    if res == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), os.fsdecode(dst))
    if err in _UNSUPPORTED:
        return False
    raise OSError(err, os.strerror(err), os.fsdecode(src))
