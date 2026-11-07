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
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..core import runtimes
from ..core.models import Mode, Service
from ..paths import IS_LINUX, quiet_start
from . import wsl
from .base import Command, IsolationError, env_file_values

APP = "/app"
_engine_cache: dict[str, tuple[float, str]] = {}


def container_name(ident: str) -> str:
    return f"pythonator-{ident}"


def engine(choice: str = "auto", distro: str = "") -> str:
    """The engine to use, or "" when none is installed and running. Cached for 30 s. Usually the program's path;
    for ``choice`` "wsl" (Docker Engine inside a WSL distribution, Windows) a ``wsl:<distro>`` id, which
    :func:`cli` turns into the command. "auto" never picks WSL: asking would start its virtual machine."""
    key = f"{choice}\n{distro}"
    now = time.monotonic()
    cached = _engine_cache.get(key)
    if cached and now - cached[0] < 30:
        return cached[1]
    found = ""
    if choice == "wsl":
        found = _wsl_engine(distro)
    else:
        for name in (("podman", "docker") if IS_LINUX else ("docker", "podman")) if choice == "auto" else (choice,):
            prog = runtimes.which(name)
            if prog and _answers([prog]):
                found = prog
                break
    _engine_cache[key] = (now, found)
    return found


def _answers(base: list[str], timeout: float = 10) -> bool:
    """The engine's server answers (Docker Desktop or Podman's machine may be installed but not running)."""
    try:
        done = subprocess.run([*base, "version", "--format", "{{.Server.Version}}"], capture_output=True,
                              timeout=timeout, check=False, stdin=subprocess.DEVNULL, startupinfo=quiet_start(),
                              env=wsl.cli_env() if base[0] == wsl.program() else None)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


WSL_ENGINE = "wsl:"  # an engine id: Docker inside WSL, then the distribution ("" for WSL's default)


def _wsl_engine(distro: str) -> str:
    if not wsl.available():
        return ""
    ident = WSL_ENGINE + distro
    return ident if _answers(cli(ident), timeout=wsl.TIMEOUT) else ""


def is_wsl(engine_id: str) -> bool:
    return engine_id.startswith(WSL_ENGINE)


def cli(engine_id: str) -> list[str]:
    """The command that runs the engine's CLI: its program, or Docker inside a WSL distribution."""
    if is_wsl(engine_id):
        return [*wsl.prefix(engine_id.removeprefix(WSL_ENGINE)), "--exec", "docker"]
    return [engine_id]


def label(engine_id: str) -> str:
    """docker, podman, or "docker in WSL (Ubuntu)"."""
    if is_wsl(engine_id):
        return f"docker in WSL ({engine_id.removeprefix(WSL_ENGINE) or 'default distribution'})"
    return Path(engine_id).name


def linux_name(program: str) -> str:
    """A program by its Linux name: ``C:\\…\\npm.cmd``, found on this Windows computer, is ``npm`` inside."""
    name = PureWindowsPath(program).name if ("\\" in program or ":" in program) else PurePosixPath(program).name
    stem, dot, ext = name.rpartition(".")
    return stem if dot and ext.lower() in ("exe", "cmd", "bat", "ps1") else name


def linux_argv(service: Service, task: list[str] | None, *, base: str = APP, venv: str = ".venv-container",
               python: str = "python") -> list[str]:
    """The command inside Linux (a container, or WSL): the runtime's program by name, files under ``base``,
    Python from the project's ``venv`` when there is one."""
    if task is not None:
        return task
    if service.runtime == runtimes.CUSTOM:
        return list(service.command)
    rt = runtimes.RUNTIMES[service.runtime]
    prog = python if rt.key == "python" else rt.programs[0]
    if rt.key == "python" and (Path(service.folder) / venv).is_dir():
        prog = f"{base}/{venv}/bin/python"
    entry = str(PurePosixPath(base, service.entry)) if service.entry else ""
    argv = rt.run(prog, entry, list(service.args))
    if argv and argv[0] != prog:  # npm start: found on this computer, by name inside
        argv[0] = linux_name(argv[0])
    return argv


def install_steps(service: Service) -> list[list[str]]:
    """Installing packages inside the container: Python gets a venv of the container's own Python. A Compose
    project pulls its images and builds its own."""
    if service.runtime == runtimes.COMPOSE:
        from . import compose

        prog = engine(service.container.engine, service.wsl.distro)
        if not prog:
            raise ValueError("No container engine is running: start Docker or Podman")
        if is_wsl(prog):
            raise ValueError(COMPOSE_IN_WSL)
        return compose.steps(prog, service, "install")
    return linux_install_steps(service)


def linux_install_steps(service: Service, *, base: str = APP, venv: str = ".venv-container",
                        python: str = "python") -> list[list[str]]:
    """Installing packages with Linux's own runtime (a container's, or WSL's): Python gets a venv of its own."""
    if service.runtime == "python":
        steps = [[python, "-m", "venv", f"{base}/{venv}"]]
        pip = [f"{base}/{venv}/bin/python", "-m", "pip", "install", "--disable-pip-version-check"]
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
    return [[linux_name(a[0]), *a[1:]] for a in rt.install(Path(service.folder), prog)]


COMPOSE_IN_WSL = ("A Docker Compose project needs Docker Desktop (engine docker or auto): the WSL engine runs "
                  "single containers only")


# Package caches inside the project folder (the only place that is kept, and writable with a read-only root),
# so an install step's downloads are there when the service runs: the official images keep them elsewhere.
CACHE = f"{APP}/.container-cache"
CONTAINER_CACHE_VARS: dict[str, dict[str, str]] = {
    "go": {"GOMODCACHE": f"{CACHE}/go/mod", "GOCACHE": f"{CACHE}/go/build", "GOFLAGS": "-modcacherw"},
    "rust": {"CARGO_HOME": f"{CACHE}/cargo"},
    "ruby": {"BUNDLE_PATH": f"{CACHE}/bundle"},
    "deno": {"DENO_DIR": f"{CACHE}/deno"},
    "dotnet": {"NUGET_PACKAGES": f"{CACHE}/nuget", "DOTNET_CLI_HOME": "/tmp"},
}
PHP_INSTALL_IMAGE = "composer:2"  # php:*-cli has no composer: installs run in Composer's own image


def image_for(service: Service) -> str:
    if service.container.image:
        return service.container.image
    if service.runtime in runtimes.RUNTIMES:
        return runtimes.RUNTIMES[service.runtime].image
    return "debian:stable-slim"


def wrap(service: Service, *, task: list[str] | None = None) -> Command:
    if service.mode is not Mode.CONTAINER:
        raise IsolationError("not a container service")
    prog = engine(service.container.engine, service.wsl.distro)
    if not prog and service.container.engine == "wsl":
        raise IsolationError(f"Docker isn't running in WSL ({service.wsl.distro or 'default distribution'}). "
                             "Install Docker Engine in that distribution and start it (sudo service docker start), "
                             "or choose another engine.")
    if not prog:
        raise IsolationError("No container engine is running. Install and start Docker or Podman, "
                             "or switch this service to Sandbox mode.")
    in_wsl = is_wsl(prog)
    if service.runtime == runtimes.COMPOSE:
        if in_wsl:
            raise IsolationError(COMPOSE_IN_WSL)
        from . import compose

        return compose.wrap(service, prog, task=task)
    name = container_name(service.id) + ("-task" if task is not None else "")
    folder = Path(service.folder)
    # Docker in WSL mounts the folder as WSL sees it (/mnt/c/…), and runs as your Linux user, who owns it there.
    place = wsl.locate(service.wsl.distro, folder) if in_wsl else None
    source = place.path if place else str(folder)
    if "," in source or '"' in source:  # --mount is comma-separated: ",source=/" would mount another
        raise IsolationError("Container mode can't use a folder whose path has a comma or a quote in it")
    writable = task is not None or service.sandbox.write_project
    argv = [*cli(prog), "run", "--rm", "-i", "--init", "--name", name, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "1024",
            "--mount", f"type=bind,source={source},target={APP}{'' if writable else ',readonly'}", "-w", APP,
            "--label", "pythonator=1"]
    if service.container.read_only_root:
        argv += ["--read-only", "--tmpfs", "/tmp:rw,exec,size=512m", "--tmpfs", "/home/app:rw,size=256m",
                 "-e", "HOME=/home/app"]
    if place is not None:
        argv += ["--user", f"{place.uid}:{place.gid}"]
    elif IS_LINUX and hasattr(os, "getuid"):
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
    passed.update(CONTAINER_CACHE_VARS.get(service.runtime, {}))  # the service's own values below win
    passed.update(env_file_values(service))
    passed.update(service.env)
    if net.ports:
        passed.setdefault("PORT", str(net.ports[0]))
        passed.setdefault("HOST", "0.0.0.0")  # inside the container; the published address decides who reaches it
    for key in passed:
        argv += ["-e", key]
    if in_wsl:
        env = wsl.cli_env(passed)  # docker runs inside WSL: the values cross over through WSLENV
    else:
        env.update(passed)
    image = image_for(service)
    if task is not None and service.runtime == "php" and not service.container.image:
        image = PHP_INSTALL_IMAGE
    argv += [image, *linux_argv(service, task)]  # the image name is validated: it can't start with "-"
    where = "this computer" if net.expose == "local" else "other computers too"
    published = f", ports {', '.join(map(str, net.ports))} for {where}" if net.ports and net.enabled else ""
    if in_wsl and published and net.expose == "lan":
        published += " (from other computers only with WSL's mirrored networking)"
    describe = f"{label(prog)} container {image}" + ("" if net.enabled else ", no network") + published
    return Command(argv, folder, env, describe=describe, container=name, engine=prog)


def stop(engine_prog: str, name: str, grace: float) -> None:
    """Ask the engine to stop the container (SIGTERM, then SIGKILL after ``grace``)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([*cli(engine_prog), "stop", "-t", str(max(1, int(grace))), name], capture_output=True,
                       timeout=grace + 15 + (wsl.TIMEOUT if is_wsl(engine_prog) else 0), check=False,
                       stdin=subprocess.DEVNULL, startupinfo=quiet_start(), env=_cli_env(engine_prog))


def remove_leftover(engine_prog: str, name: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([*cli(engine_prog), "rm", "-f", name], capture_output=True,
                       timeout=20 + (wsl.TIMEOUT if is_wsl(engine_prog) else 0), check=False,
                       stdin=subprocess.DEVNULL, startupinfo=quiet_start(), env=_cli_env(engine_prog))


def _cli_env(engine_id: str) -> dict[str, str] | None:
    """wsl.exe gets a known environment; an engine's own CLI gets yours (None)."""
    return wsl.cli_env() if is_wsl(engine_id) else None
