"""Pythonator: a desktop supervisor for long-running Python and Node.js scripts, web servers and bots."""

from __future__ import annotations

from pathlib import Path


def _version() -> str:
    """VERSION from .env.example, the one place the version is written.

    It sits next to the package in a source checkout and inside the PyInstaller builds. A pip-installed
    copy has no .env.example, so it uses the version pip recorded when it was built from that file.
    """
    try:
        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "VERSION":
            return value.strip().strip("\"'")
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("pythonator")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _version()
__all__ = ["__version__"]
