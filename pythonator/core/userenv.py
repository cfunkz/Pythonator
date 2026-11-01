"""The environment Pythonator itself starts with, made as complete as a terminal's.

An app opened from the macOS Finder, Dock, Launchpad or a login item is started by launchd with
``PATH=/usr/bin:/bin:/usr/sbin:/sbin``: Homebrew (``/opt/homebrew/bin``, ``/usr/local/bin``),
nvm, pyenv, asdf, ~/.cargo/bin and everything else the user's shell profile adds is missing, so
``node``, ``go`` or ``npm`` "aren't installed" although they work in Terminal. Before anything
is started, :func:`complete_path` asks the user's login shell for its PATH once (the way VS Code
does) and merges it in, plus the well-known install folders as a fallback.

macOS also starts GUI apps with a soft limit of 256 open files, which a supervisor with dozens of
services (three pipes and a log file each) and the web servers it starts can run out of;
:func:`raise_open_file_limit` lifts it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"
_MARKER = "__PYTHONATOR_PATH__"

# Where package managers put programs, in the order a terminal would usually list them.
MAC_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin", "/opt/local/bin",
            "/opt/local/sbin")
HOME_DIRS = (".local/bin", ".cargo/bin", ".volta/bin", ".bun/bin", ".deno/bin", "go/bin",
              ".local/share/mise/shims", ".asdf/shims", ".pyenv/shims", ".rbenv/shims", ".nodenv/shims")


def _entries(path: str) -> list[str]:
    return [p for p in path.split(os.pathsep) if p]


def looks_incomplete(path: str) -> bool:
    """True for launchd's bare default PATH: no Homebrew / MacPorts / /usr/local folder on it."""
    entries = {p.rstrip("/") for p in _entries(path)}
    return not entries.intersection(MAC_DIRS)


def login_shell_path(shell: str | None = None, timeout: float = 5.0) -> str | None:
    """PATH as the user's login shell sets it (``$SHELL -ilc``), or None if it can't be read.

    Interactive startup files may print banners or ask things, so the value is fenced by markers,
    stdin is closed and the shell runs in its own session, and a shell that hangs is killed.
    """
    shell = shell or os.environ.get("SHELL") or _account_shell() or "/bin/zsh"
    if not Path(shell).is_absolute() or not os.access(shell, os.X_OK):
        return None
    # printenv instead of $PATH: fish, csh and sh all run this the same way.
    script = f"echo {_MARKER}; /usr/bin/printenv PATH; echo {_MARKER}"
    try:
        proc = subprocess.Popen(
            [shell, "-ilc", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "TERM": "dumb", "PYTHONATOR_SHELL_ENV": "1"},
        )
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, 9)
        proc.communicate()
        log.warning("Reading PATH from %s took over %.0fs; using the default folders instead", shell, timeout)
        return None
    parts = out.decode("utf-8", "replace").split(_MARKER)
    if len(parts) < 3:
        return None
    value = parts[1].strip()
    return value or None


def _account_shell() -> str | None:
    try:
        import pwd  # noqa: PLC0415 - POSIX only

        return pwd.getpwuid(os.getuid()).pw_shell or None
    except (ImportError, KeyError, OSError):
        return None


def merged_path(current: str, preferred: str | None, home: Path | None = None) -> str:
    """``preferred`` first, then what only ``current`` has, then well-known install folders that exist."""
    home = home or Path.home()
    out: list[str] = []
    for entry in (*_entries(preferred or ""), *_entries(current)):
        if entry not in out:
            out.append(entry)
    for entry in (*MAC_DIRS, *(str(home / d) for d in HOME_DIRS)):
        if entry not in out and Path(entry).is_dir():
            out.append(entry)
    return os.pathsep.join(out)


def completed_path(environ: Mapping[str, str] | None = None, *, force: bool = False) -> str | None:
    """The PATH a terminal would have, for an app started outside one (macOS); None to keep PATH.

    None on other systems, when PATH already looks like a terminal's (started from one), or when
    ``PYTHONATOR_KEEP_PATH`` is set. Doesn't change anything, so it can run on a thread.
    """
    env = os.environ if environ is None else environ
    if not (IS_MAC or force) or env.get("PYTHONATOR_KEEP_PATH"):
        return None
    current = env.get("PATH", "")
    if not force and not looks_incomplete(current):
        return None
    new = merged_path(current, login_shell_path())
    return new if new != current else None


def complete_path(environ: MutableMapping[str, str] | None = None, *, force: bool = False) -> bool:
    """Apply :func:`completed_path` to ``environ`` (default ``os.environ``). True if PATH changed."""
    env = os.environ if environ is None else environ
    new = completed_path(env, force=force)
    if new is None:
        return False
    env["PATH"] = new
    log.info("PATH completed for an app started outside a terminal: %s", new)
    return True


def raise_open_file_limit(target: int = 10240) -> int | None:
    """Lift the soft open-files limit towards ``target`` (POSIX). Returns the new limit, or None."""
    try:
        import resource  # noqa: PLC0415 - POSIX only
    except ImportError:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if soft == resource.RLIM_INFINITY or soft >= target:
        return soft
    # macOS refuses more than kern.maxfilesperproc even with an unlimited hard limit: step down.
    for step in (target, 4096, 2048, 1024):
        wanted = step if hard == resource.RLIM_INFINITY else min(step, hard)
        if wanted <= soft:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (OSError, ValueError):
            continue
        return wanted
    return soft
