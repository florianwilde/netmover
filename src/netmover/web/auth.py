"""Anmeldung, Einrichtung beim ersten Start, CSRF und Sperre bei Fehlversuchen.

* Ein einziges Administratorkonto; Passwort-Hash (Argon2) in ``settings``.
* Erster Start: Ohne Passwort führt jede Seite zur Einrichtung. Dafür wird ein
  einmaliger Einrichtungscode ins Log geschrieben – so kann nicht irgendwer im
  LAN, der die Seite zuerst öffnet, das Passwort festlegen.
* Session-Cookie ``HttpOnly``, ``SameSite=Lax`` (Starlette-SessionMiddleware,
  signiert). CSRF-Token pro Session, Pflicht bei jedem POST – als Formularfeld
  ``csrf_token`` oder Header ``X-CSRF-Token`` (HTMX).
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from netmover.settings_store import get_value, set_value

PASSWORD_KEY = "auth.admin_password_hash"  # noqa: S105 – Schlüsselname
MIN_PASSWORD_LENGTH = 10
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_hasher = PasswordHasher()


AUTH_ALWAYS, AUTH_TRUSTED, AUTH_OFF = "always", "trusted", "off"
AUTH_MODES = (AUTH_ALWAYS, AUTH_TRUSTED, AUTH_OFF)


def parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """„10.0.10.0/24, 192.168.1.5“ → Netze; ``ValueError`` mit verständlicher Meldung."""
    nets = []
    for part in re.split(r"[,;\s]+", raw.strip()):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(f"„{part}“ ist kein gültiges Netz (z. B. 10.0.10.0/24)") from None
    return nets


ACCESS_NOPW, ACCESS_PW, ACCESS_DENY = "nopw", "pw", "deny"


def _address(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _networks_or_empty(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    try:
        return parse_networks(raw)
    except ValueError:
        return []  # gespeicherte Werte sind beim Speichern geprüft


def access_decision(host: str | None, mode: str, trusted: str, allowed: str) -> str:
    """Wie darf diese Adresse auf die Oberfläche zugreifen?

    1. aus ``trusted`` (oder Modus „off“): **ohne Passwort**,
    2. ``allowed`` leer oder Adresse darin: **mit Passwort**,
    3. sonst: **abgewiesen**.

    Hinter einem Reverse Proxy ist die Client-Adresse nur dann echt, wenn
    ``NETMOVER_FORWARDED_ALLOW_IPS`` den Proxy kennt – sonst sähe jede Anfrage aus,
    als käme sie vom Proxy selbst."""
    if mode == AUTH_OFF:
        return ACCESS_NOPW
    addr = _address(host)
    if addr is not None and any(addr in n for n in _networks_or_empty(trusted)):
        return ACCESS_NOPW
    allowed_nets = _networks_or_empty(allowed)
    if allowed_nets and (addr is None or not any(addr in n for n in allowed_nets)):
        return ACCESS_DENY
    return ACCESS_PW


def client_host(request: Request) -> str | None:
    return request.client.host if request.client else None


def login_bypass(request: Request, mode: str, trusted: str, allowed: str = "") -> str | None:
    """Grund, warum keine Anmeldung nötig ist (``"off"``/``"trusted"``), sonst ``None``."""
    if access_decision(client_host(request), mode, trusted, allowed) != ACCESS_NOPW:
        return None
    return AUTH_OFF if mode == AUTH_OFF or _covers_everything(trusted) else AUTH_TRUSTED


def _covers_everything(raw: str) -> bool:
    return any(n.prefixlen == 0 for n in _networks_or_empty(raw))


def includes_loopback(raw: str) -> bool:
    try:
        nets = parse_networks(raw)
    except ValueError:
        return False
    return any(
        ipaddress.ip_address("127.0.0.1") in n or ipaddress.ip_address("::1") in n for n in nets
    )


class LoginRequired(Exception):
    def __init__(self, setup: bool = False) -> None:
        self.setup = setup


def load_or_create_session_key(path: Path) -> str:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return key


def password_is_set(factory: sessionmaker[Session]) -> bool:
    return bool(get_value(factory, PASSWORD_KEY))


def set_password(factory: sessionmaker[Session], password: str) -> None:
    set_value(factory, PASSWORD_KEY, _hasher.hash(password))


def verify_password(factory: sessionmaker[Session], password: str) -> bool:
    stored = get_value(factory, PASSWORD_KEY)
    if not stored:
        return False
    try:
        _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False
    if _hasher.check_needs_rehash(stored):
        set_value(factory, PASSWORD_KEY, _hasher.hash(password))
    return True


def password_problem(password: str, confirm: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Mindestens {MIN_PASSWORD_LENGTH} Zeichen."
    if password != confirm:
        return "Die Passwörter stimmen nicht überein."
    return None


# -- Session / CSRF -------------------------------------------------------------------


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def login_session(request: Request) -> None:
    """Neue Session nach erfolgreicher Anmeldung (Schutz vor Session-Fixation)."""
    request.session.clear()
    request.session["user"] = "admin"
    request.session["csrf"] = secrets.token_urlsafe(32)
    request.session["since"] = int(time.time())


def is_logged_in(request: Request) -> bool:
    return request.session.get("user") == "admin"


async def check_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not expected or not sent or not hmac.compare_digest(str(expected), sent):
        raise HTTPException(status_code=403, detail="CSRF-Token fehlt oder ist ungültig.")


# -- Sperre bei Fehlversuchen --------------------------------------------------------------


class LoginThrottle:
    """Nach ``limit`` Fehlversuchen innerhalb von ``window`` Sekunden wird die
    Client-IP für ``lockout`` Sekunden gesperrt."""

    def __init__(self, limit: int = 5, window: float = 900, lockout: float = 300) -> None:
        self.limit = limit
        self.window = window
        self.lockout = lockout
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def locked_for(self, client: str) -> float:
        with self._lock:
            until = self._locked.get(client, 0)
            return max(0.0, until - time.monotonic())

    def failure(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._fails[client]
            q.append(now)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                self._locked[client] = now + self.lockout
                q.clear()

    def success(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)
            self._locked.pop(client, None)


class SetupCode:
    """Einmaliger Code für die Ersteinrichtung (steht im Log)."""

    def __init__(self) -> None:
        self.code = "-".join(secrets.token_hex(2) for _ in range(3))

    def matches(self, value: str) -> bool:
        return hmac.compare_digest(self.code, value.strip().lower())
