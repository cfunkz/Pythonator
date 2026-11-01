"""Background CPU/RAM sampling for process trees, so the GUI thread never calls psutil."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

_HAS_PSUTIL: bool = psutil is not None
log = logging.getLogger(__name__)

HAS_PSUTIL = _HAS_PSUTIL
FAST_PORT_SCAN = 30.0  # seconds after a start during which listening ports are checked every sample


@dataclass(frozen=True, slots=True)
class ProcStats:
    cpu: float = 0.0  # percent of one core, summed over the tree (like `top`)
    rss: int = 0  # bytes, summed over the tree
    procs: int = 0  # processes in the tree
    listening: tuple[tuple[str, int], ...] = ()  # (ip, port) TCP sockets the tree listens on

    @property
    def rss_mb(self) -> float:
        return self.rss / (1024 * 1024)


def listening_sockets(procs: list[Any]) -> tuple[tuple[str, int], ...]:
    """``(ip, port)`` of every TCP socket in LISTEN state owned by ``procs``.

    Works for the user's own processes on Windows, macOS and Linux without admin rights;
    processes that can't be inspected are skipped.
    """
    found: set[tuple[str, int]] = set()
    for p in procs:
        try:
            # psutil 6 renamed connections() to net_connections().
            get = getattr(p, "net_connections", None) or p.connections
            conns = get(kind="tcp")
        except (psutil.Error, OSError):
            continue
        for c in conns:
            if c.status == psutil.CONN_LISTEN and c.laddr:
                found.add((str(c.laddr[0]), int(c.laddr[1])))
    return tuple(sorted(found, key=lambda a: (a[1], a[0])))


class _Tree:
    __slots__ = ("born", "cpu_prev", "listening", "members", "refreshed", "root", "t_prev")

    def __init__(self, root: Any) -> None:
        self.root = root
        self.members: list[Any] = [root]
        self.refreshed = 0.0
        self.cpu_prev = 0.0
        self.t_prev = 0.0
        self.listening: tuple[tuple[str, int], ...] = ()
        self.born = time.monotonic()


class StatsSampler:
    """Samples registered process trees every ``interval`` seconds on a daemon thread."""

    def __init__(self, interval: float = 1.0, tree_refresh: float = 3.0) -> None:
        self._interval = interval
        self._tree_refresh = tree_refresh
        self._targets: dict[str, int] = {}
        self._trees: dict[str, _Tree] = {}
        self._results: dict[str, ProcStats] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if HAS_PSUTIL and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pythonator-stats", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def track(self, key: str, pid: int) -> None:
        with self._lock:
            self._targets[key] = pid
        self._wake.set()

    def untrack(self, key: str) -> None:
        with self._lock:
            self._targets.pop(key, None)
            self._results.pop(key, None)

    def get(self, key: str) -> ProcStats | None:
        with self._lock:
            return self._results.get(key)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._sample_all()
            except Exception:  # never let the sampler die: stats are nice to have, not critical
                log.exception("Resource sampling failed; retrying")
                self._trees.clear()
            self._wake.wait(max(0.05, self._interval - (time.monotonic() - started)))
            self._wake.clear()

    def _sample_all(self) -> None:
        with self._lock:
            targets = dict(self._targets)
        results = {key: self._sample(key, pid) for key, pid in targets.items()}
        for key in list(self._trees):
            if key not in targets:
                del self._trees[key]
        with self._lock:
            self._results = {k: v for k, v in results.items() if k in self._targets and v is not None}

    def _sample(self, key: str, pid: int) -> ProcStats | None:
        now = time.monotonic()
        tree = self._trees.get(key)
        if tree is None or tree.root.pid != pid:
            try:
                tree = _Tree(psutil.Process(pid))
            except (psutil.Error, OSError):
                return None
            self._trees[key] = tree
        # Re-read the tree (children, listening ports) every few seconds, and every sample while a
        # freshly started server is still opening its port, so the Open button shows up quickly.
        starting = not tree.listening and now - tree.born < FAST_PORT_SCAN
        refresh = starting or now - tree.refreshed >= self._tree_refresh
        if refresh:
            with contextlib.suppress(psutil.Error, OSError):
                tree.members = [tree.root, *tree.root.children(recursive=True)]
            tree.refreshed = now
        cpu_total = 0.0
        rss = 0
        alive: list[Any] = []
        for p in tree.members:
            try:
                with p.oneshot():
                    t = p.cpu_times()
                    cpu_total += t.user + t.system
                    rss += p.memory_info().rss
                alive.append(p)
            except (psutil.Error, OSError):
                continue
        if not alive:
            return None
        tree.members = alive
        if refresh:
            tree.listening = listening_sockets(alive)
        cpu = 0.0
        if tree.t_prev:
            cpu = max(0.0, cpu_total - tree.cpu_prev) / max(1e-6, now - tree.t_prev) * 100.0
        tree.cpu_prev, tree.t_prev = cpu_total, now
        return ProcStats(cpu=cpu, rss=rss, procs=len(alive), listening=tree.listening)
