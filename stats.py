"""Stats Monitor - CPU/RAM monitoring for process trees."""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any, Optional

try: import psutil; HAS_PSUTIL = True
except ImportError: psutil = None; HAS_PSUTIL = False

@dataclass(slots=True)
class ProcessStats:
    cpu_percent: float = 0.0
    ram_mb: float = 0.0
    running: bool = False
    def __str__(self) -> str:
        return f"CPU: {self.cpu_percent:5.1f}%  RAM: {self.ram_mb:6.1f} MB" if self.running else "Stopped"

class StatsMonitor:
    __slots__ = ("_tree_ttl", "_tree_cache", "_cpu_baseline", "_num_cpus")

    def __init__(self, tree_ttl: float = 2.0):
        self._tree_ttl = tree_ttl
        self._tree_cache: dict[int, tuple[float, list[Any]]] = {}
        self._cpu_baseline: dict[int, tuple[float, float]] = {}
        self._num_cpus = psutil.cpu_count() or 1 if HAS_PSUTIL else 1

    def get_stats(self, pid: int) -> ProcessStats:
        if not HAS_PSUTIL or pid <= 0: return ProcessStats()
        procs = self._get_tree(pid)
        if not procs: return ProcessStats()
        
        now = time.monotonic()
        ram_mb = sum(self._safe_rss(p) for p in procs) / (1024 * 1024)
        cpu_sec = sum(self._safe_cpu(p) for p in procs)
        
        cpu_pct = 0.0
        if pid in self._cpu_baseline:
            t0, cpu0 = self._cpu_baseline[pid]
            dt = max(1e-6, now - t0)
            cpu_pct = max(0.0, cpu_sec - cpu0) / (dt * self._num_cpus) * 100.0
        self._cpu_baseline[pid] = (now, cpu_sec)
        return ProcessStats(cpu_percent=cpu_pct, ram_mb=ram_mb, running=True)

    def clear(self, pid: int) -> None:
        self._tree_cache.pop(pid, None); self._cpu_baseline.pop(pid, None)

    def _get_tree(self, pid: int) -> list[Any]:
        now = time.monotonic()
        if (cached := self._tree_cache.get(pid)):
            t, procs = cached
            if (now - t) < self._tree_ttl:
                try:
                    for p in procs: p.status()
                    return procs
                except: pass
        try:
            parent = psutil.Process(pid)
            procs = [parent] + parent.children(recursive=True)
            self._tree_cache[pid] = (now, procs)
            return procs
        except: return []

    @staticmethod
    def _safe_rss(p: Any) -> int:
        try: return p.memory_info().rss
        except: return 0

    @staticmethod
    def _safe_cpu(p: Any) -> float:
        try: t = p.cpu_times(); return t.user + t.system
        except: return 0.0
