"""Isolation: turn a service into the exact command that runs it in Full, Sandbox or Container mode.

This package is security-critical and not pluggable. Every mode fails closed: if the isolation a service asks
for can't be set up exactly, :func:`prepare` raises :class:`IsolationError` and nothing starts.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core import runtimes
from ..core.models import Mode, Service
from ..paths import IS_LINUX, IS_MAC, IS_WINDOWS, DataDirs
from . import container
from .base import (
    Command,
    Grant,
    IsolationError,
    Policy,
    base_env,
    env_cache,
    forbidden_read,
    forbidden_write,
    own_env_vars,
    private_dirs,
)
from .base import runtime_folders as runtime_folders

__all__ = ["Command", "IsolationError", "capabilities", "prepare", "service_argv"]

_BATCH_UNSAFE = set('"%^&|<>!\r\n')


def service_argv(service: Service, task: list[str] | None = None) -> list[str]:
    """The program and arguments that run the service (or ``task``), before isolation."""
    if task is not None:
        return task
    folder = Path(service.folder)
    if service.runtime == runtimes.CUSTOM:  # as typed, no shell; the project's own programs first
        argv = [*service.command, *service.args]
        if found := (runtimes.which_in_project(argv[0], folder) if not Path(argv[0]).is_absolute() else argv[0]):
            argv[0] = found
        return argv
    rt = runtimes.RUNTIMES[service.runtime]
    prog = runtimes.program(rt, folder)
    if not prog:
        names = " or ".join(rt.programs)
        raise IsolationError(f"{rt.label} isn't installed: {names} wasn't found on PATH")
    needs_entry = rt.key not in ("node", "go", "dotnet", "rust")
    if needs_entry and not service.entry:
        raise IsolationError(f"Choose the {rt.label} file to run")
    if service.entry and not (folder / service.entry).exists():
        raise IsolationError(f"{service.entry} doesn't exist in {folder}")
    return rt.run(prog, service.entry, list(service.args))


def _check_windows_batch(argv: list[str]) -> None:
    """cmd.exe parses a .bat/.cmd file's arguments again, so these characters could run other commands."""
    if IS_WINDOWS and Path(argv[0]).suffix.lower() in (".bat", ".cmd"):
        for a in argv[1:]:
            if _BATCH_UNSAFE & set(a):
                raise IsolationError('Arguments of a .bat or .cmd file can\'t contain " % ^ & | < > ! or newlines')


def _in_windows(path: Path) -> bool:
    """Under C:\\Windows: every sandbox may read it already (cmd.exe for the console's shell), never touch it."""
    root = os.environ.get("SYSTEMROOT", "") if IS_WINDOWS else ""
    return bool(root) and path.resolve().is_relative_to(Path(root).resolve())


def policy_for(service: Service, argv: list[str], dirs: DataDirs, *, task: bool) -> Policy:
    folder = Path(service.folder).resolve()
    write = task or service.sandbox.write_project
    why = forbidden_write(folder, dirs) if write else forbidden_read(folder, dirs)
    if why:
        raise IsolationError(f"A sandboxed service can't use {why} as its folder. Pick its project folder.")
    grants = [Grant(folder, write)]
    programs = [argv[0]]
    if service.runtime in ("node", "bun") and (node := runtimes.which("node")):  # npm and npx are node scripts
        programs.append(node)
    for prog in programs:
        for p in runtime_folders(prog):
            if not p.exists() or forbidden_read(p, dirs) or _in_windows(p):
                continue
            if write and p.resolve().is_relative_to(folder):
                continue  # the project's own .venv: setup steps write it, so it mustn't be bound read-only on top
            grants.append(Grant(p, False))
    if own_env_vars(service, dirs):
        grants.append(Grant(env_cache(dirs, service), True))
    for extra in service.sandbox.folders:
        path = Path(extra.path).expanduser().resolve()
        why = forbidden_write(path, dirs) if extra.write else forbidden_read(path, dirs)
        if why:
            raise IsolationError(f"{path} is {why}: a sandbox can't be given it")
        grants.append(Grant(path, extra.write))
    net = service.network
    if task:
        return Policy(network=True, grants=grants)
    return Policy(network=net.enabled, ports=tuple(net.ports) if net.enabled else (),
                  lan=net.expose == "lan", grants=grants)


def prepare(service: Service, dirs: DataDirs, *, task: list[str] | None = None, cmdline: str | None = None) -> Command:
    """The command that runs ``service`` (or one of its ``task`` steps, such as an install) in its mode.
    ``cmdline``: Windows, the exact command line of ``task`` (a shell command for cmd.exe)."""
    folder = Path(service.folder)
    if not folder.is_dir():
        raise IsolationError(f"The folder {folder} doesn't exist")
    if service.mode is Mode.CONTAINER:
        cmd = container.wrap(service, task=task)
        cmd.shown = cmd.argv[cmd.argv.index(container.image_for(service)):]
        return cmd
    argv = service_argv(service, task)
    _check_windows_batch(argv)
    env = base_env(service, sandboxed=service.mode is Mode.SANDBOX)
    env.update(own_env_vars(service, dirs))
    if service.runtime in ("python", "node", "bun", runtimes.CUSTOM):
        env = runtimes.activated(env, folder)
    if service.mode is Mode.FULL:
        run, env = runtimes.direct_python(argv, env)
        return Command(run, folder, env, describe="Full access: runs as you, no sandbox", shown=argv,
                       cmdline=cmdline if run is argv else None)
    policy = policy_for(service, argv, dirs, task=task is not None)
    name = f"svc-{service.id}"
    if IS_WINDOWS:
        from . import windows

        cmd = windows.wrap(argv, folder, env, policy, service.id, dirs, cmdline)
    else:
        home, tmp = private_dirs(dirs, name)
        if IS_MAC:
            from . import macos

            cmd = macos.wrap(argv, folder, env, policy, home, tmp)
        elif IS_LINUX:
            from . import linux

            cmd = linux.wrap(argv, folder, env, policy, home, tmp)
        else:
            raise IsolationError("No sandbox for this operating system; use Container mode")
    cmd.describe = f"Sandbox ({cmd.describe}): {_summary(service, policy, task is not None)}"
    cmd.shown = argv
    return cmd


def _summary(service: Service, policy: Policy, task: bool) -> str:
    folder = "folder read & write" if task or service.sandbox.write_project else "folder read only"
    net = "network on" if policy.network else "network off"
    if policy.ports:
        who = "other computers too" if policy.lan else "this computer"
        net += f", serves {', '.join(map(str, policy.ports))} for {who}"
    extra = f", {len(service.sandbox.folders)} more folders" if service.sandbox.folders else ""
    return f"{folder}{extra}, {net}"


def capabilities() -> list[tuple[str, str]]:
    """What the sandbox enforces on this computer, item by item."""
    if IS_WINDOWS:
        from . import windows

        return windows.capabilities()
    if IS_MAC:
        from . import macos

        return macos.capabilities()
    from . import linux

    return linux.support().capabilities()


def forget(service: Service, dirs: DataDirs) -> None:
    """Clean up after a removed service: its private folders and, on Windows, the permissions it was given."""
    import shutil

    if IS_WINDOWS:
        from . import windows

        windows.forget(dirs, service.id)
    shutil.rmtree(dirs.sandboxes / f"svc-{service.id}", ignore_errors=True)
