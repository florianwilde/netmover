"""Buchführung in der Datenbank.

* ``copied_files`` (Fallstrick 11): Welche Dateiversion (Pfad, Größe, mtime) wurde
  schon übertragen? Verhindert endloses Neu-Kopieren im Kopiermodus – und im
  Verschiebemodus Duplikate, wenn die Quelle nicht gelöscht werden konnte.
* ``file_failures``: Fehlversuche pro Datei als Grundlage der Quarantäne.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from netmover.db import session_scope
from netmover.models import CopiedFile, FileFailure, utcnow

CopyKey = tuple[str, int, int]  # (relpath, size, mtime_ns)


@dataclass(frozen=True)
class FailureInfo:
    attempts: int
    size: int
    mtime_ns: int


class Ledger:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def copied_keys(self, rule_id: int) -> set[CopyKey]:
        with session_scope(self.session_factory) as s:
            rows = s.execute(
                select(CopiedFile.src_relpath, CopiedFile.size, CopiedFile.mtime_ns).where(
                    CopiedFile.rule_id == rule_id
                )
            ).all()
        return {(r[0], r[1], r[2]) for r in rows}

    def record_copy(self, rule_id: int, relpath: str, size: int, mtime_ns: int, dst: str) -> None:
        with session_scope(self.session_factory) as s:
            exists = s.scalar(
                select(CopiedFile.id).where(
                    CopiedFile.rule_id == rule_id,
                    CopiedFile.src_relpath == relpath,
                    CopiedFile.size == size,
                    CopiedFile.mtime_ns == mtime_ns,
                )
            )
            if exists is None:
                s.add(
                    CopiedFile(
                        rule_id=rule_id,
                        src_relpath=relpath,
                        size=size,
                        mtime_ns=mtime_ns,
                        dst_path=dst,
                    )
                )

    def prune_copied(self, rule_id: int, present: set[str]) -> int:
        """Entfernt Einträge, deren Quelldatei verschwunden ist. Nur nach einem
        *vollständigen* Scan aufrufen – sonst würde alles erneut kopiert."""
        with session_scope(self.session_factory) as s:
            rows = s.execute(
                select(CopiedFile.id, CopiedFile.src_relpath).where(CopiedFile.rule_id == rule_id)
            ).all()
            stale = [r[0] for r in rows if r[1] not in present]
            if stale:
                s.execute(delete(CopiedFile).where(CopiedFile.id.in_(stale)))
            return len(stale)

    def failures(self, rule_id: int) -> dict[str, FailureInfo]:
        with session_scope(self.session_factory) as s:
            rows = s.scalars(select(FileFailure).where(FileFailure.rule_id == rule_id)).all()
            return {r.src_relpath: FailureInfo(r.attempts, r.size, r.mtime_ns) for r in rows}

    def record_failure(
        self, rule_id: int, relpath: str, size: int, mtime_ns: int, error: str
    ) -> int:
        with session_scope(self.session_factory) as s:
            row = s.scalar(
                select(FileFailure).where(
                    FileFailure.rule_id == rule_id, FileFailure.src_relpath == relpath
                )
            )
            if row is None:
                row = FileFailure(
                    rule_id=rule_id, src_relpath=relpath, size=size, mtime_ns=mtime_ns, attempts=0
                )
                s.add(row)
            elif (row.size, row.mtime_ns) != (size, mtime_ns):
                # neue Dateiversion – Zählung beginnt von vorn
                row.size, row.mtime_ns, row.attempts = size, mtime_ns, 0
            row.attempts += 1
            row.last_error = error
            row.last_attempt_at = utcnow()
            return row.attempts

    def clear_failure(self, rule_id: int, relpath: str) -> None:
        with session_scope(self.session_factory) as s:
            s.execute(
                delete(FileFailure).where(
                    FileFailure.rule_id == rule_id, FileFailure.src_relpath == relpath
                )
            )
