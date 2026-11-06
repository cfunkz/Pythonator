"""One running process: start it, read its output, stop it with its whole process tree.

Each process has one thread, which reads the merged stdout/stderr in 64 KiB chunks and, at end of file, waits for
the exit. Everything it learns goes into the supervisor's queue; the supervisor handles it on its own thread, so
no state is shared between threads. Stopping asks first (SIGTERM to the process group, Ctrl+C on Windows,
``stop`` for a container) and kills the tree after the grace period.
"""

from __future__ import annotations

import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import psutil

from ..core import limits
from ..isolation import Command
from ..isolation import container as containers
from ..paths import quiet_start
from . import winproc

CHUNK = 64 * 1024
SMALL_READ = 16 * 1024  # a read shorter than this: the pipe was nearly empty
GATHER = 0.004  # seconds to let output gather after a short read (the window shows it every 50 ms anyway)
MAX_PENDING = 2 * 1024 * 1024  # bytes read but not yet taken; then the reader waits, and so does the program


@dataclass(frozen=True, slots=True)
class Output:
    """New output is waiting in ``source``: take it with ``source.take()``."""

    key: str
    source: Runner


@dataclass(frozen=True, slots=True)
class Exited:
    key: str
    code: int | None
    runtime: float


Event = Output | Exited


class Runner:
    def __init__(self, key: str, cmd: Command, sink: queue.SimpleQueue[Event], *, memory_mb: int = 0,
                 cpu_percent: int = 0) -> None:
        self.key = key
        self.cmd = cmd
        self.sink = sink
        self.memory_mb = memory_mb
        self.cpu_percent = 0 if cmd.container or cmd.compose else limits.effective(cpu_percent)  # containers: --cpus
        self._throttle: limits.Throttle | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.started = 0.0
        self.stop_requested = False
        self.limit_hit = ""
        self._job: winproc.Job | None = None
        self._lock = threading.Lock()
        # Output waits here until the supervisor takes it. A program that writes faster than it can be handled
        # waits on its pipe, like in a terminal, so nothing piles up and a stop is never stuck behind a backlog.
        self._out = bytearray()
        self._out_ready = threading.Condition()

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else 0

    def start(self) -> int:
        """Start the process. Raises OSError if it can't be started."""
        if self.cmd.container and self.cmd.engine:
            containers.remove_leftover(self.cmd.engine, self.cmd.container)
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            self._job = winproc.Job(self.memory_mb if not self.cmd.container else 0,
                                    limits.job_cpu_rate(self.cpu_percent) if self.cpu_percent else 0)
            kwargs["creationflags"] = winproc.CREATE_NO_WINDOW | winproc.CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True  # its own process group: stopping reaches the whole tree
        args = self.cmd.cmdline if (sys.platform == "win32" and self.cmd.cmdline) else self.cmd.argv
        self.proc = subprocess.Popen(
            args, cwd=self.cmd.cwd, env=self.cmd.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, close_fds=True, startupinfo=quiet_start(), **kwargs)  # type: ignore[call-overload]
        if self._job is not None:
            try:
                self._job.adopt(self.proc)
            except OSError:
                self.proc.kill()
                raise
        self.started = time.monotonic()
        if self.cpu_percent and sys.platform != "win32":
            self._throttle = limits.Throttle(self.proc.pid, self.cpu_percent)
            self._throttle.start()
        threading.Thread(target=self._read, name=f"out-{self.key}", daemon=True).start()
        return self.proc.pid

    def _read(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, CHUNK)
            except OSError:
                break
            if not data:
                break
            with self._out_ready:
                while len(self._out) >= MAX_PENDING:
                    self._out_ready.wait(1.0)
                wake = not self._out
                self._out += data
            if wake:
                self.sink.put(Output(self.key, self))
            if len(data) < SMALL_READ:
                # A program printing line by line hands over a line per read: tens of thousands of reads a second
                # under a flood, each a trip through this loop. Waiting a moment lets the pipe gather a chunk.
                time.sleep(GATHER)
        code: int | None
        try:
            code = proc.wait()
            if self._throttle is not None:
                self._throttle.release()
        except OSError:
            code = None
        self._cleanup_tree()
        if self._job is not None:
            self._job.close()
        self.sink.put(Exited(self.key, code, time.monotonic() - self.started))

    def take(self) -> bytes:
        """The output read since the last call (the supervisor's thread only)."""
        with self._out_ready:
            data = bytes(self._out)
            self._out.clear()
            self._out_ready.notify_all()
        return data

    def write(self, text: str) -> bool:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(text.encode("utf-8", "replace"))
            proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def stop(self, grace: float) -> None:
        """Ask the process to end, and kill its tree after ``grace`` seconds. Returns at once."""
        with self._lock:
            if self.stop_requested or self.proc is None:
                return
            self.stop_requested = True
        threading.Thread(target=self._stop, args=(grace,), name=f"stop-{self.key}", daemon=True).start()

    def _stop(self, grace: float) -> None:
        proc = self.proc
        if self._throttle is not None:  # a paused program can't handle SIGTERM
            self._throttle.release()
        if proc is None or proc.poll() is not None:
            return
        if self.cmd.compose:
            from ..isolation import compose

            compose.stop(self.cmd.compose, self.cmd.env, grace)
        elif self.cmd.container and self.cmd.engine:
            containers.stop(self.cmd.engine, self.cmd.container, grace)
        elif sys.platform == "win32":
            if not winproc.request_ctrl_c(proc.pid):
                self.kill()
                return
        else:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self, reason: str = "") -> None:
        """End the whole tree now."""
        if reason:
            self.limit_hit = reason
        proc = self.proc
        if proc is None:
            return
        if self._job is not None:
            self._job.terminate()
        tree = _tree(proc.pid)
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        for p in tree:
            with contextlib.suppress(psutil.Error):
                p.kill()
        with contextlib.suppress(OSError):
            proc.kill()

    def _cleanup_tree(self) -> None:
        """Children left behind by a process that exited (a daemonising server, a stray worker)."""
        if sys.platform != "win32" and self.proc is not None:
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGKILL)

    def tree(self) -> list[psutil.Process]:
        return _tree(self.pid) if self.pid else []


def _tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []
