"""Alembic-Umgebung für NetMover."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from netmover.config import get_settings
from netmover.db import make_migration_engine
from netmover.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().effective_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    engine = make_migration_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            # alles oder nichts: auch SQLite kann DDL in einer Transaktion
            transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            if url.startswith("sqlite"):
                # Fremdschlüssel waren während der Migration aus – vor dem Commit prüfen.
                bad = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise RuntimeError(
                        f"Migration verletzt Fremdschlüssel ({len(bad)} Zeilen), "
                        f"zurückgerollt: {bad[:5]}"
                    )
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
