"""Windows: a private copy of Node.js for sandboxes, when the installed one is off limits to them.

The Node.js installer puts node.exe in ``C:\\Program Files\\nodejs`` without the entry that lets AppContainers
read it, and only an administrator may add one, so a sandboxed ``npm`` or ``node`` fails with "Access is denied".
Instead of asking for administrator rights, Pythonator copies that folder (node.exe and npm, ~100 MB) once per
Node version into its own data folder, which is yours, and lets the sandbox read and run the copy (never write
it). ``npm`` and ``npx`` run as ``node.exe npm-cli.js``: no cmd.exe batch file in between.

Pure functions plus one copy: :func:`swap` is tested on every OS, :func:`for_sandbox` decides on Windows.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from ..paths import IS_WINDOWS, DataDirs

MAX_BYTES = 1024**3  # a Node folder is ~100 MB; anything this big isn't only Node: don't copy it
DONE = ".pythonator-complete"
SCRIPTS = {"npm": "npm-cli.js", "npx": "npx-cli.js"}
EXE = "node.exe" if IS_WINDOWS else "node"


def node_folder(argv0: str) -> Path | None:
    """The Node install that runs ``argv0`` (node, npm or npx), or None for any other program."""
    prog = Path(argv0)
    stem = prog.stem.lower()
    if stem != "node" and stem not in SCRIPTS:
        return None
    folder = prog.parent
    if (folder / EXE).is_file() and (stem == "node" or (folder / "node_modules" / "npm" / "bin").is_dir()):
        return folder
    return None


def swap(argv: list[str], folder: Path, private: Path) -> list[str]:
    """``argv`` run with the copy in ``private`` instead of the install in ``folder``."""
    stem = Path(argv[0]).stem.lower()
    node = str(private / EXE)
    if stem in SCRIPTS:
        return [node, str(private / "node_modules" / "npm" / "bin" / SCRIPTS[stem]), *argv[1:]]
    return [node, *argv[1:]]


def version_key(folder: Path) -> str:
    """Changes when Node or npm is updated, so a new version gets a new copy."""
    parts = []
    for f in (folder / EXE, folder / "node_modules" / "npm" / "package.json"):
        with contextlib.suppress(OSError):
            st = f.stat()
            parts.append(f"{f.name}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha256("|".join([str(folder.resolve()), *parts]).encode()).hexdigest()[:16]


def _size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).stat().st_size
            if total > MAX_BYTES:
                return total
    return total


_COPYING = threading.Lock()  # the start-up copy and a first start never copy at the same time


def _target(folder: Path, dirs: DataDirs) -> Path:
    return dirs.root / "runtimes" / f"node-{version_key(folder)}"


def private_copy(folder: Path, dirs: DataDirs) -> Path:
    """The copy of ``folder`` for sandboxes, made the first time. Raises OSError (or ValueError: too big)."""
    target = _target(folder, dirs)
    if (target / DONE).is_file():
        return target
    with _COPYING:
        return _copy(folder, target)


def _copy(folder: Path, target: Path) -> Path:
    base = target.parent
    if (target / DONE).is_file():  # made while this one waited
        return target
    if _size(folder) > MAX_BYTES:
        raise ValueError(f"{folder} is over 1 GB: not copying it for sandboxes")
    base.mkdir(parents=True, exist_ok=True)
    partial = base / f"{target.name}.partial-{os.getpid()}-{threading.get_ident()}"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(folder, partial, symlinks=False)
    (partial / DONE).write_text(str(folder), encoding="utf-8")
    try:
        partial.replace(target)
    except OSError:  # another start made it at the same moment
        shutil.rmtree(partial, ignore_errors=True)
        if not (target / DONE).is_file():
            raise
    for old in base.glob("node-*"):  # earlier versions; one still running stays until next time
        if old != target and old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
    return target


def prepare_ahead(dirs: DataDirs) -> None:
    """Windows: make the copy in the background when the background service starts, so a first start doesn't
    wait for it. Never raises."""
    if not IS_WINDOWS:
        return

    def make() -> None:
        from ..core import runtimes
        from . import windows

        with contextlib.suppress(Exception):
            folder = node_folder(found) if (found := runtimes.which("node")) else None
            if folder is not None and windows.blocked_program_folder(folder):
                private_copy(folder, dirs)

    threading.Thread(target=make, daemon=True, name="private-node").start()


def _install(argv: list[str], node_service: bool) -> Path | None:
    folder = node_folder(argv[0])
    if folder is None and node_service:
        from ..core import runtimes

        folder = node_folder(found) if (found := runtimes.which("node")) else None
    return folder


def pending_copy(argv: list[str], dirs: DataDirs, *, node_service: bool = False) -> Callable[[], object] | None:
    """Windows: the copy :func:`for_sandbox` would make first (seconds: ~100 MB), so a caller can make it in the
    background; None when there's none to make."""
    if not IS_WINDOWS:
        return None
    folder = _install(argv, node_service)
    if folder is None or (_target(folder, dirs) / DONE).is_file():
        return None
    from . import windows

    if not windows.blocked_program_folder(folder):
        return None
    return lambda: private_copy(folder, dirs)


def for_sandbox(argv: list[str], env: dict[str, str], dirs: DataDirs, *, node_service: bool = False,
                ) -> tuple[list[str], dict[str, str], Path | None]:
    """On Windows: ``argv`` and ``env`` switched to the private copy when the sandbox can't use the installed
    Node, and the copy's folder (to share read-only); else everything unchanged and None. ``node_service``: a
    Node service's other programs (its console's shell) find the copy first on PATH too."""
    if not IS_WINDOWS:
        return argv, env, None
    folder = _install(argv, node_service)
    from . import windows

    if folder is None or not windows.blocked_program_folder(folder):
        return argv, env, None
    from .base import IsolationError

    try:
        private = private_copy(folder, dirs)
    except (OSError, ValueError) as exc:
        raise IsolationError(f"The sandbox can't run Node.js from {folder}, and copying it for sandboxes failed: "
                             f"{exc}. Run this service in Container mode, or install Node.js for your user "
                             "only (for example with nvm-windows or fnm).") from exc
    path = os.pathsep.join([str(private), *(p for p in env.get("PATH", "").split(os.pathsep)
                                            if p and os.path.normcase(p.rstrip("\\/"))
                                            != os.path.normcase(str(folder)))])
    swapped = swap(argv, folder, private) if node_folder(argv[0]) is not None else argv
    return swapped, {**env, "PATH": path}, private
