"""Optional per-service CPU limits, for the whole process tree, on every system and any number of cores.

The limit is in percent of one core, summed over every thread and process of the service, like the CPU figure
the window shows: 50 keeps a runaway loop at half of one core, 150 allows one and a half cores, and the number of
cores this computer has times 100 means no limit.

* **Windows**: the service's job object gets a hard CPU rate cap, enforced by the kernel.
* **Containers**: ``--cpus`` for Docker or Podman, enforced by the kernel.
* **Linux and macOS** (sandbox and Full access): Pythonator holds the tree to the limit itself, the way
  ``cpulimit`` does: it lets the tree run for part of every 0.1 s and pauses it (SIGSTOP / SIGCONT) for the rest,
  adjusting that share every half second from what the tree really used. A program that doesn't need the CPU is
  never paused, and one thread or sixty-four are held alike.

Memory limits are elsewhere: a job object limit on Windows, ``--memory`` for containers, and the supervisor's
watchdog for the rest (it stops the tree when it goes over).
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import signal
import threading
import time

import psutil

PERIOD = 0.1  # seconds per run/pause cycle
MEASURE = 0.5  # seconds between re-measuring the tree and adjusting its share
MIN_SHARE = 0.02  # it always runs a little, so it can still answer a stop


def cores() -> int:
    return max(1, psutil.cpu_count() or os.cpu_count() or 1)


def effective(percent: int) -> int:
    """``percent`` if it limits anything on this computer, else 0."""
    return percent if 0 < percent < cores() * 100 else 0


def describe(percent: int) -> str:
    """150 -> "1.5 cores (150% of one core)"."""
    n = percent / 100
    text = f"{n:g} core" + ("" if n == 1 else "s")
    return f"{text} ({percent}% of one core)"


def job_cpu_rate(percent: int) -> int:
    """The Windows job CPU rate (1/100 of a percent of the whole computer) for ``percent`` of one core."""
    return max(1, min(10_000, round(percent * 100 / cores())))


class Throttle:
    """Holds a process tree (Linux, macOS) to ``percent`` of one core by pausing it in short slices."""

    def __init__(self, pid: int, percent: int) -> None:
        self.pid = pid
        self.limit = float(percent)
        self.share = 1.0  # the part of each period it may run
        self._done = threading.Event()
        self._procs: dict[int, psutil.Process] = {}
        self._used: dict[int, float] = {}  # CPU seconds of each process at the last measurement
        self._thread = threading.Thread(target=self._run, name=f"throttle-{pid}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def release(self) -> None:
        """Stop throttling for good and let the tree run (before a stop, so it can handle SIGTERM)."""
        self._done.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(PERIOD * 3)  # its last act is a SIGCONT, so nothing is paused after this
        self._signal(signal.SIGCONT)

    def _tree(self) -> None:
        try:
            root = psutil.Process(self.pid)
            found = [root, *root.children(recursive=True)]
        except psutil.Error:
            found = []
        self._procs = {p.pid: self._procs.get(p.pid, p) for p in found}

    def _signal(self, sig: int) -> None:
        for proc in list(self._procs.values()):
            with contextlib.suppress(psutil.Error, OSError):
                proc.send_signal(sig)  # checks the process is still the same one: a reused pid is never hit

    def _cpu_seconds(self) -> float:
        """CPU seconds the tree used since the last call (processes seen for the first time count from now)."""
        total, seen = 0.0, {}
        for pid, proc in self._procs.items():
            with contextlib.suppress(psutil.Error):
                t = proc.cpu_times()
                now = t.user + t.system
                seen[pid] = now
                total += now - self._used.get(pid, now)
        self._used = seen
        return max(0.0, total)

    def _run(self) -> None:
        self._tree()
        self._cpu_seconds()
        demand = 0.0  # percent of one core it would use unthrottled
        last = time.monotonic()
        try:
            while not self._done.is_set():
                run = PERIOD * self.share
                if self.share < 1.0:
                    if self._done.wait(run):
                        break
                    self._signal(signal.SIGSTOP)
                    self._done.wait(PERIOD - run)
                    self._signal(signal.SIGCONT)
                elif self._done.wait(PERIOD):
                    break
                now = time.monotonic()
                if now - last >= MEASURE:
                    used = self._cpu_seconds() / (now - last) * 100  # percent of one core, while throttled
                    seen = used / max(self.share, MIN_SHARE)
                    demand = seen if demand == 0 else 0.5 * demand + 0.5 * seen
                    self.share = 1.0 if demand <= self.limit else max(MIN_SHARE, min(1.0, self.limit / demand))
                    last = now
                    self._tree()
        finally:
            self._signal(signal.SIGCONT)


# -- typed limits, for the service settings ----------------------------------------------------------------

NO_LIMIT = ("", "0", "none", "no", "off", "no limit", "unlimited")
MIN_MEMORY_MB = 16
MIN_CPU_PERCENT = 5
_MEMORY_UNITS = {"": 1.0, "k": 1 / 1024, "kb": 1 / 1024, "kib": 1 / 1024, "m": 1.0, "mb": 1.0, "mib": 1.0,
                 "g": 1024.0, "gb": 1024.0, "gib": 1024.0, "t": 1024.0 ** 2, "tb": 1024.0 ** 2, "tib": 1024.0 ** 2}
_NUMBER = re.compile(r"^\s*(\d+(?:[.,]\d+)?|[.,]\d+)\s*([a-z%]*)\s*$")


class LimitError(ValueError):
    """Typed text that isn't a limit; the message says what to write instead."""


def memory_total_mb() -> int:
    try:
        return max(1, psutil.virtual_memory().total // (1024 * 1024))
    except (psutil.Error, OSError):
        return 1_048_576


def _number(text: str) -> tuple[float, str] | None:
    m = _NUMBER.match(text.strip().lower().replace(" ", ""))
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return (value, m.group(2)) if math.isfinite(value) else None  # 400 nines are "inf", which no int holds


def parse_memory(text: str, total_mb: int | None = None) -> tuple[int, str]:
    """``"512M"``, ``"2G"``, ``"1.5 GB"``, ``"800"`` (MB) or empty for no limit -> (MB, note).

    Raises :class:`LimitError` for anything else. Above this computer's memory it is lowered to all of it, and the
    note says so."""
    if text.strip().lower() in NO_LIMIT:
        return 0, ""
    parsed = _number(text)
    if parsed is None or parsed[1] not in _MEMORY_UNITS:
        raise LimitError("Memory limit: write a size like 512M, 2G or 1.5 GB, or leave it empty for no limit")
    mb = round(parsed[0] * _MEMORY_UNITS[parsed[1]])
    if mb < MIN_MEMORY_MB:
        raise LimitError(f"Memory limit: at least {MIN_MEMORY_MB} MB, or leave it empty for no limit")
    total = total_mb or memory_total_mb()
    if mb > total:
        return total, f"lowered to {format_memory(total)}, all the memory this computer has"
    return mb, ""


def parse_cpu(text: str, n_cores: int | None = None) -> tuple[int, str]:
    """``"1.5"`` or ``"1.5 cores"`` (cores), ``"150%"`` (of one core) or empty for no limit -> (percent, note).

    Raises :class:`LimitError` for anything else. Above this computer's cores it is lowered to all of them."""
    if text.strip().lower() in NO_LIMIT:
        return 0, ""
    parsed = _number(text)
    if parsed is None or parsed[1] not in ("", "c", "core", "cores", "%"):
        raise LimitError("CPU limit: write cores like 1.5, or a percent of one core like 150%, or leave it empty "
                         "for no limit")
    percent = round(parsed[0] if parsed[1] == "%" else parsed[0] * 100)
    if percent < MIN_CPU_PERCENT:
        raise LimitError(f"CPU limit: at least {MIN_CPU_PERCENT}% of one core ({MIN_CPU_PERCENT / 100:g} cores), "
                         "or leave it empty for no limit")
    n = n_cores or cores()
    if percent >= n * 100:
        return 0, f"all {n} cores is no limit"
    return percent, ""


def format_memory(mb: int) -> str:
    """For the text box: ``512M``-style, so it reads back the same."""
    if not mb:
        return ""
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024} GB"
    if mb >= 1024 and mb % 512 == 0:
        return f"{mb / 1024:g} GB"
    return f"{mb} MB"


def format_cpu(percent: int) -> str:
    if not percent:
        return ""
    n = percent / 100
    return f"{n:g} core" + ("" if n == 1 else "s")
