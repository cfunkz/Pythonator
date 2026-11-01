"""The version is written only in .env.example; everything else reads it from there."""

from __future__ import annotations

import re
from pathlib import Path

import pythonator

ROOT = Path(__file__).resolve().parent.parent


def test_version_comes_from_env_example() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^VERSION=(.+)$", text, re.MULTILINE)
    assert match is not None
    assert pythonator.__version__ == match.group(1).strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-.+]?\w+(?:\.\d+)?)?", pythonator.__version__)
