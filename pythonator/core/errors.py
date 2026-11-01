"""Exceptions shared by the core modules."""

from __future__ import annotations


class LaunchError(Exception):
    """The service cannot be started as configured (missing file, interpreter, tool, ...)."""
