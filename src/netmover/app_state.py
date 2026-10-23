"""Verdrahtung aller Bausteine – einmal pro Prozess.

Start: Datenbank migrieren → Schlüssel laden → Anbindungen mit ``auto_mount``
wiederherstellen (Ergebnis protokollieren) → erst dann Scheduler starten.
Ende: Scheduler stoppen, Mounts **nicht** aushängen (andere könnten sie nutzen).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from netmover import kuma
from netmover.backup import STALE_AFTER, BackupError, BackupService, load_state
from netmover.config import Settings
from netmover.crypto import SecretBox, load_or_create_key
from netmover.db import make_engine, make_session_factory, session_scope, upgrade
from netmover.health import Health, backup_check, check_health
from netmover.health import metrics as health_metrics
from netmover.logging_setup import event, get_logger
from netmover.models import Mount, utcnow
from netmover.mounting.backend import MountBackend
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.linux import LinuxMountBackend
from netmover.mounting.manager import MountManager, RestoreResult
from netmover.mover.destinations import Destination
from netmover.mover.runner import RuleRunner
from netmover.notify import Notification, Notifier
from netmover.runs import RunService
from netmover.scheduler import RuleScheduler
from netmover.settings_store import load_settings

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
        self.notifier = notifier or Notifier(self.session_factory, secrets=self.secrets)
        self.mounts = MountManager(
            self.session_factory,
            self.backend,
            self.secrets,
            settings.mount_base,
            notifier=self.notifier.mount_event,
        )
        self.runner = RuleRunner(self.session_factory, self.mounts)
        self.runs = RunService(self.session_factory, self.runner, self.notifier)
        self.backups = BackupService(
            settings,
            self.session_factory,
            self.backup_destination,
            self.secrets,
            notify=lambda kind, title, msg: self.notifier.send(Notification(kind, title, msg)),
        )
        self.scheduler = RuleScheduler(
            self.session_factory,
            self.runs,
            retry_mounts=self.mounts.retry_pending,
            push_health=self.push_health,
            backup_tick=self.backups.tick,
        )
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

    def push_health(self) -> None:
        """Scheduler-Job (jede Minute): Zustand an Uptime Kuma melden, falls eingerichtet."""
        url = load_settings(self.session_factory).kuma_push_url
        if url:
            kuma.push(url, self.health(), metrics=health_metrics(self.session_factory))

    def backup_destination(self, mount_id: int, path: str) -> Callable[[], Destination]:
        """Ziel der Datensicherung – dieselbe Prüfung wie für Regelziele."""
        with session_scope(self.session_factory) as s:
            m = s.get(Mount, mount_id)
            if m is None:
                raise BackupError("Die Anbindung für die Sicherung existiert nicht mehr")
            mtype, name = m.type, m.name
        mp = self.mounts.check_alive(mount_id)
        return self.runner.destination_factory("backup_path", mount_id, mtype, name, path, mp, None)

    def health(self) -> Health:
        cfg = load_settings(self.session_factory)
        health = check_health(
            self.session_factory,
            scheduler_running=self.scheduler.running,
            last_heartbeat=self.scheduler.last_heartbeat,
            service_started=self.started_at,
            maintenance=cfg.maintenance,
        )
        st = load_state(self.session_factory)
        health.add(
            backup_check(
                enabled=cfg.backup_enabled and bool(cfg.backup_mount_id),
                last_success=st.last_success,
                last_error=st.last_error,
                stale_after=STALE_AFTER,
                service_started=self.started_at,
            )
        )
        return health
