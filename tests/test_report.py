"""Fallstrick 12: Schreibtest erkennt Root-Squash und leitet Rollen ab."""

from __future__ import annotations

import errno

from netmover.models import MountType
from netmover.mounting.report import CheckReport, Role, assess_write


def _err(code: int) -> OSError:
    return OSError(code, "x")


def test_nfs_root_write_denied_is_root_squash_with_synology_hint() -> None:
    r = assess_write(MountType.NFS4, 0, _err(errno.EACCES), None, None)
    assert not r.write_ok
    assert r.root_squash is True
    assert any("Synology" in h and "Squash" in h for h in r.hints)


def test_nfs_file_owned_by_nobody_is_root_squash() -> None:
    r = assess_write(MountType.NFS3, 0, None, 65534, None)
    assert r.write_ok and r.delete_ok
    assert r.root_squash is True
    assert "65534" in r.message
    assert any("chown" in h for h in r.hints)


def test_nfs_no_root_squash() -> None:
    r = assess_write(MountType.NFS4, 0, None, 0, None)
    assert r.root_squash is False
    assert r.hints == []


def test_cifs_owner_never_counts_as_squash() -> None:
    r = assess_write(MountType.CIFS, 0, None, 1000, None)
    assert r.root_squash is None


def test_not_root_cannot_judge_squash() -> None:
    assert assess_write(MountType.NFS4, 1000, None, 1000, None).root_squash is None


def test_read_only() -> None:
    r = assess_write(MountType.CIFS, 0, _err(errno.EROFS), None, None)
    assert not r.write_ok
    assert "schreibgeschützt" in r.message


def test_delete_failed() -> None:
    r = assess_write(MountType.CIFS, 0, None, 0, _err(errno.EACCES))
    assert r.write_ok and not r.delete_ok


def test_roles() -> None:
    rep = CheckReport(can_read=True)
    assert rep.roles == [Role.SOURCE_COPY]
    rep.can_write = rep.can_delete = True
    assert rep.roles == [Role.SOURCE_MOVE, Role.TARGET]
    assert '"Quelle mit Löschen"' in rep.to_json()
