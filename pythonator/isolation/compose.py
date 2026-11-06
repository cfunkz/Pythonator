"""Docker Compose projects: a multi-container app as one service, driven through the engine's own ``compose``.

Start runs ``compose up`` attached (every container's output is the service's console), Stop runs ``compose stop``,
and the service's menu builds, pulls or takes the project down. The project is named ``pythonator-<id>``, so its
containers, networks and volumes never mix with others.

Compose files decide much more than Pythonator's own container mode allows, so every start and build is checked
first, on the file as the engine itself reads it (``compose config``, with variables filled in). It fails closed:
nothing starts when the check can't run, and these are refused, each with the reason and the fix:

* ``privileged``, host namespaces (``network_mode``/``pid``/``ipc``/``uts``/``userns_mode``/``cgroup: host``, or
  another container's), devices, powerful added capabilities, security options that turn protections off;
* bind mounts, build contexts, env files, secrets and configs outside the project folder, and the engine's socket;
* ports published on every network interface while the service is for this computer only (write
  ``127.0.0.1:8000:8000``, or choose "other computers too" in Network);
* the network switched off in Pythonator, which Compose can't apply.

Anything that needs more runs as a Custom command (``docker compose up``) in Full access mode, by choice.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..core.models import Service
from ..paths import IS_WINDOWS, quiet_start
from .base import Command, IsolationError, read_env_file

FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
VERBS = ("up", "stop", "down", "build", "pull", "ps")  # what Pythonator itself runs
DANGEROUS_CAPS = frozenset({"ALL", "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_RAWIO", "SYS_BOOT", "SYS_TIME",
                            "NET_ADMIN", "DAC_READ_SEARCH", "MKNOD", "BPF", "PERFMON", "MAC_ADMIN", "MAC_OVERRIDE",
                            "AUDIT_CONTROL", "LINUX_IMMUTABLE", "SYSLOG"})
NAMESPACES = ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup")
# What the engine's CLI needs from this computer's environment. On Windows it finds its Compose plugin under
# %ProgramFiles%\Docker\cli-plugins (Docker Desktop) or %ProgramData%, so those stay; nothing else of yours does.
KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMW6432",
        "PROGRAMFILES(X86)", "PROGRAMDATA", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY", "CONTAINER_HOST", "CONTAINER_CONNECTION")
EVERYWHERE = ("", "0.0.0.0", "::", "[::]")  # noqa: S104 - what to refuse, not a bind


def project_name(service: Service) -> str:
    return f"pythonator-{service.id}"


def compose_file(service: Service) -> Path:
    """The service's compose file, inside its folder. Raises IsolationError."""
    folder = Path(service.folder).resolve()
    name = service.entry or next((f for f in FILES if (folder / f).is_file()), "")
    if not name:
        raise IsolationError(f"No compose file in {folder} ({' or '.join(FILES[:2])})")
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise IsolationError(f"{name} isn't a file in the service's folder")
    return path


def base(prog: str, service: Service) -> list[str]:
    """``<engine> compose`` for this service's project: its file, its folder, its own project name."""
    folder = Path(service.folder).resolve()
    return [prog, "compose", "--project-directory", str(folder), "-f", str(compose_file(service)),
            "-p", project_name(service)]


def environment(service: Service) -> dict[str, str]:
    """The compose CLI's own environment: what it needs to reach the engine, and the service's variables (for
    ``${VAR}`` in the compose file). Values never go on a command line."""
    env = {k: os.environ[k] for k in KEEP if os.environ.get(k)}
    if service.env_file:
        env.update(read_env_file(Path(service.folder) / service.env_file))
    env.update(service.env)
    env.setdefault("COMPOSE_MENU", "false")  # no interactive key menu in the console
    return env


def _inside(path: str, folder: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(folder)
    except (OSError, ValueError):
        return False


def audit(config: dict[str, Any], folder: Path, *, lan: bool) -> list[str]:
    """What in this (normalised) compose config Pythonator won't run: one line per problem, [] when it's fine."""
    folder = folder.resolve()
    problems: list[str] = []
    services = config.get("services") or {}
    for name, svc in (services.items() if isinstance(services, dict) else ()):
        if not isinstance(svc, dict):
            continue

        def bad(text: str, name: str = name) -> None:
            problems.append(f"{name}: {text}")

        if svc.get("privileged"):
            bad("privileged: true gives it the whole computer")
        for key in NAMESPACES:
            value = str(svc.get(key) or "")
            if value == "host" or value.startswith("container:"):
                bad(f"{key}: {value} shares {'this computer' if value == 'host' else 'another container'}'s "
                    f"{key.replace('_mode', '')}")
        caps = {str(c).upper().removeprefix("CAP_") for c in svc.get("cap_add") or []}
        if caps & DANGEROUS_CAPS:
            bad(f"cap_add {', '.join(sorted(caps & DANGEROUS_CAPS))} can break out of the container")
        if svc.get("devices") or svc.get("device_cgroup_rules"):
            bad("devices give it this computer's hardware")
        for opt in svc.get("security_opt") or []:
            text = str(opt).lower()
            if "unconfined" in text or "disable" in text:
                bad(f"security_opt {opt} turns a protection off")
        for ref in svc.get("volumes_from") or []:
            if str(ref).startswith("container:"):
                bad(f"volumes_from {ref} takes another container's files")
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, dict):
                continue
            kind, source = str(vol.get("type", "")), str(vol.get("source", ""))
            if kind == "npipe" or source.endswith(("docker.sock", "podman.sock")) or "docker_engine" in source:
                bad(f"mounts the container engine ({source}): that is the whole computer")
            elif kind == "bind" and not _inside(source, folder):
                bad(f"mounts {source}, outside the project folder")
        build = svc.get("build")
        if isinstance(build, dict):
            context = str(build.get("context", ""))
            if context and "://" not in context and not context.startswith("git@") and not _inside(context, folder):
                bad(f"builds from {context}, outside the project folder")
            for extra in (build.get("additional_contexts") or {}).values():
                if "://" not in str(extra) and not str(extra).startswith(("docker-image:", "service:")) \
                        and not _inside(str(extra), folder):
                    bad(f"builds with {extra}, outside the project folder")
        for item in svc.get("env_file") or []:
            path = str(item.get("path", "")) if isinstance(item, dict) else str(item)
            if path and not _inside(path, folder):
                bad(f"reads the env file {path}, outside the project folder")
        for port in svc.get("ports") or []:
            if not isinstance(port, dict) or not port.get("published"):
                continue
            host = str(port.get("host_ip", ""))
            if not lan and host in EVERYWHERE:
                bad(f"publishes port {port.get('published')} to every network: write "
                    f"\"127.0.0.1:{port.get('published')}:{port.get('target')}\", or allow other computers in "
                    "Network")
    for kind in ("secrets", "configs"):
        for name, item in (config.get(kind) or {}).items():
            path = str(item.get("file", "")) if isinstance(item, dict) else ""
            if path and not _inside(path, folder):
                problems.append(f"{kind} {name}: reads {path}, outside the project folder")
    return problems


def published(config: dict[str, Any]) -> list[int]:
    """The host ports the project publishes, for the Open button and health checks."""
    out: list[int] = []
    for svc in (config.get("services") or {}).values():
        for port in (svc.get("ports") or []) if isinstance(svc, dict) else []:
            value = str(port.get("published", "")) if isinstance(port, dict) else ""
            if value.isdigit() and int(value) not in out:
                out.append(int(value))
    return out


def read_config(prog: str, service: Service) -> dict[str, Any]:
    """The compose file as the engine reads it (variables filled in, paths made absolute). Raises IsolationError."""
    argv = [*base(prog, service), "config", "--format", "json", "--no-env-resolution"]
    try:
        done = subprocess.run(argv, cwd=service.folder, env=environment(service), capture_output=True, text=True,
                              timeout=60, check=False, stdin=subprocess.DEVNULL, startupinfo=quiet_start(),
                              creationflags=0x08000000 if IS_WINDOWS else 0, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolationError(f"Couldn't check the compose file: {exc}") from None
    if done.returncode != 0:
        lines = [line.strip() for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        why = next((line for line in lines if not line.startswith("Run '")), "") or f"exit code {done.returncode}"
        if "unknown command" in why or "not a docker command" in why:
            why += " (the engine has no Compose plugin: install Docker Compose 2.24 or newer)"
        elif "unknown flag" in why:
            why += " (Pythonator needs Docker Compose 2.24 or newer: update Docker Desktop, or the compose plugin)"
        raise IsolationError(f"Couldn't check the compose file: {why[:400]}")
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise IsolationError("Couldn't check the compose file: the engine's answer wasn't JSON") from None
    if not isinstance(data, dict):
        raise IsolationError("Couldn't check the compose file")
    return data


def check(prog: str, service: Service) -> dict[str, Any]:
    """Read and audit the compose file. Raises IsolationError listing every problem."""
    if not service.network.enabled:
        raise IsolationError("Compose projects always have a network: turn the network on in Network")
    config = read_config(prog, service)
    if problems := audit(config, Path(service.folder), lan=service.network.expose == "lan"):
        raise IsolationError("Not started: the compose file asks for more than a Pythonator container may have:\n  "
                             + "\n  ".join(problems[:20]) + "\nFix the file, or run `docker compose up` as a Custom "
                             "command in Full access mode.")
    return config


def steps(prog: str, service: Service, verb: str) -> list[list[str]]:
    """The commands of one menu action: build, pull, down or ps."""
    b = base(prog, service)
    return {"build": [[*b, "build", "--pull"]], "pull": [[*b, "pull", "--ignore-buildable"]],
            "install": [[*b, "pull", "--ignore-buildable"], [*b, "build", "--pull"]],
            "down": [[*b, "down", "--remove-orphans"]], "ps": [[*b, "ps", "--all"]]}[verb]


def wrap(service: Service, prog: str, *, task: list[str] | None = None) -> Command:
    """``compose up`` for the service, or one of its own ``steps``. The console's shell has no place here: a shell
    on this computer would be Full access."""
    folder = Path(service.folder).resolve()
    b = base(prog, service)
    if task is not None:
        if task[:len(b)] != b or len(task) <= len(b) or task[len(b)] not in VERBS:
            raise IsolationError("The console's shell doesn't run in a Compose project: use the Compose ▾ menu, or "
                                 "`docker compose exec <service> sh` in a terminal")
        if task[len(b)] in ("build", "up"):
            check(prog, service)
        return Command(task, folder, environment(service), describe=f"{Path(prog).name} compose",
                       engine=prog, compose=b)
    config = check(prog, service)
    names = ", ".join(sorted((config.get("services") or {}).keys())) or "no services"
    ports = published(config)
    where = "other computers too" if service.network.expose == "lan" else "this computer"
    describe = (f"{Path(prog).name} compose project {project_name(service)}: {names}"
                + (f", ports {', '.join(map(str, ports))} for {where}" if ports else ""))
    return Command([*b, "up", "--remove-orphans"], folder, environment(service), describe=describe, engine=prog,
                   compose=b)


def stop(compose: list[str], env: dict[str, str], grace: float) -> None:
    """``compose stop``: each container gets SIGTERM, then SIGKILL after ``grace``; ``up`` then ends by itself."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([*compose, "stop", "-t", str(max(1, int(grace)))], env=env, capture_output=True,
                       timeout=grace + 30, check=False, stdin=subprocess.DEVNULL, startupinfo=quiet_start(),
                       creationflags=0x08000000 if IS_WINDOWS else 0)
