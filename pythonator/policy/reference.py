"""Reference machines for "Enforced on this machine → macOS / Windows / Podman": what the same spec compiles to on a
typical, fully set-up computer of each kind. The compiler is pure, so this is the real compiler, not a description.
The service folder is moved to the same place under the reference home, so a plan for another OS reads naturally."""

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath, PureWindowsPath

from pythonator.model.service import Mode, ServiceSpec
from pythonator.policy.host import HostFacts

REFERENCE = {
    "linux": HostFacts(
        os="linux", home="/home/me", data_dir="/home/me/.local/share/pythonator", run_dir="/run/user/1000/pythonator",
        uid=1000, bwrap="/usr/bin/bwrap", pasta="/usr/bin/pasta", systemd_run="/usr/bin/systemd-run", systemd_user=True,
        landlock_abi=6, usr_merged=True, python="/usr/bin/python3", launcher="/opt/pythonator/jail/launcher.py",
        node="/usr/bin/node", podman="/usr/bin/podman", seccomp=True, cpus=8,
    ),
    "darwin": HostFacts(
        os="darwin", home="/Users/me", data_dir="/Users/me/Library/Application Support/Pythonator",
        run_dir="/tmp/pythonator-501",  # noqa: S108 - a description of a Mac, not a path used here
        uid=501, python="/opt/homebrew/bin/python3",
        launcher="/Applications/Pythonator.app/jail/launcher.py", sandbox_exec="/usr/bin/sandbox-exec",
        podman="/opt/homebrew/bin/podman", cpus=8,
    ),
    "windows": HostFacts(
        os="windows", home="C:\\Users\\me", data_dir="C:\\Users\\me\\AppData\\Local\\Pythonator",
        python="C:\\Users\\me\\AppData\\Local\\Programs\\Python\\Python313\\python.exe", appcontainer=True,
        winjail="C:\\Users\\me\\AppData\\Local\\Programs\\Pythonator\\jail\\winjail.py",
        wsl="C:\\Windows\\System32\\wsl.exe", wsl_ready=True, podman="C:\\Program Files\\RedHat\\Podman\\podman.exe", cpus=8,
    ),
}

def move_folder(folder: str, home: str, ref: HostFacts) -> str:
    """The same folder relative to the reference home (outside the home: under it, by its last two parts)."""
    win_src = "\\" in folder or (len(folder) > 1 and folder[1] == ":")
    src = PureWindowsPath(folder) if win_src else PurePosixPath(folder)
    hp = PureWindowsPath(home) if win_src else PurePosixPath(home)
    try:
        rel = src.relative_to(hp).parts
    except ValueError:
        rel = src.parts[-2:]
    dst = PureWindowsPath(ref.home) if ref.os == "windows" else PurePosixPath(ref.home)
    return str(dst.joinpath(*rel))

def for_os(spec: ServiceSpec, target: str, home: str) -> tuple[ServiceSpec, HostFacts]:
    """`target`: linux, darwin, windows or podman (the spec as a rootless Podman container on Linux)."""
    ref = REFERENCE["linux" if target == "podman" else target]
    changes: dict[str, object] = {"folder": move_folder(spec.folder, home, ref), "folders": []}
    if target == "podman":
        changes["mode"] = Mode.CONTAINER.value
        changes["container"] = {"image": spec.container.image if spec.container else "python:3.13-slim", "engine": "podman"}
    elif spec.mode is Mode.WSL and target != "windows":
        changes["mode"] = Mode.SANDBOX.value  # WSL is Windows-only; elsewhere the same jail is the Sandbox
    return spec.evolve(**changes), dataclasses.replace(ref)
