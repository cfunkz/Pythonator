"""Environment checks: on Pythonator itself at start, and on each project before it runs.

Each check returns ``(level, text)`` items, level ``ok``, ``warn`` or ``error``. Nothing here changes anything;
the window shows the items and offers the fix (create the venv, install packages, switch mode).
"""

from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path

from ..paths import FROZEN, IS_LINUX, IS_WINDOWS, DataDirs
from . import runtimes, versions
from .models import Mode, Service

Item = tuple[str, str]


def is_elevated() -> bool:
    if IS_WINDOWS:
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _since_start() -> str:
    import time

    import psutil

    from .sysinfo import human_duration

    try:
        return human_duration(time.time() - psutil.Process().create_time())
    except (psutil.Error, OSError):
        return "a while"


def app_report(dirs: DataDirs) -> list[Item]:
    """What Pythonator runs on, and anything that weakens it."""
    items: list[Item] = []
    v = sys.version_info
    items.append(("ok", f"Python {v.major}.{v.minor}.{v.micro}" + (" (built-in, frozen app)" if FROZEN else "")))
    if not FROZEN:
        if sys.prefix != sys.base_prefix:
            items.append(("ok", f"Running from a virtual environment: {sys.prefix}"))
        elif os.environ.get("CONDA_PREFIX"):
            items.append(("ok", f"Running from a conda environment: {os.environ['CONDA_PREFIX']}"))
        else:
            items.append(("warn", "Running from the system Python; a virtual environment or the installer keeps "
                                  "Pythonator's packages apart from yours"))
    items.append(("ok", f"Background service: process {os.getpid()}, running for {_since_start()}"))
    items.append(("ok", f"Data folder: {dirs.root}"))
    if is_elevated():
        items.append(("warn", "Running as administrator/root: Full-mode services get those rights too"))
    if IS_LINUX:
        if Path("/.flatpak-info").exists():
            items.append(("warn", "Inside Flatpak: the sandbox can't use bubblewrap here"))
        if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
            items.append(("warn", "Pythonator itself runs inside a container"))
        try:
            if "microsoft" in Path("/proc/version").read_text().lower():
                items.append(("ok", "Running under WSL"))
        except OSError:
            pass
    if not IS_WINDOWS:
        try:
            mode = dirs.root.stat().st_mode & 0o077
            if mode:
                items.append(("warn", f"{dirs.root} can be read by other users; Pythonator makes it private"))
        except OSError:
            pass
    return items


def service_report(service: Service) -> list[Item]:
    """What the project needs before it runs: its runtime, its venv or packages, and its mode."""
    items: list[Item] = []
    folder = Path(service.folder)
    if not folder.is_dir():
        return [("error", f"The folder {folder} doesn't exist")]
    if service.mode is Mode.FULL:
        items.append(("warn", "Full access: it can read and change everything your account can"))
    if service.runtime == runtimes.CUSTOM:
        prog = service.command[0] if service.command else ""
        if prog and not (Path(prog).is_absolute() or runtimes.which(prog)):
            items.append(("error", f"{prog} wasn't found on PATH"))
        return items
    rt = runtimes.RUNTIMES[service.runtime]
    if service.mode is not Mode.CONTAINER:
        found = runtimes.program(rt, folder)
        prog = found or ""
        if not found:
            items.append(("error", f"{rt.label} isn't installed ({' / '.join(rt.programs)} not on PATH)"))
        elif why := _declared_version(rt.key, folder, found):
            items.append(("error", why))
        if rt.key == "python":
            items += _python(folder, prog, own=service.own_env)
        elif service.own_env:
            pass  # the summary line says what's missing and that Start makes it
        elif rt.key in ("node", "bun") and (folder / "package.json").is_file() \
                and not (folder / "node_modules").is_dir():
            items.append(("warn", "package.json lists packages but node_modules is missing: Install packages"))
        elif rt.key == "ruby" and (folder / "Gemfile").is_file() and not (folder / "vendor" / "bundle").is_dir():
            items.append(("warn", "Gemfile found; Install packages puts the gems in vendor/bundle"))
        elif rt.key == "php" and (folder / "composer.json").is_file() and not (folder / "vendor").is_dir():
            items.append(("warn", "composer.json found but vendor is missing: Install packages"))
    if service.entry and not (folder / service.entry).exists():
        items.append(("error", f"{service.entry} doesn't exist"))
    summary = runtimes.env_summary(rt.key, folder, own=service.own_env, container=service.mode is Mode.CONTAINER)
    items.append(("warn" if summary.startswith("Own environment is off") else "ok", summary))
    return items


def port_problems(service: Service) -> list[str]:
    """Before a start: each listed port this computer won't let it listen on, and why, in words. A refused port
    otherwise shows up only as a server nobody can reach, or a framework that exits saying little."""
    if service.mode is Mode.CONTAINER or not service.network.enabled:
        return []
    out = []
    for port in service.network.ports:
        why = _port_refused(port)
        if why == "in use":
            out.append(f"Port {port} is already in use{_owner(port)}. Stop that program, or choose another port "
                       "on the Network tab (and the same one in the program's settings)")
        elif why == "reserved":
            out.append(f"Windows has reserved port {port} for itself: Hyper-V, WSL and Docker reserve ranges of "
                       "ports, and the ranges can change at each restart (netsh interface ipv4 show "
                       "excludedportrange protocol=tcp lists them). Choose another port on the Network tab")
        elif why == "privileged":
            out.append(f"Port {port} is below 1024, which only an administrator may listen on here. "
                       "Choose a port from 1024 up on the Network tab")
    return out


def _port_refused(port: int) -> str:
    """"" when this computer lets a program listen on ``port`` now, else "in use", "reserved" or "privileged"."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if not IS_WINDOWS:  # a port a stopped server used a moment ago is free; SO_REUSEADDR still refuses a listener
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            code = getattr(exc, "winerror", None) or exc.errno
            if code in (errno.EADDRINUSE, 10048):
                return "in use"
            if code in (errno.EACCES, 10013):
                return "reserved" if IS_WINDOWS else "privileged" if port < 1024 else ""
            return ""
    return ""


def _owner(port: int) -> str:
    """ " by <program> (pid N)" when the program listening on ``port`` can be named."""
    import psutil

    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return f" by {psutil.Process(c.pid).name()} (pid {c.pid})"
    except (psutil.Error, OSError):
        pass
    return ""


def _declared_version(runtime: str, folder: Path, prog: str) -> str:
    """The project's own files say which versions of its runtime it needs (package.json engines,
    requires-python, go.mod, rust-version, composer's php): "" when the installed one fits, else what to do."""
    spec, source = versions.declared(runtime, folder)
    if not spec:
        return ""
    return versions.problem(runtime, versions.installed(prog), spec, what=f"This project ({source})")


def _python(folder: Path, prog: str | None, *, own: bool = True) -> list[Item]:
    venv = runtimes.project_venv(folder)
    wants = (folder / "requirements.txt").is_file() or (folder / "pyproject.toml").is_file()
    if own and runtimes.env_missing("python", folder):
        return []  # the summary line says what Start makes
    if venv is None:
        if wants:
            return [("warn", "No virtual environment: Install packages creates .venv first, so nothing goes into "
                             "your system Python")]
        return [("ok", "No packages needed and no venv: runs on the system Python")]
    home = runtimes.venv_home(venv)
    if not runtimes.venv_python(venv).is_file() or (home is not None and not home.exists()):
        return [("error", f"{venv.name} is broken (its Python is gone): Install packages recreates it")]
    return [("ok", f"Uses its own virtual environment {venv.name}")]
