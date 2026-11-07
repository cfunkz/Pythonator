"""What a service can leave behind once it is removed (or stopped): an open door or a busy port.

Removing a service stops it with its whole process tree, deletes its sandbox folders and, on Windows, takes its
sandbox off the folders it was given and undoes **Make reachable** (one administrator prompt). What can still be
left, and what :func:`find` looks for:

- **reach**: Windows only. Make reachable's firewall rules, loopback exemption and loopback permit for a sandbox
  whose service is gone (the prompt was cancelled, or the service was removed before Pythonator undid it on
  removal). Nothing listens behind them, but they admit whatever later runs under that sandbox's name.
- **container**: a running Docker or Podman container of Pythonator's (labelled ``pythonator=1``, or a Compose
  project ``pythonator-<id>``) whose service is gone, or that Pythonator doesn't run any more (a background
  service that crashed leaves its containers running). Its published ports stay taken.
- **process**: a process still listening on a port that was started for a service (Pythonator marks every
  service's environment with ``PYTHONATOR_SERVICE``), whose service is gone, or that isn't part of what Pythonator
  runs for it now. A crash of the background service, or a program that slipped out of its process tree.

Finding needs no administrator. Cleaning up is :func:`clean` (containers, processes) and, for **reach**,
:func:`windows.unreach <pythonator.isolation.windows.unreach>`, which asks Windows for administrator rights once.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..paths import IS_WINDOWS, quiet_start

MARKER = "PYTHONATOR_SERVICE"
IDENT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
OWN_LABEL = re.compile(r"(?:^|[,\s\[])pythonator[=:]1(?=$|[,\s\]])")
COMPOSE_LABEL = re.compile(r"com\.docker\.compose\.project[=:](pythonator-[a-z0-9][a-z0-9-]{0,62})(?=$|[,\s\]])")
HOST_PORT = re.compile(r":(\d{1,5})->")


@dataclass(frozen=True)
class Leftover:
    kind: str  # "reach", "container" or "process"
    key: str  # what clean-up acts on: an AppContainer name, container names, or "pid:start time"
    service: str  # the service's id ("" when it can't be told)
    what: str  # one line for the user
    ports: tuple[int, ...] = ()
    gone: bool = True  # its service no longer exists (else: it exists, but Pythonator doesn't run this)


@dataclass
class Found:
    items: list[Leftover] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what couldn't be checked, and why


def _ports(ports: Iterable[int]) -> str:
    listed = sorted(set(ports))
    if not listed:
        return ""
    return f" (port{'s' if len(listed) > 1 else ''} {', '.join(map(str, listed))})"


def find(services: Mapping[str, Sequence[int]]) -> Found:
    """Everything left behind. ``services``: each service's id → the processes Pythonator runs for it now
    (the background service's ``pids()``). Slow (PowerShell on Windows, the container engine): call it off the
    GUI thread."""
    found = Found()
    _reach(services, found)
    for prog in _engines():
        _containers(services, prog, found)
    _processes(services, found)
    return found


# -- Windows: Make reachable ---------------------------------------------------------------------------------


def _reach(services: Mapping[str, Sequence[int]], found: Found) -> None:
    if not IS_WINDOWS:
        return
    from . import windows

    rules = windows.reach_rules()
    if rules is None:
        found.notes.append("Couldn't read the Windows firewall rules, so Make reachable leftovers weren't checked")
        return
    current = {windows.container_name(sid) for sid in services}
    for name, ports in rules.items():
        if name in current:
            continue  # its service still exists: Make reachable is wanted
        ident = name.removeprefix("Pythonator.")
        found.items.append(Leftover(
            "reach", name, ident if IDENT.fullmatch(ident) else "",
            f"Firewall rules and loopback permit{_ports(ports)}", tuple(ports)))


# -- containers ----------------------------------------------------------------------------------------------


def _engines() -> list[str]:
    """Docker and Podman, each that is installed and running (a service may use either)."""
    from . import container

    out: list[str] = []
    for choice in ("docker", "podman"):
        with contextlib.suppress(Exception):  # a broken engine install is "no engine" here
            if (prog := container.engine(choice)) and prog not in out:
                out.append(prog)
    return out


def _containers(services: Mapping[str, Sequence[int]], prog: str, found: Found) -> None:
    try:
        done = subprocess.run([prog, "ps", "--format", "{{.Names}}\t{{.Ports}}\t{{.Labels}}"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
                              stdin=subprocess.DEVNULL, startupinfo=quiet_start(),
                              creationflags=0x08000000 if IS_WINDOWS else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        found.notes.append(f"Couldn't list the running containers: {exc}")
        return
    if done.returncode != 0:
        found.notes.append(f"Couldn't list the running containers: {(done.stderr or '').strip()[:200]}")
        return
    groups: dict[str, tuple[list[str], set[int]]] = {}
    for line in done.stdout.splitlines():
        name, _t, rest = line.strip().partition("\t")
        ports, _t, labels = rest.partition("\t")
        name = name.strip().split(",")[0]
        if not name:
            continue
        if (m := COMPOSE_LABEL.search(labels)) is not None:
            project = m.group(1)
        elif OWN_LABEL.search(labels) and name.startswith("pythonator-"):
            project = name
        else:
            continue
        names, published = groups.setdefault(project, ([], set()))
        names.append(name)
        published.update(int(p) for p in HOST_PORT.findall(ports) if 0 < int(p) <= 65535)
    for project, (names, published) in sorted(groups.items()):
        ident = project.removeprefix("pythonator-")
        if not IDENT.fullmatch(ident) or any(i.kind == "container" and i.service == ident for i in found.items):
            continue  # (Podman's "docker" stand-in lists the same containers again)
        gone = ident not in services
        if not gone and services[ident]:
            continue  # Pythonator runs it now
        many = len(names) > 1
        found.items.append(Leftover(
            "container", "\n".join([prog, *sorted(names)]), ident,
            f"Container{'s' if many else ''} {', '.join(sorted(names)[:4])}{' …' if len(names) > 4 else ''} still "
            f"running{_ports(published)}", tuple(sorted(published)), gone))


# -- processes -----------------------------------------------------------------------------------------------


def _listening() -> dict[int, set[int]] | None:
    """Every process listening on a TCP port → its ports. None when the system won't say."""
    import psutil

    out: dict[int, set[int]] = {}
    if sys.platform != "darwin":  # macOS lists other processes' sockets only to root: ask each of yours below
        try:
            for c in psutil.net_connections(kind="tcp"):
                if c.status == psutil.CONN_LISTEN and c.pid and c.laddr:
                    out.setdefault(c.pid, set()).add(c.laddr.port)
            return out
        except (psutil.Error, OSError):
            pass
    try:
        procs = list(psutil.process_iter(["username"]))
        me = psutil.Process().username()
    except (psutil.Error, OSError):
        return None
    for proc in procs:
        if proc.info.get("username") != me:
            continue
        with contextlib.suppress(psutil.Error, OSError):
            for own in proc.net_connections(kind="tcp"):
                if own.status == psutil.CONN_LISTEN and own.laddr:
                    out.setdefault(proc.pid, set()).add(own.laddr.port)
    return out


def _trees(pids: Iterable[int]) -> set[int]:
    import psutil

    out: set[int] = set()
    for pid in pids:
        with contextlib.suppress(psutil.Error):
            root = psutil.Process(pid)
            out.add(pid)
            out.update(p.pid for p in root.children(recursive=True))
    return out


def _marker(proc: object) -> str:
    import psutil

    try:
        value = str(proc.environ().get(MARKER, ""))  # type: ignore[attr-defined]
    except (psutil.Error, OSError):
        return ""
    return value if IDENT.fullmatch(value) else ""


def _processes(services: Mapping[str, Sequence[int]], found: Found) -> None:
    import os

    import psutil

    listening = _listening()
    if listening is None:
        found.notes.append("Couldn't list which processes listen on ports")
        return
    ours = _trees(pid for pids in services.values() for pid in pids)
    me = os.getpid()
    for pid, ports in sorted(listening.items()):
        if pid in ours or pid == me:
            continue
        try:
            proc = psutil.Process(pid)
            ident = _marker(proc)
            if not ident:
                continue
            name, created = proc.name(), proc.create_time()
        except (psutil.Error, OSError):
            continue
        found.items.append(Leftover(
            "process", f"{pid}:{created}", ident, f"{name} (process {pid}) still listening{_ports(ports)}",
            tuple(sorted(ports)), ident not in services))


def origin(item: Leftover, names: Mapping[str, str]) -> str:
    """Where it came from, for the user: ``names`` maps the services that still exist to their names."""
    if item.gone:
        return f"left by a removed service ({item.service})" if item.service else "left by a removed service"
    name = names.get(item.service, item.service)
    if item.kind == "container":
        return f"{name}'s, but Pythonator doesn't run it now (a crash of Pythonator's background service?)"
    return f"started for {name}, but outside what Pythonator runs for it now"


# -- cleaning up -------------------------------------------------------------------------------------------


def clean(items: Sequence[Leftover]) -> list[str]:
    """Stop the leftover containers and processes in ``items`` (``reach`` ones are :func:`windows.unreach`'s,
    with its administrator prompt). Each process is checked again first (same pid, start time and service mark),
    so a pid reused since never is. Returns what couldn't be done."""
    import psutil

    errors: list[str] = []
    by_engine: dict[str, list[str]] = {}
    for item in items:
        if item.kind == "container":
            prog, *names = item.key.split("\n")
            by_engine.setdefault(prog, []).extend(n for n in names if n.startswith("pythonator-"))
    running = _engines() if by_engine else []
    for prog, names in by_engine.items():
        if prog not in running:
            errors.append(f"{prog} isn't running any more")
        elif names:
            try:
                done = subprocess.run([prog, "rm", "-f", *names], capture_output=True, text=True, timeout=120,
                                      check=False, stdin=subprocess.DEVNULL, startupinfo=quiet_start(),
                                      creationflags=0x08000000 if IS_WINDOWS else 0)
                if done.returncode != 0:
                    errors.append(f"{prog} rm: {(done.stderr or '').strip()[:300]}")
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"Couldn't remove the containers: {exc}")
    for item in items:
        if item.kind != "process":
            continue
        pid_text, _c, created_text = item.key.partition(":")
        try:
            proc = psutil.Process(int(pid_text))
            if abs(proc.create_time() - float(created_text)) > 1 or _marker(proc) != item.service:
                continue  # it ended, and the pid is someone else's now
            tree = [*proc.children(recursive=True), proc]
        except (psutil.NoSuchProcess, ValueError):
            continue
        except psutil.Error as exc:
            errors.append(f"process {pid_text}: {exc}")
            continue
        for p in tree:
            with contextlib.suppress(psutil.NoSuchProcess):
                try:
                    p.kill()
                except psutil.AccessDenied:
                    errors.append(f"process {p.pid}: access denied")
        psutil.wait_procs(tree, timeout=5)
    return errors
