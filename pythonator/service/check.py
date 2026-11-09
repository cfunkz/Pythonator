"""System check: what protects a service on this machine, measured here, plus the fixes that would help.
Nothing here is static text: every number is measured on this machine and kept for 30 days (bench.json), and
every hint comes from a probe or from a change in those numbers."""

from __future__ import annotations

import asyncio
import os
import platform
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import psutil

from pythonator.model.service import Network, ServiceSpec, Traffic
from pythonator.policy.compile import compile_service
from pythonator.policy.host import HostFacts
from pythonator.policy.plan import Refused
from pythonator.safe.process import NO_WINDOW
from pythonator.store.files import read_json, write_json

KEEP_DAYS = 30
BUDGETS = {"sandbox_ms": 80.0, "container_ms": 500.0, "plugin_ms": 150.0, "daemon_mb": 45.0, "first_paint_ms": 400.0,
           "ui_stall_ms": 50.0}

def probes(host: HostFacts) -> list[dict[str, str]]:
    def row(ok: str, title: str, detail: str, fix: str = "") -> dict[str, str]:
        return {"status": ok, "title": title, "detail": detail, "fix": fix}

    out: list[dict[str, str]] = []
    if host.os == "windows":
        out.append(row("ok" if host.appcontainer else "bad", "AppContainer (Sandbox mode)",
                       "a probe ran in an AppContainer inside a job object" if host.appcontainer
                       else "Windows refused the probe: Sandbox mode is refused",
                       "" if host.appcontainer else "Use Container or WSL mode; see the log in the data folder."))
        out.append(row("ok", "Job objects", "every service, Full access too: Stop ends the tree, limits enforced"))
        out.append(row("ok" if host.wsl_ready else "warn", "WSL mode",
                       "Pythonator's own distribution is set up" if host.wsl_ready
                       else ("WSL is installed; Pythonator's distribution isn't set up" if host.wsl else "WSL isn't installed"),
                       "" if host.wsl_ready else "Engines → WSL → Set up" if host.wsl else "wsl --install --no-distribution"))
    elif host.os == "darwin":
        out.append(row("ok" if host.sandbox_exec else "bad", "Seatbelt (Sandbox mode)",
                       (host.sandbox_exec + (f" · base {host.seatbelt_base}" if host.seatbelt_base else ""))
                       if host.sandbox_exec else "macOS refused the profile: Sandbox mode is refused",
                       "" if host.sandbox_exec else "Use Container mode."))
        out.append(row("warn", "Limits", "memory by watchdog once a second; macOS has no CPU or task caps"))
    else:
        out.append(row("ok" if host.bwrap else "bad", "bubblewrap", host.bwrap or "not installed",
                       "" if host.bwrap else "Install the 'bubblewrap' package."))
        out.append(row("ok" if host.seccomp else "bad", "libseccomp", "loads" if host.seccomp else "not found",
                       "" if host.seccomp else "Install 'libseccomp2' (libseccomp on Fedora and Arch)."))
        if host.bwrap:
            ver = _tool_version(host.bwrap)
            if ver and not version_ok("bwrap", ver):
                out.append(row("bad", "bubblewrap version", f"{ver}: older than {_floor('bwrap')}",
                               "Update bubblewrap from your distribution."))
        if _read_sys("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") == "1":
            out.append(row("warn", "AppArmor user namespaces", "restricted: bubblewrap may be refused its namespaces",
                           "sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0, or install an AppArmor profile "
                           "for bwrap (Ubuntu 24.04+)"))
    if host.os == "linux":
        out.append(
            row(
                "ok" if host.landlock_abi else "warn", "Landlock",
                f"ABI {host.landlock_abi} · kernel {platform.release().split('-')[0]}" if host.landlock_abi else "not available",
                "" if host.landlock_abi else "Use a kernel 5.13 or newer with Landlock enabled.",
            )
        )
        out.append(
            row(
                "ok" if host.systemd_user else "warn", "Limits (cgroup v2)",
                "systemd user session" if host.systemd_user else "no systemd user session: watchdog only",
                "" if host.systemd_user else "loginctl enable-linger $USER",
            )
        )
    if host.os == "linux":
        out.append(
            row(
                "ok" if host.pasta else "warn", "pasta (service network)", host.pasta or "not installed",
                "" if host.pasta else "Install the 'passt' package to allow traffic for Sandbox services.",
            )
        )
    engines = [n for n, path in (("Podman", host.podman), ("Docker", host.docker)) if path]
    out.append(
        row(
            "ok" if engines else "warn", "Container engine",
            " and ".join(engines) if engines else "none: Container mode is unavailable",
            "" if engines else "Install Podman or Docker Desktop if you want Container mode.",
        )
    )
    from pythonator.safe.process import running_as_root

    if running_as_root():
        out.append(row("warn", "Running as root", "Sandbox services would run as root too",
                       "Run Pythonator as your own account."))
    if host.docker and _listening(2375):
        out.append(row("warn", "Docker API on tcp://localhost:2375", "any service with network can control Docker",
                       "Docker Desktop → Settings → General → turn off 'Expose daemon on tcp://localhost:2375'."))
    return out

def _floor(name: str) -> str:
    return ".".join(map(str, CVE_FLOORS[name][0]))

def _read_sys(path: str) -> str:
    try:
        with open(path, encoding="ascii") as f:
            return f.read(16).strip()
    except OSError:
        return ""

def _tool_version(exe: str) -> str:
    import subprocess

    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=2, check=False,  # noqa: S603
                             env={"PATH": "/usr/bin:/bin", "LANG": "C"}, **NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    return m.group(1) if m else ""

def _listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False

class Bench:
    def __init__(self, root: Path) -> None:
        self.path = root / "bench.json"
        data = read_json(self.path, [])
        self.rows: list[dict[str, Any]] = data if isinstance(data, list) else []

    def add(self, **values: float) -> None:
        now = time.time()
        self.rows.append({"ts": now, **{k: round(v, 1) for k, v in values.items()}})
        self.rows = [r for r in self.rows if now - r.get("ts", 0) < KEEP_DAYS * 86400][-2000:]
        write_json(self.path, self.rows)

    def series(self, key: str) -> list[float]:
        return [float(r[key]) for r in self.rows if isinstance(r.get(key), (int, float))]

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for key, budget in BUDGETS.items():
            s = self.series(key)
            out.append(
                {
                    "key": key, "now": s[-1] if s else None, "series": s[-60:], "budget": budget,
                    "measured_at": next((r["ts"] for r in reversed(self.rows) if key in r), None),
                }
            )
        return out

    def hints(self, host: HostFacts) -> list[dict[str, str]]:
        hints: list[dict[str, str]] = []
        fp = self.series("first_paint_ms")
        if len(fp) >= 6:
            before, recent = statistics.median(fp[:-3]), statistics.median(fp[-3:])
            if recent > before * 1.2 and recent - before > 40:
                hints.append(
                    {
                        "level": "warn", "title": f"First paint went from {before:.0f} to {recent:.0f} ms",
                        "detail": "Something added at window start slowed it down. Plugins are the usual cause.",
                        "action": "plugin-timings",
                    }
                )
        sb = self.series("sandbox_ms")
        if sb and sb[-1] > BUDGETS["sandbox_ms"]:
            hints.append(
                {
                    "level": "warn", "title": f"Sandbox start is {sb[-1]:.0f} ms (budget {BUDGETS['sandbox_ms']:.0f})",
                    "detail": "A slow disk or a busy CPU at measure time; measure again when idle.",
                    "action": "measure",
                }
            )
        if host.os == "linux" and not host.systemd_user:
            hints.append(
                {
                    "level": "warn", "title": "No systemd user session",
                    "detail": "Memory and task limits fall back to the watchdog (reduced).",
                    "action": "copy:loginctl enable-linger $USER",
                }
            )
        if host.os == "linux" and not host.pasta:
            hints.append(
                {
                    "level": "info", "title": "pasta isn't installed",
                    "detail": "Sandbox services can only run with Traffic off.",
                    "action": "copy:sudo apt install passt",
                }
            )
        return hints

async def measure_sandbox(host: HostFacts) -> float | None:
    """Time from spawn to the first output line of an empty Python program in a real Sandbox jail."""
    with tempfile.TemporaryDirectory(prefix="pythonator-bench-") as d:
        spec = ServiceSpec(name="bench", folder=d, command=("-c", "print('x')"), network=Network(traffic=Traffic()))
        try:
            jail = compile_service(spec, host)
        except Refused:
            return None
        t0 = time.perf_counter()
        p = await asyncio.create_subprocess_exec(
            *jail.argv, env=jail.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            **({"start_new_session": True} if sys.platform != "win32" else NO_WINDOW),
        )
        if p.stdout is None:
            return None
        line = await asyncio.wait_for(p.stdout.readline(), 10)
        ms = (time.perf_counter() - t0) * 1000
        await p.wait()
        return ms if line.strip() == b"x" else None

def daemon_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)

# Oldest engine versions without known container-escape CVEs (data, checked by System check and the status bar).
CVE_FLOORS = {
    "bwrap": ((0, 4, 1), "CVE-2020-5291 (privilege escalation in setuid mode)"),
    "docker": ((25, 0, 5), "CVE-2024-41110 (AuthZ bypass) and CVE-2024-21626 (runc leaky fd)"),
    "podman": ((4, 9, 4), "CVE-2024-1753 (build mounts host files) and CVE-2024-21626 (runc leaky fd)"),
}

def _ver(text: str) -> tuple[int, ...]:
    import re

    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x or 0) for x in m.groups()) if m else ()

def version_ok(engine: str, version: str) -> bool:
    floor = CVE_FLOORS.get(engine)
    v = _ver(version)
    return not floor or not v or v >= floor[0]

async def engine_version(path: str) -> str:
    import asyncio

    try:
        p = await asyncio.create_subprocess_exec(
            path, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL,
            **NO_WINDOW,
        )
        out, _ = await asyncio.wait_for(p.communicate(), 10)
    except (OSError, TimeoutError):
        return ""
    v = _ver(out.decode(errors="replace"))
    return ".".join(str(x) for x in v) if v else ""

# --- the protection table (screen 10) --------------------------------------------------------------------------
ROWS = (
    ("tree", "Process tree (Stop stops all)"), ("limits", "Memory · CPU · tasks"), ("files", "Files: folder only"),
    ("syscalls", "Syscall filter"), ("network", "Traffic: allowed hosts"), ("loopback", "Block 127.0.0.1 programs"),
    ("ports", "Ports: this computer / LAN"),
)
OS_KEYS = ("linux", "darwin", "windows")

def _sample(mode: str, folder: str, traffic: str, ports: bool = True) -> ServiceSpec:
    d: dict[str, Any] = {
        "name": "check", "folder": folder, "command": ["main.py"], "mode": mode,
        "network": {"traffic": {"mode": traffic, "hosts": ["example.com"] if traffic == "hosts" else []},
                    "ports": [{"port": 8000}] if ports else []},
    }
    if mode == "container":
        d["container"] = {"image": "python:3.13-slim"}
    return ServiceSpec.model_validate(d, strict=False)

def _column(mode: str, target: str, host: HostFacts) -> dict[str, Any]:
    """One OS column: each layer's status and what enforces it. This machine is compiled with its own facts; the
    others with the reference machines (the same compiler)."""
    from pythonator.policy.reference import for_os

    here = target == host.os
    folder = str((PureWindowsPath if host.os == "windows" else PurePosixPath)(host.home, "services", "check"))

    def plan(traffic: str, ports: bool = True) -> Any:
        spec = _sample(mode, folder, traffic, ports)
        if here:
            return compile_service(spec, host).plan
        tspec, ref = for_os(spec, target, host.home)
        if mode == "wsl" and target != "windows":
            raise Refused("RUN-WSL-001", "WSL mode is only on Windows.")
        return compile_service(tspec, ref).plan

    cells: dict[str, dict[str, str]] = {}
    want = "any" if mode == "container" else "hosts"  # containers can't use the gate: their engine owns the network
    try:
        p = plan(want)
    except Refused as e:
        if not e.code.startswith("NET-"):
            return {"os": target, "here": here, "refused": {"code": e.code, "message": e.message, "fix": e.fix}, "cells": {}}
        try:
            p = plan("any")
        except Refused as e2:
            if not e2.code.startswith("NET-"):
                return {"os": target, "here": here, "refused": {"code": e2.code, "message": e2.message, "fix": e2.fix},
                        "cells": {}}
            try:  # the port is what this OS refuses (an AppContainer takes no connections): the rest still runs
                p = plan("any", ports=False)
            except Refused as e3:
                return {"os": target, "here": here, "refused": {"code": e3.code, "message": e3.message, "fix": e3.fix},
                        "cells": {}}
            cells["ports"] = {"status": "refused", "by": e2.code, "detail": e2.message, "fix": e2.fix}
        cells["network"] = {"status": "refused", "by": e.code, "detail": e.message, "fix": e.fix}
    for layer in p.layers:
        if layer.key in cells:
            continue
        cells[layer.key] = {"status": layer.status.value, "by": layer.by, "detail": layer.detail, "fix": ""}
    if cells.get("network", {}).get("status") == "refused" and mode != "container":
        try:  # the allow-list itself may work, with a port asking for something else (WSL ports need Anywhere)
            spec = _sample(mode, folder, "hosts").evolve(network={"traffic": {"mode": "hosts", "hosts": ["example.com"]}})
            if here:
                lp = compile_service(spec, host).plan
            else:
                tspec, ref = for_os(spec, target, host.home)
                lp = compile_service(tspec, ref).plan
            net = next((x for x in lp.layers if x.key == "network"), None)
            if net is not None:
                port_fix = cells["network"]
                cells["network"] = {"status": net.status.value, "by": net.by, "detail": net.detail, "fix": ""}
                if "ports" in cells:
                    cells["ports"] = {**cells["ports"], "status": "reduced", "detail": port_fix["detail"], "fix": port_fix["fix"]}
        except Refused:
            pass
    if mode == "container" and "network" in cells:
        cells["network"] = {**cells["network"], "status": "refused", "by": "engine network",
                            "detail": "A container's engine owns its network, so there's no allow-list: Off or Anywhere.",
                            "fix": "Use Sandbox mode for an allow-list."}
    return {"os": target, "here": here, "refused": None, "cells": cells}

def matrix(host: HostFacts, mode: str) -> dict[str, Any]:
    order = [host.os] + [o for o in OS_KEYS if o != host.os]
    return {"mode": mode, "rows": [{"key": k, "title": t} for k, t in ROWS], "columns": [_column(mode, o, host) for o in order]}

MODES = (("sandbox", "Sandbox"), ("container", "Container"), ("wsl", "WSL"), ("full", "Full access"))
OS_TITLES = {"linux": "Linux", "darwin": "macOS", "windows": "Windows"}

def matrix_markdown() -> str:
    """The protection tables in docs/isolation.md, from the same compiler and reference machines as System check
    (tests/test_invariants.py fails when the docs and the code disagree)."""
    from pythonator.policy.reference import REFERENCE

    host = REFERENCE["linux"]
    out: list[str] = []
    for mode, title in MODES:
        cols = {c["os"]: c for c in matrix(host, mode)["columns"]}
        out += [f"#### {title}", "", "| Protection | " + " | ".join(OS_TITLES[o] for o in OS_KEYS) + " |",
                "| --- |" + " --- |" * len(OS_KEYS)]
        for key, row_title in ROWS:
            cells = []
            for o in OS_KEYS:
                c = cols[o]
                if c["refused"]:
                    cells.append(f"refused ({c['refused']['code']})")
                    continue
                cell = c["cells"].get(key)
                if cell is None:
                    cells.append("—")
                elif cell["status"] == "refused":
                    cells.append(f"refused ({cell['by']})")
                else:
                    cells.append(f"{cell['status']} · {cell['by']}")
            out.append(f"| {row_title} | " + " | ".join(x.replace("|", "/") for x in cells) + " |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"

if __name__ == "__main__":
    if sys.argv[1:] == ["--doc"]:
        sys.stdout.write(matrix_markdown())
