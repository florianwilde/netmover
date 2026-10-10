"""Dateizugriffe mit Zeitlimit (Fallstrick 13).

Ein Zugriff auf einen hängenden ``hard``-NFS-Mount blockiert im Kernel und lässt
sich weder abbrechen noch beenden. Deshalb läuft jeder solche Zugriff in einem
eigenen Daemon-Thread: Läuft das Zeitlimit ab, geben wir auf und lassen den
Thread zurück. Er blockiert nur sich selbst, nie den Scheduler oder andere
Regeln. Bewusst kein Thread-Pool – hängende Threads würden ihn auffüllen.
"""

from __future__ import annotations

import threading
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
