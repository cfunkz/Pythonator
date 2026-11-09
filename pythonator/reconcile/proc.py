"""Process helpers: tree stats and stopping a whole tree. psutil is the only dependency used here."""

from __future__ import annotations

import os
import signal
import sys

import psutil


def tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []

def stats(pid: int, cache: dict[int, psutil.Process]) -> tuple[float, int, int]:
    """(cpu percent, resident bytes, task count) for the whole tree. `cache` keeps Process objects so cpu_percent
    has a previous sample to compare against."""
    cpu, mem, n = 0.0, 0, 0
    seen: set[int] = set()
    for p in tree(pid):
        q = cache.setdefault(p.pid, p)
        seen.add(p.pid)
        try:
            with q.oneshot():
                cpu += q.cpu_percent(None)
                mem += q.memory_info().rss
                n += q.num_threads()
        except psutil.Error:
            continue
    for gone in set(cache) - seen:
        cache.pop(gone, None)
    return cpu, mem, n

def private_bytes(pid: int) -> int:
    """Memory the tree really uses: PSS on Linux (shared pages split between sharers), USS elsewhere. Summed RSS
    counts a pre-fork server's shared pages once per worker and killed such servers at a quarter of their limit.
    Costlier than RSS, so the watchdog only asks when the RSS sum is already over the limit."""
    total = 0
    for p in tree(pid):
        try:
            info = p.memory_full_info()
        except psutil.Error:
            continue
        total += getattr(info, "pss", 0) or getattr(info, "uss", 0) or info.rss
    return total

def start_time(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0

def same_process(pid: int, created: float) -> bool:
    """True when `pid` is still the process that was started at `created` (pids get reused)."""
    t = start_time(pid)
    return t != 0.0 and abs(t - created) < 0.05

def signal_tree(pid: int, sig: int) -> None:
    """Signal the process group (services start in their own session), then any stragglers in the tree."""
    if sys.platform != "win32":
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    for p in reversed(tree(pid)):
        try:
            p.send_signal(sig)
        except psutil.Error:
            pass

def kill_marked(marker: str) -> int:
    """macOS fallback: end every process of yours whose environment carries this run's marker (a child that left
    the process group). Returns how many were found."""
    n = 0
    for p in psutil.process_iter(["pid"]):
        try:
            if p.environ().get("PYTHONATOR_RUN") == marker:
                p.kill()
                n += 1
        except psutil.Error:
            continue
    return n

TERM = signal.SIGTERM
KILL = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
