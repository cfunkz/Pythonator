"""The version is written only in pythonator/__init__.py; everything else reads it from there."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pythonator

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_a_release_version() -> None:
    # The release workflow tags this as v<version>; a dash makes a pre-release.
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?", pythonator.__version__)


def test_pyproject_reads_the_version_from_the_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "pythonator.__version__"}
