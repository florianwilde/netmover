"""Verdrahtung aller Bausteine – einmal pro Prozess.

Start: Datenbank migrieren → Schlüssel laden → Anbindungen mit ``auto_mount``
wiederherstellen (Ergebnis protokollieren) → erst dann Scheduler starten.
Ende: Scheduler stoppen, Mounts **nicht** aushängen (andere könnten sie nutzen).
"""

from __future__ import annotations

from datetime import datetime

from netmover.config import Settings
from netmover.crypto import SecretBox, load_or_create_key
from netmover.db import make_engine, make_session_factory, upgrade
from netmover.health import Health, check_health
from netmover.logging_setup import event, get_logger
from netmover.models import utcnow
from netmover.mounting.backend import MountBackend
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.linux import LinuxMountBackend
from netmover.mounting.manager import MountManager, RestoreResult
from netmover.mover.runner import RuleRunner
from netmover.notify import Notifier
from netmover.runs import RunService
from netmover.scheduler import RuleScheduler

log = get_logger(__name__)


def make_backend(settings: Settings) -> MountBackend:
    if settings.mount_backend == "fake":
        return FakeMountBackend(settings.data_dir / "fake")
    return LinuxMountBackend(settings.run_dir)


class NetMover:
    def __init__(
        self,
        settings: Settings,
        backend: MountBackend | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        url = settings.effective_database_url
        upgrade(url)
        self.db_engine = make_engine(url)
        self.session_factory = make_session_factory(self.db_engine)
        self.secrets = SecretBox(load_or_create_key(settings.secret_key_path))
        self.backend = backend or make_backend(settings)
        self.notifier = notifier or Notifier(self.session_factory)
        self.mounts = MountManager(
            self.session_factory,
            self.backend,
            self.secrets,
            settings.mount_base,
            notifier=self.notifier.mount_event,
        )
        self.runner = RuleRunner(self.session_factory, self.mounts)
        self.runs = RunService(self.session_factory, self.runner, self.notifier)
        self.scheduler = RuleScheduler(self.session_factory, self.runs)
        self.started_at: datetime = utcnow()
        self.restore_results: list[RestoreResult] = []

    def start(self) -> None:
        """Muss im laufenden Event-Loop aufgerufen werden (AsyncIOScheduler)."""
        self.started_at = utcnow()
        self.restore_results = self.mounts.restore_all()
        ok = sum(r.ok for r in self.restore_results)
        event(log, "startup.mounts_restored", ok=ok, failed=len(self.restore_results) - ok)
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.shutdown()
        self.db_engine.dispose()

    def health(self) -> Health:
        return check_health(
            self.session_factory,
            scheduler_running=self.scheduler.running,
            last_heartbeat=self.scheduler.last_heartbeat,
            service_started=self.started_at,
        )
