#!/usr/bin/env python3
"""GitHub-Release zu einem Tag anlegen oder aktualisieren – Text aus CHANGELOG.md.

    scripts/github_release.py v0.11.0     # ein Tag
    scripts/github_release.py --all       # alle Tags (bestehende Releases werden aktualisiert)

Token (GitHub → Settings → Developer settings → Fine-grained token, nur dieses
Repository, Berechtigung „Contents: Read and write“) aus ``GITHUB_TOKEN`` oder
``~/.config/netmover/github-token`` (Modus 0600). Repository aus ``GITHUB_REPOSITORY``,
sonst aus ``git remote origin``. In GitHub Actions setzt der Workflow beides selbst
(siehe ``.github/workflows/ci.yml``). Nur Standardbibliothek – läuft ohne venv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path.home() / ".config" / "netmover" / "github-token"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
API = "https://api.github.com"
WEB = "https://github.com"


def changelog_section(text: str, version: str) -> str:
    """Abschnitt ``## [version] – datum`` bis zum nächsten ``## [``, ohne Überschrift."""
    m = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
        text,
        re.M | re.S,
    )
    if m is None:
        raise SystemExit(f"CHANGELOG.md enthält keinen Abschnitt [{version}]")
    return m.group(1).strip()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def release_title(tag: str) -> str:
    """Betreff der Tag-Annotation, z. B. „NetMover 0.11.0 – mehrere Ziele je Regel“."""
    return _git("tag", "-l", "--format=%(contents:subject)", tag) or f"NetMover {tag[1:]}"


def remote_repo(origin: str) -> str:
    """``owner/repo`` aus einer GitHub-URL (HTTPS oder SSH)."""
    m = re.match(
        r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+/[^/]+?)(?:\.git)?/?$",
        origin,
    )
    if m is None:
        raise SystemExit(f"Kein GitHub-Repository: {origin!r}")
    return m.group(1)


def repository() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or remote_repo(
        _git("config", "--get", "remote.origin.url")
    )


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN", "").strip()
    if not value and TOKEN_FILE.is_file():
        if TOKEN_FILE.stat().st_mode & 0o077:
            raise SystemExit(f"{TOKEN_FILE} ist für andere lesbar – chmod 600")
        value = TOKEN_FILE.read_text().strip()
    if not value:
        raise SystemExit(f"Kein Token: GITHUB_TOKEN setzen oder {TOKEN_FILE} anlegen")
    return value


def _api(method: str, url: str, tok: str, body: dict[str, object] | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 – https-URL
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read() or b"null")


def publish(tag: str, repo: str, tok: str, changelog: str) -> str:
    payload: dict[str, object] = {
        "tag_name": tag,
        "name": release_title(tag),
        "body": changelog_section(changelog, tag[1:]),
        "draft": False,
        "prerelease": False,
    }
    api = f"{API}/repos/{repo}/releases"
    try:
        existing = _api("GET", f"{api}/tags/{tag}", tok)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        _api("POST", api, tok, payload)
        return "angelegt"
    if not isinstance(existing, dict):
        raise SystemExit(f"Unerwartete Antwort für {tag}: {existing!r}")
    _api("PATCH", f"{api}/{existing['id']}", tok, payload)
    return "aktualisiert"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    tags = (
        [t for t in _git("tag", "-l", "--sort=v:refname").splitlines() if TAG_RE.match(t)]
        if argv == ["--all"]
        else argv
    )
    changelog = (ROOT / "CHANGELOG.md").read_text()
    for tag in tags:  # zuerst alles prüfen, dann veröffentlichen
        if not TAG_RE.match(tag):
            raise SystemExit(f"Kein Versions-Tag: {tag}")
        changelog_section(changelog, tag[1:])
    repo = repository()
    tok = token()
    for tag in tags:
        try:
            what = publish(tag, repo, tok, changelog)
        except urllib.error.HTTPError as exc:
            print(f"{tag}: Fehler {exc.code} {exc.reason}", file=sys.stderr)
            return 1
        print(f"{tag}: Release {what} – {WEB}/{repo}/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
