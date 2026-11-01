"""Running one child process with non-blocking output capture and reliable process-tree shutdown.

Each :class:`ProcessRunner` uses three small daemon threads:

* a *reader* that pulls merged stdout/stderr in 64 KiB chunks and decodes UTF-8 incrementally,
* a *writer* that feeds queued text to the child's stdin without ever blocking the caller,
* a *waiter* that detects exit, cleans up leftover children and reports :class:`ExitInfo`.

The child gets its own process group (POSIX session / Windows process group) so stopping it
also stops everything it spawned: uvicorn/gunicorn workers, shells, subprocesses, ...
"""

from __future__ import annotations

import codecs
import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .launch import LaunchSpec

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency, but stay usable without it
    psutil = None  # type: ignore[assignment]

_HAS_PSUTIL: bool = psutil is not None

_CHUNK = 64 * 1024
_ORPHAN_GRACE = 1.0


@dataclass(frozen=True, slots=True)
class ExitInfo:
    code: int | None
    requested: bool  # True if the exit was caused by stop()/kill()
    runtime: float  # seconds the process was alive

    @property
    def success(self) -> bool:
        return self.code == 0

    def describe(self) -> str:
        if self.code is None:
            return "unknown exit status"
        if self.code < 0 and sys.platform != "win32":
            try:
                return f"killed by {signal.Signals(-self.code).name}"
            except ValueError:
                return f"killed by signal {-self.code}"
        if sys.platform == "win32" and self.code >= 0x80000000:
            return f"exit code 0x{self.code:08X}"
        return f"exit code {self.code}"


def _windows_has_console() -> bool:
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    return False


class ProcessRunner:
    """Owns a single ``subprocess.Popen``. All public methods are thread-safe and non-blocking."""

    def __init__(
        self,
        spec: LaunchSpec,
        on_output: Callable[[str], None],
        on_exit: Callable[[ExitInfo], None],
    ) -> None:
        self.spec = spec
        self._on_output = on_output
        self._on_exit = on_exit
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._stop_requested = False
        self._graceful_ok = True
        self._proc_exited = threading.Event()
        self._done = threading.Event()
        self._stdin: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._reader: threading.Thread | None = None
        self.started_at = 0.0

    # -- lifecycle ------------------------------------------------------------------------

    def start(self) -> int:
        """Spawn the process. Raises ``OSError`` if it cannot be started."""
        kwargs: dict[str, Any] = {
            "cwd": str(self.spec.cwd),
            "env": self.spec.env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 0,
        }
        args: str | list[str] = self.spec.argv
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            if not _windows_has_console():
                # GUI build: don't flash a console window per child. CTRL_BREAK can't be delivered
                # without a shared console, so stop() falls back to terminating the tree.
                flags |= subprocess.CREATE_NO_WINDOW
                self._graceful_ok = False
            kwargs["creationflags"] = flags
            if self.spec.cmdline is not None:
                args = self.spec.cmdline
        else:
            kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(args, **kwargs)
        self.started_at = time.monotonic()
        name = f"pythonator-{self._proc.pid}"
        self._reader = threading.Thread(target=self._read_loop, name=f"{name}-out", daemon=True)
        self._reader.start()
        threading.Thread(target=self._write_loop, name=f"{name}-in", daemon=True).start()
        threading.Thread(target=self._wait_loop, name=f"{name}-wait", daemon=True).start()
        return self._proc.pid

    @property
    def pid(self) -> int:
        return self._proc.pid if self._proc else 0

    @property
    def running(self) -> bool:
        return self._proc is not None and not self._proc_exited.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the process has exited and been reported. Returns False on timeout."""
        return self._done.wait(timeout)

    def send(self, text: str) -> bool:
        """Queue ``text`` for the child's stdin."""
        if not self.running:
            return False
        self._stdin.put(text.encode("utf-8"))
        return True

    def stop(self, grace: float = 10.0) -> None:
        """Ask the process tree to exit (SIGTERM / CTRL+BREAK), then kill it after ``grace`` s."""
        with self._lock:
            if not self.running or self._stop_requested:
                return
            self._stop_requested = True
        threading.Thread(target=self._stop_worker, args=(grace,), daemon=True).start()

    def kill(self) -> None:
        """Kill the whole process tree immediately."""
        with self._lock:
            if not self.running:
                return
            self._stop_requested = True
        threading.Thread(target=self._kill_tree, daemon=True).start()

    # -- threads --------------------------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream: IO[bytes] = self._proc.stdout
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                data = stream.read(_CHUNK)  # raw pipe: returns as soon as anything is available
            except (OSError, ValueError):
                break
            if not data:
                break
            if text := decoder.decode(data):
                self._safe_output(text)
        if tail := decoder.decode(b"", final=True):
            self._safe_output(tail)
        with contextlib.suppress(OSError):
            stream.close()

    def _safe_output(self, text: str) -> None:
        with contextlib.suppress(Exception):  # a sink error must never kill the reader thread
            self._on_output(text)

    def _write_loop(self) -> None:
        assert self._proc is not None
        stdin = self._proc.stdin
        while (data := self._stdin.get()) is not None:
            if stdin is None:
                continue
            try:
                view = memoryview(data)
                while view:
                    written = stdin.write(view)
                    view = view[written or 0 :]
            except (OSError, ValueError):
                break
        if stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()

    def _wait_loop(self) -> None:
        assert self._proc is not None
        try:
            code: int | None = self._proc.wait()
        except Exception:
            code = None
        runtime = time.monotonic() - self.started_at
        self._proc_exited.set()
        self._stdin.put(None)
        # Leftover children (e.g. server workers) would keep ports and our pipe open.
        self._reap_orphans(signal.SIGTERM if sys.platform != "win32" else None)
        if self._reader is not None:
            self._reader.join(_ORPHAN_GRACE)
            if self._reader.is_alive():
                self._reap_orphans(signal.SIGKILL if sys.platform != "win32" else None)
                self._reader.join(_ORPHAN_GRACE)
        try:
            self._on_exit(ExitInfo(code, self._stop_requested, runtime))
        finally:
            self._done.set()

    def _stop_worker(self, grace: float) -> None:
        descendants = self._descendants()
        if self._graceful_ok and self._signal_graceful() and self._proc_exited.wait(grace):
            return
        self._kill_tree(descendants)

    # -- signalling -----------------------------------------------------------------------

    def _signal_graceful(self) -> bool:
        assert self._proc is not None
        try:
            if sys.platform == "win32":
                os.kill(self._proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self._proc.pid, signal.SIGTERM)
        except (OSError, ValueError):
            return False
        return True

    def _descendants(self) -> list[Any]:
        if not _HAS_PSUTIL or self._proc is None:
            return []
        try:
            return list(psutil.Process(self._proc.pid).children(recursive=True))
        except (psutil.Error, OSError):
            return []

    def _kill_tree(self, extra: list[Any] | None = None) -> None:
        proc = self._proc
        if proc is None:
            return
        victims = [*(extra or []), *self._descendants()]
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        elif not _HAS_PSUTIL:
            with contextlib.suppress(OSError):
                # Full path: a bare "taskkill" would also be looked up in the current folder.
                taskkill = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"))
                subprocess.run(
                    [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
                )
        for p in victims:
            with contextlib.suppress(Exception):
                p.kill()
        with contextlib.suppress(OSError):
            proc.kill()

    def _reap_orphans(self, sig: int | None) -> None:
        """Terminate processes left behind by the (already exited) main process."""
        proc = self._proc
        if proc is None:
            return
        if sys.platform == "win32":
            self._reap_windows_orphans(proc.pid)
        elif sig is not None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, sig)

    def _reap_windows_orphans(self, parent_pid: int) -> None:
        # Windows has no process groups to signal; find children of the dead parent instead.
        if not _HAS_PSUTIL:
            return
        started = time.time() - (time.monotonic() - self.started_at) - 1.0
        try:
            orphans = [
                p for p in psutil.process_iter(["ppid", "create_time"])
                if p.info["ppid"] == parent_pid and (p.info["create_time"] or 0) >= started
            ]
        except (psutil.Error, OSError):
            return
        for p in orphans:
            with contextlib.suppress(Exception):
                for child in p.children(recursive=True):
                    child.kill()
                p.kill()
