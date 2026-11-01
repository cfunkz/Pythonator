"""Starting Pythonator when the user logs in, so services marked "Start automatically" come back
after a reboot without anyone opening the app.

* macOS: a LaunchAgent in ``~/Library/LaunchAgents`` (started in the user's GUI session only),
* Linux: an XDG autostart entry in ``~/.config/autostart`` (GNOME, KDE, Xfce, Cinnamon, ...),
* Windows: a value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.

The app is started with ``--minimized``: straight to the tray / menu bar, window hidden.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .paths import APP_NAME, IS_FROZEN, app_dir

LABEL = "io.github.cfunkz.pythonator"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MINIMIZED = "--minimized"


def launch_command() -> list[str]:
    """How to start this same Pythonator (frozen app, or ``python -m pythonator``) at login."""
    if IS_FROZEN:
        argv = [str(Path(sys.executable).resolve())]
    else:
        python = Path(sys.executable)
        if sys.platform == "win32" and (windowed := python.with_name("pythonw.exe")).is_file():
            python = windowed  # no console window at login
        # A source checkout may not be importable from elsewhere: start its app.py then.
        launcher = app_dir() / "app.py"
        argv = [str(python), str(launcher)] if launcher.is_file() else [str(python), "-m", "pythonator"]
    argv.append(MINIMIZED)
    if home := os.environ.get("PYTHONATOR_HOME"):  # keep using the same data folder
        argv += ["--home", str(Path(home).expanduser().resolve())]
    return argv


def _desktop_quote(arg: str) -> str:
    """Quote one argument for the Exec key of a .desktop file (freedesktop.org rules)."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`='):
        return arg
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'


class LoginItem:
    """The login item of one platform. ``home`` and ``key`` are for tests."""

    def __init__(self, platform: str = sys.platform, home: Path | None = None, key: str = RUN_KEY,
                 name: str = APP_NAME) -> None:
        self.platform = platform
        self.home = home or Path.home()
        self.key = key
        self.name = name

    @property
    def supported(self) -> bool:
        return self.platform in ("darwin", "win32") or self.platform.startswith(("linux", "freebsd", "openbsd"))

    @property
    def path(self) -> Path | None:
        """The file that starts Pythonator at login (None on Windows, which uses the registry)."""
        if self.platform == "darwin":
            return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if self.platform == "win32":
            return None
        config = os.environ.get("XDG_CONFIG_HOME") if self.home == Path.home() else None
        return Path(config or self.home / ".config") / "autostart" / f"{self.name.lower()}.desktop"

    def where(self) -> str:
        return f"HKCU\\{self.key}\\{self.name}" if self.platform == "win32" else str(self.path)

    def is_enabled(self) -> bool:
        if self.platform == "win32":
            return self._registry_value() is not None
        path = self.path
        return path is not None and path.is_file()

    def set_enabled(self, enabled: bool, argv: list[str] | None = None) -> None:
        """Turn starting at login on (pointing at ``argv``, default this app) or off. Raises OSError."""
        if not self.supported:
            raise OSError(f"Starting at login isn't supported on {self.platform}")
        argv = argv or launch_command()
        if self.platform == "win32":
            self._set_registry(subprocess.list2cmdline(argv) if enabled else None)
            return
        path = self.path
        assert path is not None
        if not enabled:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._launch_agent(argv) if self.platform == "darwin" else self._desktop_entry(argv).encode()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    # -- formats ----------------------------------------------------------------------------

    @staticmethod
    def _launch_agent(argv: list[str]) -> bytes:
        return plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",  # the desktop session, not ssh logins
        })

    def _desktop_entry(self, argv: list[str]) -> str:
        return "\n".join((
            "[Desktop Entry]",
            "Type=Application",
            f"Name={self.name}",
            "Comment=Supervisor for long-running scripts, web servers and bots",
            "Exec=" + " ".join(_desktop_quote(a) for a in argv),
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "X-GNOME-Autostart-Delay=5",
            "",
        ))

    # -- Windows registry -------------------------------------------------------------------

    def _registry_value(self) -> str | None:
        if sys.platform == "win32":
            import winreg  # noqa: PLC0415

            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as key:
                    value, _ = winreg.QueryValueEx(key, self.name)
            except OSError:
                return None
            return str(value)
        return None

    def _set_registry(self, command: str | None) -> None:
        if sys.platform == "win32":
            import winreg  # noqa: PLC0415

            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as key:
                if command is None:
                    with contextlib.suppress(FileNotFoundError):
                        winreg.DeleteValue(key, self.name)
                else:
                    winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, command)
            return
        raise OSError("The Windows registry is only available on Windows")
