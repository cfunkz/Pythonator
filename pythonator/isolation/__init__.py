"""Isolation: turn a service into the exact command that runs it in Full, Sandbox or Container mode.

This package is security-critical and not pluggable. Every mode fails closed: if the isolation a service asks
for can't be set up exactly, :func:`prepare` raises :class:`IsolationError` and nothing starts.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..core import runtimes
from ..core.models import Mode, Service
from ..paths import IS_LINUX, IS_MAC, IS_WINDOWS, DataDirs
from . import container, leftovers
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
    rustup_home,
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


def program_folders(service: Service, argv: list[str], dirs: DataDirs, *, with_node: bool = True) -> list[Path]:
    """The folders the program (and for Node, node itself: npm and npx are node scripts) is installed in.
    ``with_node`` False: the sandbox uses a private copy of Node instead of the installed one."""
    programs = [argv[0]]
    stem = Path(argv[0]).stem.lower()
    node_script = stem in ("npm", "npx") or (service.runtime in ("node", "bun") and stem != "node")
    if with_node and node_script and (node := runtimes.which("node")):
        programs.append(node)
    out: list[Path] = []
    project = Path(service.folder).absolute()
    for prog in programs:
        for p in runtime_folders(prog, project):
            if p.exists() and not forbidden_read(p, dirs) and not _in_windows(p) and p not in out:
                out.append(p)
    return out


def policy_for(service: Service, argv: list[str], dirs: DataDirs, *, task: bool,
               runtime: Path | None = None) -> Policy:
    """``runtime``: a private copy of the runtime (Windows, Node.js) the sandbox reads and runs, never writes."""
    folder = Path(service.folder).resolve()
    write = task or service.sandbox.write_project
    why = forbidden_write(folder, dirs) if write else forbidden_read(folder, dirs)
    if why:
        raise IsolationError(f"A sandboxed service can't use {why} as its folder. Pick its project folder.")
    grants = [Grant(folder, write)]
    for p in program_folders(service, argv, dirs, with_node=runtime is None):
        if write and p.resolve().is_relative_to(folder):
            continue  # the project's own .venv: setup steps write it, so it mustn't be bound read-only on top
        grants.append(Grant(p, False))
    if runtime is not None:
        grants.append(Grant(runtime, False))
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


def slow_setup(service: Service, dirs: DataDirs, *, task: list[str] | None = None) -> Callable[[], object] | None:
    """Work :func:`prepare` would do first that takes seconds (Windows: copying Node.js for sandboxes, once per
    Node version), for the caller to run in the background before it; None when there's none."""
    if not IS_WINDOWS or service.mode is not Mode.SANDBOX:
        return None
    from . import private_node

    try:
        argv = service_argv(service, task)
    except (IsolationError, ValueError, OSError):
        return None  # prepare() says why
    return private_node.pending_copy(argv, dirs, node_service=service.runtime in ("node", "bun"))


def prepare(service: Service, dirs: DataDirs, *, task: list[str] | None = None, cmdline: str | None = None) -> Command:
    """The command that runs ``service`` (or one of its ``task`` steps, such as an install) in its mode.
    ``cmdline``: Windows, the exact command line of ``task`` (a shell command for cmd.exe)."""
    folder = Path(service.folder)
    if not folder.is_dir():
        raise IsolationError(f"The folder {folder} doesn't exist")
    if service.mode is Mode.CONTAINER:
        cmd = container.wrap(service, task=task)
        if cmd.compose:
            cmd.shown = ["compose", *cmd.argv[len(cmd.compose):]]
        else:
            cmd.shown = cmd.argv[cmd.argv.index(container.image_for(service)):]
        return cmd
    argv = service_argv(service, task)
    _check_windows_batch(argv)
    env = base_env(service, sandboxed=service.mode is Mode.SANDBOX)
    env.update(own_env_vars(service, dirs))
    env[leftovers.MARKER] = service.id  # the System check finds what a removed service left listening by it
    if service.runtime in ("python", "node", "bun", runtimes.CUSTOM):
        env = runtimes.activated(env, folder)
    if service.mode is Mode.FULL:
        run, env = runtimes.direct_python(argv, env)
        return Command(run, folder, env, describe="Full access: runs as you, no sandbox", shown=argv,
                       cmdline=cmdline if run is argv else None)
    shown = argv
    name = f"svc-{service.id}"
    if (rustup := rustup_home(Path(runtimes.which(argv[0]) or argv[0]))) is not None:
        env.setdefault("RUSTUP_HOME", str(rustup))  # the sandbox's home isn't yours: tell rustup where it is
    if IS_WINDOWS:
        from . import private_node, windows

        windows.check_not_wsl(runtimes.which(argv[0]) or argv[0])
        argv, env, runtime = private_node.for_sandbox(argv, env, dirs,
                                                      node_service=service.runtime in ("node", "bun"))
        policy = policy_for(service, argv, dirs, task=task is not None, runtime=runtime)
        windows.check_program_folders(program_folders(service, argv, dirs, with_node=runtime is None))
        cmd = windows.wrap(argv, folder, env, policy, service.id, dirs, cmdline)
        if runtime is not None:
            cmd.describe = f"AppContainer, Node.js from Pythonator's private copy in {runtime}"
        cmd.describe = f"Sandbox ({cmd.describe}): {_summary(service, policy, task is not None)}"
        cmd.shown = argv if runtime is not None else shown  # what really runs: node.exe npm-cli.js, not npm.cmd
        cmd.sandboxed = True
        return cmd
    policy = policy_for(service, argv, dirs, task=task is not None)
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
    cmd.shown = shown
    cmd.sandboxed = True
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


def forget(service: Service, dirs: DataDirs, *, deleted: Path | None = None) -> None:
    """Clean up after a removed service: its private folders and, on Windows, the permissions it was given.
    ``deleted``: a folder about to be deleted, whose permissions aren't worth changing first. Slow on Windows
    (permissions change file by file): call it off the event loop."""
    import shutil

    if IS_WINDOWS:
        from . import windows

        windows.forget(dirs, service.id, deleted=deleted)
    shutil.rmtree(dirs.sandboxes / f"svc-{service.id}", ignore_errors=True)
