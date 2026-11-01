"""Filesystem locations used by the application."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .safety import make_private, unsafe_reason

APP_NAME = "Pythonator"
CONFIG_NAME = "pythonator.json"
LEGACY_CONFIG_NAME = "bots.json"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory holding the executable (frozen build) or the source checkout."""
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _user_data_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME.lower()


def data_dir() -> Path:
    """Where config, logs and generated files live.

    Resolution order:

    1. ``$PYTHONATOR_HOME`` if set.
    2. *Portable mode*: the application directory, if it already holds a config
       (``pythonator.json``, or a ``bots.json`` from Pythonator 1.x).
    3. The per-user config directory of the platform.
    """
    if env := os.environ.get("PYTHONATOR_HOME"):
        return Path(env).expanduser()
    portable = app_dir()
    if (portable / CONFIG_NAME).is_file() or (portable / LEGACY_CONFIG_NAME).is_file():
        return portable
    return _user_data_dir()


class DataPaths:
    """Concrete paths inside a data directory."""

    __slots__ = ("config", "exposed", "legacy_config", "lock", "logs", "requirements", "root")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / CONFIG_NAME
        self.legacy_config = root / LEGACY_CONFIG_NAME
        self.logs = root / "logs"
        self.requirements = root / "requirements"
        self.lock = root / "pythonator.lock"
        # Set by ensure(): why another local user could have changed the settings, if they could.
        self.exposed: str | None = None

    def ensure(self) -> DataPaths:
        """Create the folders. Logs and package lists are made owner-only: output can hold tokens."""
        if self.exposed is None:  # checked before the permissions below are tightened
            self.exposed = next(filter(None, map(unsafe_reason, (self.root, self.config, self.legacy_config))), None)
        created = not self.root.exists()
        for directory in (self.root, self.logs, self.requirements):
            directory.mkdir(parents=True, exist_ok=True)
        # The root may be a portable folder the user picked: only lock down one we made (or an exposed one).
        for path in (*((self.root,) if created or self.exposed else ()), self.logs, self.requirements, self.config,
                     self.config.with_name(self.config.name + ".bak")):
            if path.exists():
                make_private(path)
        return self
