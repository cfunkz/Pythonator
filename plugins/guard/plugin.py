"""Guard: security rules for services, enforced before they start and watched while they run.

Shows how to write a policy plugin (``check_start`` returns a reason to refuse a start), a watchdog (``on_stats``
with the ``inspect`` permission, acting through ``control``) and a panel (``ui``). Standard library only.

The watchdog also looks at the whole computer (``self.system``): when CPU or memory stays near the top for 30 s,
the service causing it gets the chosen action, so a runaway service can't make the computer crawl, swap or run hot.
"""

from __future__ import annotations

import time
from collections import deque

from pythonator.sdk import Plugin, ServiceInfo, SystemInfo, ui

OVERLOAD_SAMPLES = 15  # stats come every 2 s: about 30 s


def heaviest(services: list[ServiceInfo], system: SystemInfo, what: str) -> ServiceInfo | None:
    """The running service using the most CPU (``what="cpu"``) or memory, if it is a big part of the load:
    at least a quarter of all cores, or a tenth of the memory. Otherwise the load is not ours to act on."""
    running = [s for s in services if s.state == "running"]
    if not running:
        return None
    if what == "cpu":
        top = max(running, key=lambda s: s.cpu)
        return top if top.cpu / system.cores >= 25 else None
    top = max(running, key=lambda s: s.memory)
    return top if system.memory_total and top.memory >= system.memory_total / 10 else None


def parse_ranges(text: str) -> list[tuple[int, int]]:
    """``"3000-3999, 443"`` -> ``[(3000, 3999), (443, 443)]``. Bad parts are ignored."""
    out = []
    for part in text.replace(";", ",").split(","):
        lo, _, hi = part.strip().partition("-")
        if lo.strip().isdigit() and (not hi or hi.strip().isdigit()):
            a, b = int(lo), int(hi or lo)
            if 1 <= a <= b <= 65535:
                out.append((a, b))
    return out


def in_ranges(port: int, ranges: list[tuple[int, int]]) -> bool:
    return not ranges or any(a <= port <= b for a, b in ranges)


class Guard(Plugin):
    def on_start(self) -> None:
        self.violations: deque[list[str]] = deque(maxlen=200)
        self.hot: dict[str, int] = {}  # samples over the CPU limit in a row
        self.overload = {"cpu": 0, "memory": 0}  # samples of the whole computer over its threshold in a row
        self.acted: dict[tuple[str, str], float] = {}
        self.render()

    # -- before a start ---------------------------------------------------------------------------------

    def check_start(self, service: ServiceInfo) -> str | None:
        reason = self.start_problem(service)
        if reason:
            self.record(service.name, reason, "blocked start")
        return reason

    def start_problem(self, s: ServiceInfo) -> str | None:
        if self.settings.get("block_full") and s.mode == "full":
            return "Full access mode isn't allowed (Guard)"
        if self.settings.get("block_lan") and s.expose == "lan" and s.ports:
            return "Other computers may not reach services (Guard)"
        ranges = parse_ranges(str(self.settings.get("allowed_ports", "")))
        bad = [p for p in s.ports if not in_ranges(p, ranges)]
        if bad:
            return f"Port {', '.join(map(str, bad))} isn't allowed (Guard allows {self.settings['allowed_ports']})"
        limit = int(self.settings.get("max_memory_mb", 0) or 0)
        if limit and s.memory_limit_mb > limit:
            return f"Its memory limit is above {limit} MB (Guard)"
        return None

    # -- while running ----------------------------------------------------------------------------------

    def on_stats(self) -> None:
        ranges = parse_ranges(str(self.settings.get("allowed_ports", "")))
        limit_mb = int(self.settings.get("max_memory_mb", 0) or 0)
        limit_cpu = int(self.settings.get("max_cpu", 0) or 0)
        for s in self.services.values():
            if s.state != "running":
                self.hot.pop(s.id, None)
                continue
            if self.settings.get("undeclared_ports", True):
                extra = [p for p in s.listening if p not in s.ports and (s.ports or not in_ranges(p, ranges))]
                if extra and (s.ports or ranges):
                    self.act(s, f"listens on undeclared port {', '.join(map(str, extra))}")
            if limit_mb and s.memory > limit_mb * 1024 * 1024:
                self.act(s, f"uses {s.memory // 1048576} MB, over {limit_mb} MB")
            if limit_cpu:
                self.hot[s.id] = self.hot.get(s.id, 0) + 1 if s.cpu > limit_cpu else 0
                if self.hot[s.id] >= OVERLOAD_SAMPLES:
                    self.hot[s.id] = 0
                    self.act(s, f"used over {limit_cpu}% CPU for 30 s")
        self.check_overload()

    def check_overload(self) -> None:
        """The whole computer near its limit for 30 s (it would slow down, run hot or start swapping): act on the
        service causing it."""
        host = self.system
        for what, level, limit in (("cpu", host.cpu, int(self.settings.get("system_cpu", 95) or 0)),
                                   ("memory", host.memory, int(self.settings.get("system_ram", 92) or 0))):
            self.overload[what] = self.overload[what] + 1 if limit and level > limit else 0
            if self.overload[what] < OVERLOAD_SAMPLES:
                continue
            self.overload[what] = 0
            top = heaviest(list(self.services.values()), host, what)
            if top is not None:
                use = f"{top.cpu:.0f}% CPU" if what == "cpu" else f"{top.memory // 1048576} MB"
                self.act(top, f"overloads the computer ({what} {level:.0f}% for 30 s; it uses {use})")

    def act(self, s: ServiceInfo, problem: str) -> None:
        key = (s.id, problem.split(" ", maxsplit=1)[0])
        if time.monotonic() - self.acted.get(key, -1e9) < 300:
            return  # once per rule and service every 5 minutes
        self.acted[key] = time.monotonic()
        action = str(self.settings.get("action", "warn"))
        if action == "stop":
            self.stop(s.id)
        elif action == "restart":
            self.restart(s.id)
        self.notify(f"{s.name} {problem}" + ("" if action == "warn" else f": {action}"), "warn")
        self.record(s.name, problem, action)

    def record(self, name: str, problem: str, action: str) -> None:
        self.violations.appendleft([time.strftime("%H:%M:%S"), name, problem, action])
        self.render()

    # -- the panel --------------------------------------------------------------------------------------

    def render(self) -> None:
        rules = []
        if self.settings.get("block_full"):
            rules.append("no Full access")
        if self.settings.get("block_lan"):
            rules.append("this computer only")
        if self.settings.get("allowed_ports"):
            rules.append(f"ports {self.settings['allowed_ports']}")
        if self.settings.get("max_memory_mb"):
            rules.append(f"≤ {self.settings['max_memory_mb']} MB")
        if self.settings.get("max_cpu"):
            rules.append(f"≤ {self.settings['max_cpu']}% CPU")
        if self.settings.get("system_cpu", 95) or self.settings.get("system_ram", 92):
            rules.append("computer overload")
        rows = [list(v) for v in self.violations]
        self.show(ui.panel(
            "Guard",
            ui.row(ui.stat("Rules", ", ".join(rules) or "defaults"), ui.stat("Violations", str(len(rows)),
                                                                             "warn" if rows else "ok")),
            ui.table(["Time", "Service", "Problem", "Action"], rows[:100]) if rows
            else ui.text("No violations. Rules are under Plugins → Guard → Settings.", "muted"),
            ui.button("Clear", "clear"),
        ))

    def on_action(self, action: str, value: str) -> None:
        if action == "clear":
            self.violations.clear()
            self.render()

    def on_settings(self) -> None:
        self.render()
