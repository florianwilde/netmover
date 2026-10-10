"""Dateizugriffe mit Zeitlimit (Fallstrick 13).

Ein Zugriff auf einen hängenden ``hard``-NFS-Mount blockiert im Kernel und lässt
sich weder abbrechen noch beenden. Deshalb läuft jeder solche Zugriff in einem
eigenen Daemon-Thread: Läuft das Zeitlimit ab, geben wir auf und lassen den
Thread zurück. Er blockiert nur sich selbst, nie den Scheduler oder andere
Regeln. Bewusst kein Thread-Pool – hängende Threads würden ihn auffüllen.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from netmover.logging_setup import event, get_logger

log = get_logger(__name__)

_abandoned = 0
_abandoned_lock = threading.Lock()


class OperationTimeout(TimeoutError):
    def __init__(self, what: str, seconds: float) -> None:
        super().__init__(f"{what}: keine Antwort nach {seconds:g} s")
        self.what = what
        self.seconds = seconds


def abandoned_threads() -> int:
    """Anzahl zurückgelassener (vermutlich hängender) Threads – für die Statusseite."""
    return _abandoned


def run_with_timeout[T](fn: Callable[[], T], seconds: float, what: str) -> T:
    result: list[T] = []
    error: list[BaseException] = []
    done = threading.Event()

    def target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=target, name=f"nm-timeout:{what}", daemon=True)
    thread.start()
    if not done.wait(seconds):
        global _abandoned
        with _abandoned_lock:
            _abandoned += 1
        event(log, "operation.timeout", level=30, what=what, seconds=seconds)
        raise OperationTimeout(what, seconds)
    if error:
        raise error[0]
    return result[0]


class Cancelled(Exception):
    """Lauf wurde abgebrochen (Zeitlimit oder Dienstende)."""


class CancelToken:
    """Abbruch-Flag plus Lebenszeichen für :func:`run_with_watchdog`.

    Der Worker ruft :meth:`check` regelmäßig auf (pro Datei, pro Datenblock).
    Das meldet Fortschritt und wirft :class:`Cancelled`, sobald abgebrochen wurde.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self.last_beat = time.monotonic()

    def beat(self) -> None:
        self.last_beat = time.monotonic()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled
        self.beat()


def run_with_watchdog[T](
    fn: Callable[[CancelToken], T],
    idle_timeout: float,
    what: str,
    *,
    on_finish: Callable[[], None] | None = None,
    poll: float = 0.1,
) -> T:
    """Wie :func:`run_with_timeout`, aber das Zeitlimit gilt für *Inaktivität*:
    Ein großer Transfer, der stetig Fortschritt meldet, wird nicht abgebrochen,
    ein hängender Dateizugriff schon. ``on_finish`` läuft im Worker-Thread, wenn
    dieser wirklich endet – auch lange nach einer Zeitüberschreitung."""
    token = CancelToken()
    result: list[T] = []
    error: list[BaseException] = []
    done = threading.Event()

    def target() -> None:
        try:
            result.append(fn(token))
        except BaseException as exc:
            error.append(exc)
        finally:
            done.set()
            if on_finish is not None:
                on_finish()

    thread = threading.Thread(target=target, name=f"nm-worker:{what}", daemon=True)
    thread.start()
    while not done.wait(poll):
        if time.monotonic() - token.last_beat > idle_timeout:
            token.cancel()
            global _abandoned
            with _abandoned_lock:
                _abandoned += 1
            event(log, "worker.timeout", level=30, what=what, idle_seconds=idle_timeout)
            raise OperationTimeout(what, idle_timeout)
    if error:
        raise error[0]
    return result[0]
