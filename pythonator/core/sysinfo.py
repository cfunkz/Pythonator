"""What the computer is: its system, processor, memory and disk, and which runtimes and container engines it has.

For the System check window. Nothing here changes anything, and nothing is sent anywhere. :func:`facts` is quick
(psutil and the platform module); :func:`tools` starts each runtime once with ``--version``, so it runs on a
thread of its own. The host name and user name are left out on purpose: the report is safe to paste into an issue.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from ..paths import IS_LINUX, IS_MAC, IS_WINDOWS, quiet_start
from . import runtimes

Fact = tuple[str, str]  # (label, value)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str  # "Python", "Docker"…
    path: str  # where it was found, "" when it wasn't
    version: str = ""  # its own words, first line
    note: str = ""  # "engine not running", "Compose v2.29"…


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    m = int(seconds // 60)
    d, h, m = m // 1440, m // 60 % 24, m % 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


def os_name() -> str:
    """"Windows 11 Pro (build 22631)", "Ubuntu 24.04.1 LTS", "macOS 15.1"…"""
    if IS_WINDOWS:
        release, version, _csd, _ = platform.win32_ver()
        build = version.rsplit(".", 1)[-1] if version else ""
        edition = ""
        with contextlib.suppress(AttributeError, OSError):
            edition = platform.win32_edition() or ""
        if build.isdigit() and int(build) >= 22000:
            release = "11"  # Windows 11 still reports itself as release 10
        edition = {"Professional": "Pro", "Core": "Home"}.get(edition, edition)
        return " ".join(x for x in ("Windows", release, edition, f"(build {build})" if build else "") if x)
    if IS_MAC:
        return f"macOS {platform.mac_ver()[0]}".strip()
    if IS_LINUX:
        with contextlib.suppress(OSError):
            info = platform.freedesktop_os_release()
            return info.get("PRETTY_NAME") or info.get("NAME") or "Linux"
        return "Linux"
    return platform.system() or sys.platform


def cpu_name() -> str:
    """The processor's marketing name, where the system tells it without a program to run."""
    with contextlib.suppress(Exception):
        if IS_WINDOWS:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return " ".join(str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).split())  # type: ignore[attr-defined]
        if IS_LINUX:
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            for key in ("model name", "Model", "Hardware", "cpu model"):
                if m := re.search(rf"^{key}\s*:\s*(.+)$", text, re.MULTILINE):
                    return " ".join(m.group(1).split())
        if IS_MAC:
            done = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                  text=True, timeout=3, check=False, stdin=subprocess.DEVNULL,
                                  startupinfo=quiet_start())
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout.strip()
    return platform.processor() or platform.machine() or "Unknown processor"


def facts(data_folder: Path | None = None) -> dict[str, list[Fact]]:
    """The computer, in sections: ``{"This computer": [(label, value)], "Right now": [...]}``."""
    physical = psutil.cpu_count(logical=False) or 0
    logical = psutil.cpu_count(logical=True) or 1
    cores = f"{physical} cores, {logical} threads" if physical and physical != logical else f"{logical} cores"
    freq = ""
    with contextlib.suppress(Exception):
        f = psutil.cpu_freq()
        if f and f.max:
            freq = f" · up to {f.max / 1000:.1f} GHz"
    mem = psutil.virtual_memory()
    computer: list[Fact] = [
        ("System", os_name()),
        ("Kernel", f"{platform.system()} {platform.release()}"),
        ("Architecture", f"{platform.machine() or '?'} ({'64' if sys.maxsize > 2**32 else '32'}-bit)"),
        ("Processor", cpu_name()),
        ("Cores", cores + freq),
        ("Memory", human_bytes(mem.total)),
    ]
    with contextlib.suppress(Exception):
        swap = psutil.swap_memory()
        if swap.total:
            computer.append(("Swap / page file", human_bytes(swap.total)))
    with contextlib.suppress(Exception):
        computer.append(("Up for", human_duration(time.time() - psutil.boot_time())))
    if IS_LINUX:
        with contextlib.suppress(OSError):
            if "microsoft" in Path("/proc/version").read_text().lower():
                computer.append(("Runs under", "WSL (Windows Subsystem for Linux)"))
    now: list[Fact] = [("CPU in use", f"{psutil.cpu_percent(interval=None):.0f}%"),
                       ("Memory in use", f"{human_bytes(mem.total - mem.available)} of {human_bytes(mem.total)} "
                                         f"({mem.percent:.0f}%)")]
    with contextlib.suppress(Exception):
        if hasattr(os, "getloadavg"):
            one, five, fifteen = os.getloadavg()
            now.append(("Load (1, 5, 15 min)", f"{one:.2f}, {five:.2f}, {fifteen:.2f}"))
    if data_folder is not None:
        with contextlib.suppress(OSError):
            disk = psutil.disk_usage(str(data_folder))
            now.append(("Free disk (data folder)", f"{human_bytes(disk.free)} of {human_bytes(disk.total)}"))
    with contextlib.suppress(Exception):
        now.append(("Processes", f"{len(psutil.pids())}"))
    with contextlib.suppress(Exception):
        battery = psutil.sensors_battery()
        if battery is not None:
            plugged = "plugged in" if battery.power_plugged else "on battery"
            now.append(("Battery", f"{battery.percent:.0f}%, {plugged}"))
    return {"This computer": computer, "Right now": now}


VERSION_ARGS = {"go": ["version"], "java": ["-version"], "dotnet": ["--version"]}


def _first_line(argv: list[str], timeout: float = 4.0) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL, startupinfo=quiet_start(),
                              creationflags=0x08000000 if IS_WINDOWS else 0, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    text = (done.stdout or done.stderr or "").strip()
    return text.splitlines()[0][:120] if text else ""


def tools() -> tuple[list[Tool], list[Tool]]:
    """``(runtimes, container engines)`` found on PATH, with their versions. Slow (it starts each one)."""
    found: list[Tool] = []
    for rt in runtimes.RUNTIMES.values():
        if rt.key == "compose":
            continue  # listed with the engines
        prog = next((p for name in rt.programs if (p := runtimes.which(name))), None)
        if not prog:
            found.append(Tool(rt.label, ""))
            continue
        args = VERSION_ARGS.get(rt.key, ["--version"])
        found.append(Tool(rt.label, prog, _first_line([prog, *args])))
    engines: list[Tool] = []
    for name in ("docker", "podman"):
        prog = runtimes.which(name)
        if not prog:
            engines.append(Tool(name.capitalize(), ""))
            continue
        client = _first_line([prog, "--version"])
        server = _first_line([prog, "version", "--format", "{{.Server.Version}}"], timeout=8)
        compose = _first_line([prog, "compose", "version", "--short"], timeout=8)
        notes = ["engine running" if server else "installed, engine not running"]
        if compose:
            notes.append(f"Compose {compose}")
        engines.append(Tool(name.capitalize(), prog, client, " · ".join(notes)))
    return found, engines
