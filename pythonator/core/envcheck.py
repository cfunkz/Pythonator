"""Environment checks: on Pythonator itself at start, and on each project before it runs.

Each check returns ``(level, text)`` items, level ``ok``, ``warn`` or ``error``. Nothing here changes anything;
the window shows the items and offers the fix (create the venv, install packages, switch mode).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..paths import FROZEN, IS_LINUX, IS_WINDOWS, DataDirs
from . import runtimes
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
        if rt.key == "python":
            items += _python(folder, prog)
        elif rt.key in ("node", "bun") and (folder / "package.json").is_file() \
                and not (folder / "node_modules").is_dir():
            items.append(("warn", "package.json lists packages but node_modules is missing: Install packages"))
        elif rt.key == "ruby" and (folder / "Gemfile").is_file() and not (folder / "vendor" / "bundle").is_dir():
            items.append(("warn", "Gemfile found; Install packages puts the gems in vendor/bundle"))
        elif rt.key == "php" and (folder / "composer.json").is_file() and not (folder / "vendor").is_dir():
            items.append(("warn", "composer.json found but vendor is missing: Install packages"))
    if service.entry and not (folder / service.entry).exists():
        items.append(("error", f"{service.entry} doesn't exist"))
    items.append(("ok", f"Packages go to {rt.packages or 'the runtime’s own folders'}"))
    return items


def _python(folder: Path, prog: str | None) -> list[Item]:
    venv = runtimes.project_venv(folder)
    wants = (folder / "requirements.txt").is_file() or (folder / "pyproject.toml").is_file()
    if venv is None:
        if wants:
            return [("warn", "No virtual environment: Install packages creates .venv first, so nothing goes into "
                             "your system Python")]
        return [("ok", "No packages needed and no venv: runs on the system Python")]
    home = runtimes.venv_home(venv)
    if not runtimes.venv_python(venv).is_file() or (home is not None and not home.exists()):
        return [("error", f"{venv.name} is broken (its Python is gone): Install packages recreates it")]
    return [("ok", f"Uses its own virtual environment {venv.name}")]
