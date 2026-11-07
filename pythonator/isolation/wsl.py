"""WSL mode (Windows): the service runs in a WSL 2 Linux distribution, started through Microsoft's ``wsl.exe``.

Not a sandbox. The service runs as your Linux user, who can read and change your Windows files under ``/mnt``, so
it needs the same one-time confirmation as Full access. What it adds is Linux itself: a project that needs Linux
(a bot using uvloop, a server built for Linux, shell scripts) runs as it would on a Linux server, with the
distribution's own runtimes and packages. For isolation on Linux, use Container mode; its "wsl" engine runs the
container with Docker Engine installed inside WSL.

Only ``wsl.exe``'s documented options are used: ``--distribution``, ``--cd`` and ``--exec``. Values of variables
never appear on a command line: they go through wsl.exe's own environment and are named in ``WSLENV``, WSL's way to
share variables with Linux. Every process of a run carries a mark (``PYTHONATOR_WSL_RUN``), and Stop ends the
processes with it inside Linux, also ones that started a session of their own: ending wsl.exe on Windows isn't
documented to end what it started.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core.models import Mode, Service
from ..paths import IS_WINDOWS, quiet_start
from .base import Command, IsolationError, WslRun, env_file_values

MARK = "PYTHONATOR_WSL_RUN"
VENV = ".venv-wsl"  # the project's venv made by WSL's Python (a Windows .venv can't run in Linux)
PYTHON = "python3"  # Ubuntu and Debian have no "python" unless python-is-python3 is installed
TIMEOUT = 60.0  # a stopped distribution boots first: a few seconds, longer on a cold start
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
# What wsl.exe needs from Windows. Nothing else of yours crosses into Linux unless the service sets it.
_WINDOWS_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "USERPROFILE", "USERNAME", "LOCALAPPDATA",
                "APPDATA", "TEMP", "TMP", "COMSPEC", "PROGRAMDATA")
# Runs in Linux: the folder as Linux sees it, and the Linux user's ids. No double quotes, so how the Windows
# command line is quoted can't change it; IFS and set -f keep a folder with spaces or brackets one argument.
_LOCATE = "set -f; IFS=; wslpath -a -u $1 && id -u && id -g"
# Runs in Linux: signals each process whose environment has the run's mark. ``$1`` is hex, ``$2`` TERM or KILL.
_SIGNAL = ("for f in $(grep -lszxF PYTHONATOR_WSL_RUN=$1 /proc/[0-9]*/environ); do p=${f%/environ}; "
           "kill -$2 ${p#/proc/} 2>/dev/null; done; true")
# Runs in Linux: a login shell (so ~/.profile's PATH, with ~/.local/bin, cargo, go), then the service as is.
_LOGIN = 'exec "$0" "$@"'


@dataclass(frozen=True, slots=True)
class Place:
    """A Windows folder as a WSL distribution sees it, and who runs there."""

    path: str  # e.g. /mnt/c/Users/me/bot
    uid: int
    gid: int


def program() -> str:
    """``wsl.exe`` by its full path in System32: a bare name is looked for in the current folder first."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    if IS_WINDOWS:
        from .windows import system32

        return str(system32() / "wsl.exe")
    return str(Path(root) / "System32" / "wsl.exe")


def available() -> bool:
    return IS_WINDOWS and Path(program()).is_file()


def prefix(distro: str) -> list[str]:
    """``wsl.exe`` for ``distro`` ("" for WSL's default distribution)."""
    return [program(), *(["--distribution", distro] if distro else [])]


def cli_env(passed: dict[str, str] | None = None) -> dict[str, str]:
    """wsl.exe's environment: what it needs from Windows, readable messages, and ``passed`` for Linux, named in
    WSLENV (``/u``: only from Windows into Linux, not translated)."""
    env = {k: v for k in _WINDOWS_ENV if (v := os.environ.get(k))}
    env["WSL_UTF8"] = "1"
    if passed:
        env.update(passed)
        env["WSLENV"] = ":".join(f"{k}/u" for k in passed)
    return env


def decode(data: bytes) -> str:
    """wsl.exe's own messages: UTF-16 on WSL versions that ignore WSL_UTF8, UTF-8 otherwise."""
    if b"\x00" in data:
        with contextlib.suppress(UnicodeDecodeError):
            return data.decode("utf-16-le").lstrip("\ufeff")
        data = data.replace(b"\x00", b"")
    return data.decode("utf-8", "replace")


def _run(argv: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                          env=cli_env(), startupinfo=quiet_start(),
                          creationflags=NO_WINDOW if sys.platform == "win32" else 0)


def distros() -> list[str]:
    """The installed distributions as ``wsl --list`` names them; [] when WSL isn't installed or has none. Doesn't
    start WSL's virtual machine."""
    from ..core import validate

    if not available():
        return []
    try:
        done = _run([program(), "--list", "--quiet"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    out = []
    for line in decode(done.stdout).splitlines():
        with contextlib.suppress(ValueError):
            if name := validate.wsl_distro(line):
                out.append(name)
    return out


def _where(distro: str) -> str:
    return f"WSL ({distro})" if distro else "WSL's default distribution"


_places: dict[tuple[str, str], tuple[float, Place]] = {}
PLACE_TTL = 300.0  # seconds a folder's place is remembered: the next start or install step doesn't ask again


def known(distro: str, folder: Path) -> bool:
    """Whether :func:`locate` would answer at once, without asking WSL (which may have to start first)."""
    cached = _places.get((distro, str(folder)))
    return cached is not None and time.monotonic() - cached[0] < PLACE_TTL


def locate(distro: str, folder: Path) -> Place:
    """Where ``folder`` is inside the distribution, and the Linux user's ids. Raises IsolationError."""
    key = (distro, str(folder))
    if known(distro, folder):
        return _places[key][1]
    place = _ask_place(distro, folder)
    _places[key] = (time.monotonic(), place)
    return place


def _ask_place(distro: str, folder: Path) -> Place:
    if not available():
        raise IsolationError("WSL isn't installed. Install it once with `wsl --install` in an administrator "
                             "PowerShell, or switch this service to another mode.")
    try:
        done = _run([*prefix(distro), "--exec", "/bin/sh", "-c", _LOCATE, "pythonator", str(folder)])
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolationError(f"{_where(distro)} didn't answer: {exc}") from None
    lines = decode(done.stdout).strip().splitlines()
    if done.returncode != 0 or len(lines) != 3 or not lines[0].startswith("/"):
        why = decode(done.stderr).strip() or decode(done.stdout).strip() or f"exit code {done.returncode}"
        raise IsolationError(f"{_where(distro)} can't open {folder}: {why[:300]}")
    try:
        return Place(lines[0], int(lines[1]), int(lines[2]))
    except ValueError:
        raise IsolationError(f"{_where(distro)} gave an unexpected answer: {lines!r}") from None


def install_steps(service: Service) -> list[list[str]]:
    """Its packages, installed by WSL's own runtime into the project: Python gets ``.venv-wsl``."""
    from . import container

    return container.linux_install_steps(service, base=".", venv=VENV, python=PYTHON)


def wrap(service: Service, *, task: list[str] | None = None) -> Command:
    """The command that runs ``service`` (or a ``task``, such as an install step) in WSL. Raises IsolationError."""
    from . import container

    if service.mode is not Mode.WSL:
        raise IsolationError("not a WSL service")
    if not IS_WINDOWS:
        raise IsolationError("WSL mode is for Windows. Use Sandbox or Container mode here.")
    net = service.network
    if task is None and (service.memory_mb or service.cpu_percent):
        raise IsolationError("WSL mode can't hold one service to RAM or CPU limits (.wslconfig limits all of WSL). "
                             "Clear them on the Limits tab, or use Container mode with the wsl engine.")
    if task is None and not net.enabled:
        raise IsolationError("WSL mode can't turn a service's network off. Use Sandbox or Container mode for that.")
    distro = service.wsl.distro
    folder = Path(service.folder)
    place = locate(distro, folder)
    inner = container.linux_argv(service, task, base=place.path, venv=VENV, python=PYTHON)
    token = secrets.token_hex(8)
    passed = {"PYTHONUNBUFFERED": "1", "FORCE_COLOR": "1"}
    passed.update(env_file_values(service))
    passed.update(service.env)
    if net.ports:
        passed.setdefault("PORT", str(net.ports[0]))
        passed.setdefault("HOST", "0.0.0.0" if net.expose == "lan" else "127.0.0.1")  # noqa: S104 (LAN, when asked)
    passed[MARK] = token
    argv = [*prefix(distro), "--cd", place.path, "--exec", "/bin/sh", "-lc", _LOGIN, *inner]
    where = "this computer" if net.expose == "local" else "other computers too (with WSL's mirrored networking)"
    ports = f", serves {', '.join(map(str, net.ports))} for {where}" if net.ports and task is None else ""
    describe = (f"WSL ({distro or 'default distribution'}): runs as your Linux user and can change your Windows "
                f"files under /mnt{ports}")
    # wsl.exe itself starts in System32: a folder inside WSL (\\wsl.localhost\…) can't be a Windows current folder.
    cwd = folder if not str(folder).startswith("\\\\") else Path(program()).parent
    return Command(argv, cwd, cli_env(passed), describe=describe, shown=inner, wsl=WslRun(distro, token))


def signal(run: WslRun, sig: Literal["TERM", "KILL"], timeout: float = TIMEOUT) -> None:
    """Send ``sig`` to every process of ``run`` inside WSL. Best effort: a run that already ended has none."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _run([*prefix(run.distro), "--exec", "/bin/sh", "-c", _SIGNAL, "pythonator", run.token, sig], timeout)
