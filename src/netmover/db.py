"""Datenbank-Engine, Sessions und Migrationen."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        # Worker-Threads der Regelläufe greifen ebenfalls zu.
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def make_migration_engine(url: str) -> Engine:
    """Verbindung nur für Migrationen.

    * Fremdschlüssel AUS: SQLite baut Tabellen bei Änderungen neu auf (Alembic
      „batch mode“) und löscht dabei die alte Tabelle. Mit eingeschalteter Prüfung
      scheitert das, sobald andere Tabellen darauf verweisen (Praxisbefund beim
      Update auf 0.2.0). Die Integrität prüft ``PRAGMA foreign_key_check`` danach.
    * Echte Transaktion: pysqlite eigene Transaktionssteuerung abnehmen und selbst
      ``BEGIN`` senden. So wird eine abgebrochene Migration vollständig
      zurückgerollt, statt einen halben Zustand zu hinterlassen.
    """
    engine = create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("sqlite"):

        def on_connect(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=OFF")  # außerhalb einer Transaktion setzen
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        def on_begin(conn: Any) -> None:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        event.listen(engine, "connect", on_connect)
        event.listen(engine, "begin", on_begin)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("netmover") / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)
