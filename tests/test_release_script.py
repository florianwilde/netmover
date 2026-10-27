"""scripts/github_release.py: Release-Text aus dem Changelog."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "github_release", ROOT / "scripts/github_release.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_changelog_version_has_notes() -> None:
    rel = _script()
    text = (ROOT / "CHANGELOG.md").read_text()
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    assert "0.1.0" in versions
    for v in versions:
        notes = rel.changelog_section(text, v)
        assert notes and "## [" not in notes and not notes.startswith("[")


def test_section_boundaries() -> None:
    rel = _script()
    text = "## [Unreleased]\n\n## [0.2.0] – x\n\n- neu\n\n## [0.1.0] – y\n\n- alt\n\n[0.2.0]: https://e\n"
    assert rel.changelog_section(text, "0.2.0") == "- neu"
    assert rel.changelog_section(text, "0.1.0") == "- alt"
    with pytest.raises(SystemExit):
        rel.changelog_section(text, "9.9.9")


def test_remote_repo_from_origin() -> None:
    rel = _script()
    for url in (
        "https://github.com/florianwilde/netmover",
        "https://github.com/florianwilde/netmover.git",
        "git@github.com:florianwilde/netmover.git",
        "ssh://git@github.com/florianwilde/netmover.git",
    ):
        assert rel.remote_repo(url) == "florianwilde/netmover"
    with pytest.raises(SystemExit):
        rel.remote_repo("https://git.example.org/florian/netmover.git")


def test_tag_message_checks_version_and_changelog() -> None:
    rel = _script()
    init = '__version__ = "0.12.0"\n'
    log = "## [Unreleased]\n\n## [0.12.0] – x\n\n- neu\n"
    assert rel.tag_message("0.12.0", "  Statusseite,\n Quarantäne ", init, log) == (
        "v0.12.0",
        "NetMover 0.12.0 – Statusseite, Quarantäne",
    )
    assert rel.tag_message("0.12.0", "", init, log) == ("v0.12.0", "NetMover 0.12.0")
    for version, init_text, changelog in (
        ("0.12", init, log),  # keine X.Y.Z
        ("0.13.0", init, log),  # Version im Code nicht angehoben
        ("0.12.0", init, "## [Unreleased]\n\n- neu\n"),  # Abschnitt fehlt
    ):
        with pytest.raises(SystemExit):
            rel.tag_message(version, "", init_text, changelog)
