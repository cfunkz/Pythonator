"""Container mode: the service runs in a Docker or Podman container, driven through the engine's own CLI.

The container is hardened the same way every time: all capabilities dropped, no new privileges, a read-only
image, a process limit, your user id (Linux), and only the project folder mounted at ``/app``. Ports are published
on ``127.0.0.1`` (this computer) or on every interface (LAN). Nothing here ever generates ``--privileged``,
``--cap-add``, ``--device``, ``--network host`` or a mount other than the project folder.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path, PurePosixPath

from ..core import runtimes
from ..core.models import Mode, Service
from ..paths import IS_LINUX
from .base import Command, IsolationError, read_env_file

APP = "/app"
_engine_cache: dict[str, tuple[float, str]] = {}


def container_name(ident: str) -> str:
    return f"pythonator-{ident}"


def engine(choice: str = "auto") -> str:
    """The engine to use: its program path, or "" when none is installed and running. Cached for 30 s."""
    now = time.monotonic()
    cached = _engine_cache.get(choice)
    if cached and now - cached[0] < 30:
        return cached[1]
    found = ""
    for name in (("podman", "docker") if IS_LINUX else ("docker", "podman")) if choice == "auto" else (choice,):
        prog = runtimes.which(name)
        if not prog:
            continue
        try:
            done = subprocess.run([prog, "version", "--format", "{{.Server.Version}}"], capture_output=True,
                                  text=True, timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip():
            found = prog
            break
    _engine_cache[choice] = (now, found)
    return found


def _inner_argv(service: Service, task: list[str] | None) -> list[str]:
    """The command inside the container: the runtime's program by name, files relative to /app."""
    if task is not None:
        return task
    if service.runtime == runtimes.CUSTOM:
        return list(service.command)
    rt = runtimes.RUNTIMES[service.runtime]
    prog = rt.programs[-1] if rt.key == "python" else rt.programs[0]
    if rt.key == "python" and (Path(service.folder) / ".venv-container").is_dir():
        prog = f"{APP}/.venv-container/bin/python"
    entry = str(PurePosixPath(APP, service.entry)) if service.entry else ""
    return rt.run(prog, entry, list(service.args))


def install_steps(service: Service) -> list[list[str]]:
    """Installing packages inside the container: Python gets a venv of the container's own Python."""
    if service.runtime == "python":
        steps = [["python", "-m", "venv", f"{APP}/.venv-container"]]
        pip = [f"{APP}/.venv-container/bin/python", "-m", "pip", "install", "--disable-pip-version-check"]
        if (Path(service.folder) / "requirements.txt").is_file():
            steps.append([*pip, "-r", "requirements.txt"])
        elif (Path(service.folder) / "pyproject.toml").is_file():
            steps.append([*pip, "-e", "."])
        return steps
    if service.runtime == runtimes.CUSTOM or service.runtime not in runtimes.RUNTIMES:
        return []
    rt = runtimes.RUNTIMES[service.runtime]
    if rt.install is None:
        return []
    prog = rt.programs[0]
    return [[Path(a[0]).name, *a[1:]] for a in rt.install(Path(service.folder), prog)]


def image_for(service: Service) -> str:
    if service.container.image:
        return service.container.image
    if service.runtime in runtimes.RUNTIMES:
        return runtimes.RUNTIMES[service.runtime].image
    return "debian:stable-slim"


def wrap(service: Service, *, task: list[str] | None = None) -> Command:
    if service.mode is not Mode.CONTAINER:
        raise IsolationError("not a container service")
    prog = engine(service.container.engine)
    if not prog:
        raise IsolationError("No container engine is running. Install and start Docker or Podman, "
                             "or switch this service to Sandbox mode.")
    name = container_name(service.id) + ("-task" if task is not None else "")
    folder = Path(service.folder)
    writable = task is not None or service.sandbox.write_project
    argv = [prog, "run", "--rm", "-i", "--init", "--name", name, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "1024",
            "--mount", f"type=bind,source={folder},target={APP}{'' if writable else ',readonly'}", "-w", APP,
            "--label", "pythonator=1"]
    if service.container.read_only_root:
        argv += ["--read-only", "--tmpfs", "/tmp:rw,exec,size=512m", "--tmpfs", "/home/app:rw,size=256m",
                 "-e", "HOME=/home/app"]
    if IS_LINUX and hasattr(os, "getuid"):
        if Path(prog).name == "podman":
            argv += ["--userns", "keep-id"]
        else:
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    if service.memory_mb:
        argv += ["--memory", f"{service.memory_mb}m"]
    if service.cpu_percent:
        argv += ["--cpus", f"{service.cpu_percent / 100:g}"]
    net = service.network
    if task is None and not net.enabled:
        argv += ["--network", "none"]
    elif service.container.network:
        argv += ["--network", service.container.network]
    if task is None and net.enabled:
        host = "" if net.expose == "lan" else "127.0.0.1:"
        for port in net.ports:
            argv += ["-p", f"{host}{port}:{port}"]
    # Values go through the engine CLI's own environment (``-e NAME``), so they never show in a process list.
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
           "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""), "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "")}
    env = {k: v for k, v in env.items() if v}
    passed = {"PYTHONUNBUFFERED": "1", "FORCE_COLOR": "1"}
    if service.env_file:
        passed.update(read_env_file(folder / service.env_file))
    passed.update(service.env)
    if net.ports:
        passed.setdefault("PORT", str(net.ports[0]))
        passed.setdefault("HOST", "0.0.0.0")  # inside the container; the published address decides who reaches it
    for key in passed:
        argv += ["-e", key]
    env.update(passed)
    image = image_for(service)
    argv += [image, *_inner_argv(service, task)]  # the image name is validated: it can't start with "-"
    where = "this computer" if net.expose == "local" else "other computers too"
    published = f", ports {', '.join(map(str, net.ports))} for {where}" if net.ports and net.enabled else ""
    describe = f"{Path(prog).name} container {image}" + ("" if net.enabled else ", no network") + published
    return Command(argv, folder, env, describe=describe, container=name, engine=prog)


def stop(engine_prog: str, name: str, grace: float) -> None:
    """Ask the engine to stop the container (SIGTERM, then SIGKILL after ``grace``)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([engine_prog, "stop", "-t", str(max(1, int(grace))), name], capture_output=True,
                       timeout=grace + 15, check=False, stdin=subprocess.DEVNULL)


def remove_leftover(engine_prog: str, name: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([engine_prog, "rm", "-f", name], capture_output=True, timeout=20, check=False,
                       stdin=subprocess.DEVNULL)
