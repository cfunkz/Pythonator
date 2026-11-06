"""Windows: a private copy of Node.js for sandboxes, when the installed one is off limits to them.

The Node.js installer puts node.exe in ``C:\\Program Files\\nodejs`` without the entry that lets AppContainers
read it, and only an administrator may add one, so a sandboxed ``npm`` or ``node`` fails with "Access is denied".
Instead of asking for administrator rights, Pythonator copies that folder (node.exe and npm, ~100 MB) once per
Node version into its own data folder, which is yours, and lets the sandbox read and run the copy (never write
it). ``npm`` and ``npx`` run as ``node.exe npm-cli.js``: no cmd.exe batch file in between.

The copy also fixes the names of the pipes Node.js makes for the programs it starts (see :data:`PIPE_NAME`), so
it's used for every Node.js that has the old names, even one the sandbox could read where it is installed.

Pure functions plus one copy: :func:`swap` is tested on every OS, :func:`for_sandbox` decides on Windows.
"""

from __future__ import annotations

import contextlib
import hashlib
import mmap
import os
import shutil
import threading
from collections.abc import Callable
from functools import cache
from pathlib import Path

from ..paths import IS_WINDOWS, DataDirs

MAX_BYTES = 1024**3  # a Node folder is ~100 MB; anything this big isn't only Node: don't copy it
DONE = ".pythonator-complete"
SCRIPTS = {"npm": "npm-cli.js", "npx": "npx-cli.js"}
EXE = "node.exe" if IS_WINDOWS else "node"

# libuv, Node.js's input and output library, names the pipes it makes for a child process's stdin, stdout and
# stderr \\?\pipe\uv\<number>-<pid> (up to libuv 1.52, which every Node.js release has as of November 2026).
# Windows lets an AppContainer make pipes only under \\?\pipe\LOCAL\, and libuv takes the "access denied" for
# a name that is in use and tries the next number, forever: node.exe uses a whole core, prints nothing and never
# goes on, as soon as the app starts any program. Vite does so at start (``net use``, to look for network
# drives), Next.js and Angular start workers, npm runs install scripts. libuv 1.53 adds LOCAL\ itself in an
# AppContainer; until Node.js ships it, the copy's node.exe gets that name. It must keep its length, so the pid
# goes: the number alone is almost always unique, and on a clash libuv tries the next number, as it does now.
PIPE_NAME = b"\\\\?\\pipe\\uv\\%llu-%lu\x00"
LOCAL_PIPE_NAME = b"\\\\?\\pipe\\LOCAL\\%llu\x00\x00"
COPY_FORMAT = "pipes-in-LOCAL"  # part of each copy's name: a copy made before the fix is made again


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
    return hashlib.sha256("|".join([COPY_FORMAT, str(folder.resolve()), *parts]).encode()).hexdigest()[:16]


def pipes_hang(exe: Path) -> bool:
    """Whether ``exe`` (node.exe) names its pipes as no AppContainer may: see :data:`PIPE_NAME`."""
    try:
        st = exe.stat()
    except OSError:
        return False
    return _has_pipe_name(str(exe), st.st_size, st.st_mtime_ns)


@cache
def _has_pipe_name(exe: str, _size: int, _mtime: int) -> bool:
    with contextlib.suppress(OSError, ValueError), Path(exe).open("rb") as f, \
            mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        return m.find(PIPE_NAME) != -1
    return False


def fix_pipe_names(exe: Path) -> bool:
    """Give the copy's node.exe pipe names an AppContainer may use (:data:`PIPE_NAME`), and take off its
    signature, which no longer matches: Windows then sees a program without one, as it does every program you
    build yourself. False when there was nothing to change."""
    data = bytearray(exe.read_bytes())
    if PIPE_NAME not in data:
        return False
    data = bytearray(data.replace(PIPE_NAME, LOCAL_PIPE_NAME))
    drop_signature(data)
    exe.write_bytes(data)
    return True


def drop_signature(exe: bytearray) -> None:
    """Remove the Authenticode signature of the Windows program in ``exe``, in place: the security entry of the
    PE header, and the certificates at the end of the file."""
    if exe[:2] != b"MZ" or len(exe) < 0x40:
        return
    pe = int.from_bytes(exe[0x3C:0x40], "little")
    if exe[pe:pe + 4] != b"PE\0\0":
        return
    optional = pe + 24
    magic = int.from_bytes(exe[optional:optional + 2], "little")
    entry = optional + (112 if magic == 0x20B else 96) + 4 * 8  # data directory 4: the certificate table
    if entry + 8 > len(exe):
        return
    start = int.from_bytes(exe[entry:entry + 4], "little")
    size = int.from_bytes(exe[entry + 4:entry + 8], "little")
    if not start or not size:
        return
    exe[entry:entry + 8] = bytes(8)
    if len(exe) - 8 <= start + size <= len(exe):  # at the very end, as always (padded to 8 bytes)
        del exe[start:]


def needs_copy(folder: Path) -> bool:
    """Whether sandboxes run the private copy instead of the Node.js in ``folder``: it is off limits to them,
    or its pipe names hang in one."""
    from . import windows

    return windows.blocked_program_folder(folder) or pipes_hang(folder / EXE)


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
    fix_pipe_names(partial / EXE)
    (partial / DONE).write_text(str(folder), encoding="utf-8")
    if target.exists():
        _remove(target)  # half of one, from a removal that stopped at a file in use
    try:
        partial.replace(target)
    except OSError:  # another start made it at the same moment
        shutil.rmtree(partial, ignore_errors=True)
        if not (target / DONE).is_file():
            raise
    for old in base.glob("node-*"):  # earlier versions
        if old != target and old.is_dir() and ".partial-" not in old.name:
            _remove(old)
    return target


def _remove(folder: Path) -> None:
    """Delete a copy unless a service still runs from it: Windows won't rename a folder with a running program
    in it, so it stays whole (and usable) until next time instead of losing every file but the busy ones."""
    trash = folder.with_name(f"{folder.name}.partial-old-{os.getpid()}")
    try:
        folder.rename(trash)
    except OSError:
        return
    shutil.rmtree(trash, ignore_errors=True)


def prepare_ahead(dirs: DataDirs) -> None:
    """Windows: make the copy in the background when the background service starts, so a first start doesn't
    wait for it. Never raises."""
    if not IS_WINDOWS:
        return

    def make() -> None:
        from ..core import runtimes

        with contextlib.suppress(Exception):
            folder = node_folder(found) if (found := runtimes.which("node")) else None
            if folder is not None and needs_copy(folder):
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
    if folder is None or (_target(folder, dirs) / DONE).is_file() or not needs_copy(folder):
        return None
    return lambda: private_copy(folder, dirs)


def for_sandbox(argv: list[str], env: dict[str, str], dirs: DataDirs, *, node_service: bool = False,
                ) -> tuple[list[str], dict[str, str], Path | None]:
    """On Windows: ``argv`` and ``env`` switched to the private copy when the sandbox shouldn't run the installed
    Node (:func:`needs_copy`), and the copy's folder (to share read-only); else everything unchanged and None.
    ``node_service``: a Node service's other programs (its console's shell) find the copy first on PATH too."""
    if not IS_WINDOWS:
        return argv, env, None
    folder = _install(argv, node_service)
    if folder is None or not needs_copy(folder):
        return argv, env, None
    from .base import IsolationError

    try:
        private = private_copy(folder, dirs)
    except (OSError, ValueError) as exc:
        raise IsolationError(f"The sandbox can't run Node.js from {folder} as installed, and copying it for "
                             f"sandboxes failed: {exc}. Run this service in Container mode.") from exc
    path = os.pathsep.join([str(private), *(p for p in env.get("PATH", "").split(os.pathsep)
                                            if p and os.path.normcase(p.rstrip("\\/"))
                                            != os.path.normcase(str(folder)))])
    swapped = swap(argv, folder, private) if node_folder(argv[0]) is not None else argv
    return swapped, {**env, "PATH": path}, private
