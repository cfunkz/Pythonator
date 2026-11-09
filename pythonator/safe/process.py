"""Start-up hardening shared by the window and pythonatord, and the one exception hook.
* Both processes work from the data folder, never from wherever they were started (a project folder with a planted
  `git.exe` or `python.dll`), and Windows is told never to search the current folder for programs.
* An uncaught exception in any slot, timer or thread is written to a capped log and reported; it never ends the
  process (PyQt's default for an exception in a slot is to abort)."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

# Windows: a console program started by pythonatord (which has no console) would otherwise open its own window.
NO_WINDOW: dict[str, int] = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
LOG_CAP = 1024 * 1024  # window.log / daemon.log: one megabyte, then the old one is kept as .1

def harden(root: Path) -> None:
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"  # Windows: CreateProcess and `where` skip the cwd
    with contextlib.suppress(OSError):
        os.chdir(root)
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.kernel32.SetDllDirectoryW("")  # DLLs: never from the current folder

def raise_open_files(want: int = 4096) -> None:
    """Many services, each with pipes and logs: macOS starts processes with a soft limit of 256 open files."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, OSError, ValueError):
        pass

def running_as_root() -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False

def write_log(path: Path, text: str) -> None:
    """Append, with the size counted in bytes; a failed rotation is retried on the next write."""
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > LOG_CAP:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n".encode("utf-8", "replace"))
        finally:
            os.close(fd)

def install_hook(log: Path, report: Callable[[str], None] | None = None) -> None:
    """Every uncaught exception, on any thread: logged with its traceback, reported in one line, process kept."""

    def handle(kind: type[BaseException], exc: BaseException, tb: object, where: str = "main") -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, exc, tb)  # type: ignore[arg-type]
            return
        write_log(log, f"uncaught ({where}): " + "".join(traceback.format_exception(kind, exc, tb)))  # type: ignore[arg-type]
        if report is not None:
            with contextlib.suppress(Exception):
                report(f"{kind.__name__}: {exc}"[:300])

    sys.excepthook = handle
    threading.excepthook = lambda a: handle(a.exc_type, a.exc_value or a.exc_type(), a.exc_traceback,
                                            a.thread.name if a.thread else "thread")
