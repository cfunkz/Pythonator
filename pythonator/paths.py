"""Where Pythonator keeps its files, and how to start its own helper processes."""

from __future__ import annotations

import contextlib
import os
import sys
from functools import cache
from pathlib import Path

import platformdirs

APP = "Pythonator"
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
FROZEN = bool(getattr(sys, "frozen", False))
STARTF_FORCEOFFFEEDBACK = 0x00000080


def quiet_start() -> object:
    """``startupinfo`` for every process Pythonator starts: on Windows, without it each start (a service, its
    setup steps, a stop) turns the mouse pointer into the busy "app starting" one for up to five seconds."""
    if sys.platform == "win32":
        import subprocess

        info = subprocess.STARTUPINFO()
        info.dwFlags |= STARTF_FORCEOFFFEEDBACK
        return info
    return None


@cache
def source_root() -> Path:
    """The folder that holds the ``pythonator`` package (the repository, or the frozen app's bundle)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent.parent


def official_plugins_dir() -> Path:
    """The official plugins: the repository's ``plugins`` folder, bundled with every build."""
    return source_root() / "plugins"


class DataDirs:
    """Pythonator's own folders. Every one is private to the user (0700 on macOS and Linux)."""

    def __init__(self, root: Path | None = None) -> None:
        env = os.environ.get("PYTHONATOR_HOME")
        self.root = (root or (Path(env) if env else Path(platformdirs.user_data_dir(APP, appauthor=False)))).resolve()

    @property
    def services_file(self) -> Path:
        return self.root / "services.json"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def sandboxes(self) -> Path:
        """One private home and temp folder per sandboxed service or plugin."""
        return self.root / "sandbox"

    @property
    def plugins(self) -> Path:
        """Third-party plugins the user installed."""
        return self.root / "plugins"

    @property
    def plugin_data(self) -> Path:
        """Each plugin's own writable folder."""
        return self.root / "plugin-data"

    @property
    def token_file(self) -> Path:
        """The secret a window must present to the background service."""
        return self.root / "daemon.token"

    @property
    def state_file(self) -> Path:
        """Which processes the background service started, to clean up after a crash."""
        return self.root / "running.json"

    def ensure(self) -> None:
        for folder in (self.root, self.logs, self.sandboxes, self.plugins, self.plugin_data):
            folder.mkdir(parents=True, exist_ok=True)
            make_private(folder)


def make_private(path: Path) -> None:
    """Only the user may read or change ``path`` (a no-op on Windows, where the profile folder already is)."""
    if not IS_WINDOWS:
        with contextlib.suppress(OSError):
            path.chmod(0o700 if path.is_dir() else 0o600)


def self_command(*args: str) -> list[str]:
    """The command line that runs Pythonator itself with ``args`` (a helper, the daemon, a plugin worker)."""
    if FROZEN:
        return [sys.executable, *args]
    # -I: isolated mode, so nothing from the current folder or PYTHON* variables is imported. The package is
    # found through -c, which puts the source root on sys.path explicitly.
    boot = f"import sys; sys.path.insert(0, {str(source_root())!r}); from pythonator.__main__ import main; main()"
    return [sys.executable, "-I", "-c", boot, *args]
