"""Hilfsfunktionen für Tests der Weboberfläche."""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

PASSWORD = "sehr-geheim-123"


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "kein CSRF-Token in der Seite"
    return m.group(1)


def do_setup(c: TestClient) -> str:
    page = c.get("/setup")
    token = csrf_of(page.text)
    code = c.app.state.setup_code.code  # type: ignore[attr-defined]
    r = c.post(
        "/setup",
        data={"csrf_token": token, "code": code, "password": PASSWORD, "confirm": PASSWORD},
    )
    assert r.status_code == 303, r.text
    return csrf_of(c.get("/").text)


def post(c: TestClient, url: str, data: dict[str, str] | None = None, **kw: Any) -> Any:
    payload = {"csrf_token": c.headers["X-Test-CSRF"], **(data or {})}
    return c.post(url, data=payload, **kw)
