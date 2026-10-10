"""Start-Konfiguration aus Umgebungsvariablen (Präfix ``NETMOVER_``).

Hier steht nur, was vor dem Öffnen der Datenbank feststehen muss. Alles, was der
Nutzer zur Laufzeit ändert (Aufbewahrung, Webhook, …), liegt in der Tabelle
``settings``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETMOVER_", env_file=None, extra="ignore")

    data_dir: Path = Path("/var/lib/netmover")
    run_dir: Path = Path("/run/netmover")
    mount_base: Path = Path("/mnt/netmover")

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["logfmt", "json"] = "logfmt"

    # "fake" nur für Tests und Entwicklung ohne root
    mount_backend: Literal["linux", "fake"] = "linux"

    database_url: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "netmover.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def effective_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.db_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
