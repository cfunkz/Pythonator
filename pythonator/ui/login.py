"""Start Pythonator when the user signs in: a Run value on Windows, a LaunchAgent on macOS, an XDG autostart entry
on Linux. Only ever the user's own entry, never one for all users, so nothing needs administrator rights."""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from ..paths import FROZEN, IS_WINDOWS

NAME = "Pythonator"
HIDDEN = "--hidden"  # started at sign-in: open in the tray (or minimised), not in the user's face
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AGENT = "app.pythonator.login"


def command() -> list[str]:
    if FROZEN:
        return [sys.executable, HIDDEN]
    exe = Path(sys.executable)
    if IS_WINDOWS and (quiet := exe.with_name("pythonw.exe")).is_file():
        exe = quiet  # no console window at sign-in
    return [str(exe), "-m", "pythonator", HIDDEN]


def _desktop_file() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "autostart" / "pythonator.desktop"


def _agent_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT}.plist"


def _desktop_arg(arg: str) -> str:
    """Quoted as the Desktop Entry spec wants: in double quotes, with " ` $ and \\ escaped."""
    for ch in '\\"`$':
        arg = arg.replace(ch, "\\" + ch)
    return f'"{arg}"'.replace("%", "%%")


def enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, NAME)
            return True
        except OSError:
            return False
    return (_agent_file() if sys.platform == "darwin" else _desktop_file()).is_file()


def set_enabled(on: bool) -> None:
    """Raises OSError if the entry can't be written."""
    cmd = command()
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if on:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, NAME)
        return
    path = _agent_file() if sys.platform == "darwin" else _desktop_file()
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        data = plistlib.dumps({"Label": AGENT, "ProgramArguments": cmd, "RunAtLoad": True})
    else:
        data = ("[Desktop Entry]\nType=Application\nName=Pythonator\nComment=Run your scripts, servers and bots\n"
                f"Exec={' '.join(map(_desktop_arg, cmd))}\nX-GNOME-Autostart-enabled=true\n").encode()
    path.write_bytes(data)
