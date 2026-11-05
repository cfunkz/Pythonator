"""The languages Pythonator runs: how to find each runtime, start a program and install its packages.

Adding a language is one :class:`Runtime` entry in :data:`RUNTIMES`. A service either names an entry file
(``bot.py``, ``server.js``, ``main.go``...) and Pythonator picks the runtime from it, or gives a full command,
which then runs as is.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..paths import IS_WINDOWS

Argv = list[str]
VENV_NAMES = (".venv", "venv", "env")


@dataclass(frozen=True, slots=True)
class Runtime:
    key: str
    label: str
    programs: tuple[str, ...]  # looked up on PATH, first found wins
    extensions: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()  # files that mark a project of this language
    image: str = ""  # the default container image
    run: Callable[[str, str, Argv], Argv] = lambda prog, entry, args: [prog, entry, *args]
    install: Callable[[Path, str], list[Argv]] | None = None
    # Where the project's packages go, shown in the UI: the venv equivalent of each language.
    packages: str = ""


def _python_install(folder: Path, prog: str) -> list[Argv]:
    """``prog`` is the system Python: it makes the venv (or remakes a broken one); pip runs inside the venv."""
    venv = project_venv(folder)
    steps: list[Argv] = []
    home = venv_home(venv) if venv else None
    if venv is None:
        venv = folder / ".venv"
        steps.append([prog, "-m", "venv", str(venv)])
    elif not venv_python(venv).is_file() or (home is not None and not home.exists()):
        steps.append([prog, "-m", "venv", "--clear", str(venv)])
    py = str(venv_python(venv))
    if (folder / "requirements.txt").is_file():
        steps.append([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"])
    elif (folder / "pyproject.toml").is_file():
        steps.append([py, "-m", "pip", "install", "--disable-pip-version-check", "-e", "."])
    return steps


def _node_install(folder: Path, prog: str) -> list[Argv]:
    if (folder / "pnpm-lock.yaml").is_file() and (pnpm := which("pnpm")):
        return [[pnpm, "install", "--frozen-lockfile"]]
    if (folder / "yarn.lock").is_file() and (yarn := which("yarn")):
        return [[yarn, "install", "--frozen-lockfile"]]
    npm = which("npm") or "npm"
    verb = "ci" if (folder / "package-lock.json").is_file() else "install"
    return [[npm, verb, "--no-audit", "--no-fund"]]


def _node_run(prog: str, entry: str, args: Argv) -> Argv:
    if entry:
        return [prog, entry, *args]
    npm = which("npm") or "npm"
    return [npm, "start", *(["--", *args] if args else [])]


def _go_run(prog: str, entry: str, args: Argv) -> Argv:
    return [prog, "run", entry or ".", *args]


def _java_run(prog: str, entry: str, args: Argv) -> Argv:
    return [prog, "-jar", entry, *args] if entry.endswith(".jar") else [prog, entry, *args]


def _dotnet_run(prog: str, entry: str, args: Argv) -> Argv:
    project = ["--project", entry] if entry else []
    return [prog, "run", *project, *(["--", *args] if args else [])]


def _cargo_run(prog: str, entry: str, args: Argv) -> Argv:
    return [prog, "run", "--release", *(["--", *args] if args else [])]


def _powershell_run(prog: str, entry: str, args: Argv) -> Argv:
    return [prog, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", entry, *args]


def _deno_run(prog: str, entry: str, args: Argv) -> Argv:
    # Pythonator's sandbox is the boundary; Deno's own permission prompts would block a background service.
    return [prog, "run", "--allow-all", entry, *args]


RUNTIMES: dict[str, Runtime] = {r.key: r for r in (
    Runtime("python", "Python", ("python", "py") if IS_WINDOWS else ("python3", "python"), (".py", ".pyw"),
            ("pyproject.toml", "requirements.txt"), "python:3.13-slim",
            run=lambda p, e, a: [p, "-u", e, *a], install=_python_install,
            packages="a .venv folder in the project (created for you)"),
    Runtime("node", "Node.js", ("node",), (".js", ".mjs", ".cjs"), ("package.json",), "node:22-slim",
            run=_node_run, install=_node_install, packages="node_modules in the project"),
    Runtime("bun", "Bun", ("bun",), (".ts", ".tsx"), ("bun.lockb", "bun.lock"), "oven/bun:1",
            run=lambda p, e, a: [p, "run", e, *a], install=lambda f, p: [[p, "install"]],
            packages="node_modules in the project"),
    Runtime("deno", "Deno", ("deno",), (), ("deno.json", "deno.jsonc"), "denoland/deno:2",
            run=_deno_run, install=None, packages="Deno's cache (DENO_DIR, private to the sandbox)"),
    Runtime("go", "Go", ("go",), (".go",), ("go.mod",), "golang:1.24",
            run=_go_run, install=lambda f, p: [[p, "mod", "download"]],
            packages="the Go module cache (GOMODCACHE)"),
    Runtime("java", "Java", ("java",), (".java", ".jar"), (), "eclipse-temurin:21",
            run=_java_run, install=None, packages="your build tool's folders (a .jar has them inside)"),
    Runtime("dotnet", ".NET", ("dotnet",), (".csproj", ".fsproj"), (), "mcr.microsoft.com/dotnet/sdk:9.0",
            run=_dotnet_run, install=lambda f, p: [[p, "restore"]], packages="the NuGet cache"),
    Runtime("ruby", "Ruby", ("ruby",), (".rb",), ("Gemfile",), "ruby:3.4-slim",
            install=lambda f, p: [[which("bundle") or "bundle", "install"]],
            packages="vendor/bundle in the project (BUNDLE_PATH)"),
    Runtime("php", "PHP", ("php",), (".php",), ("composer.json",), "php:8.4-cli",
            install=lambda f, p: [[which("composer") or "composer", "install", "--no-interaction"]],
            packages="vendor in the project"),
    Runtime("rust", "Rust", ("cargo",), (".rs",), ("Cargo.toml",), "rust:1-slim",
            run=_cargo_run, install=lambda f, p: [[p, "fetch"]], packages="target and the Cargo cache"),
    Runtime("shell", "Shell", ("bash", "sh"), (".sh",), (), "debian:stable-slim"),
    Runtime("powershell", "PowerShell", ("pwsh", "powershell"), (".ps1",), (), "mcr.microsoft.com/powershell",
            run=_powershell_run),
)}
CUSTOM = "custom"  # a full command line instead of an entry file


def which(program: str) -> str | None:
    """``program`` on PATH. Never in the current folder: a project can't shadow ``python`` or ``node``."""
    found = shutil.which(program, path=os.environ.get("PATH", os.defpath))
    if found and IS_WINDOWS and Path(found).parent.resolve() == Path.cwd().resolve():
        return None
    return found


def for_entry(entry: str) -> Runtime | None:
    """The runtime of an entry file, by its extension (``.ts`` picks Bun, else Deno)."""
    suffix = Path(entry).suffix.lower()
    if suffix in (".ts", ".tsx"):
        return RUNTIMES["bun"] if which("bun") or not which("deno") else RUNTIMES["deno"]
    return next((r for r in RUNTIMES.values() if suffix in r.extensions), None)


def detect(folder: Path) -> tuple[Runtime | None, str]:
    """Guess the runtime and entry file of a project folder: ``(runtime, entry)``."""
    for name in ("main.py", "app.py", "bot.py", "server.py", "index.js", "server.js", "main.go", "main.rb",
                 "index.php", "main.ts", "index.ts"):
        if (folder / name).is_file():
            return for_entry(name), name
    for runtime in RUNTIMES.values():
        if any((folder / marker).is_file() for marker in runtime.markers):
            entry = ""
            if runtime.key == "node":
                entry = _package_main(folder)
            return runtime, entry
    return None, ""


def _package_main(folder: Path) -> str:
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if isinstance(data, dict) and isinstance(data.get("scripts"), dict) and "start" in data["scripts"]:
        return ""  # npm start
    main = data.get("main") if isinstance(data, dict) else None
    return main if isinstance(main, str) else ""


def project_venv(folder: Path) -> Path | None:
    """The project's own virtual environment, if it has one."""
    for name in VENV_NAMES:
        if (folder / name / "pyvenv.cfg").is_file():
            return folder / name
    return None


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_home(venv: Path) -> Path | None:
    """The Python installation a venv was made from (``home =`` in pyvenv.cfg)."""
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                return Path(value.strip())
    except OSError:
        pass
    return None


def program(runtime: Runtime, folder: Path) -> str | None:
    """The runtime's program for this project: the project's venv Python first, then PATH."""
    if runtime.key == "python" and (venv := project_venv(folder)) is not None:
        py = venv_python(venv)
        return str(py) if py.is_file() else None
    return next((p for name in runtime.programs if (p := which(name))), None)


def install_root(prog: str) -> Path:
    """The folder a runtime is installed in (``/usr/local/go`` for ``/usr/local/go/bin/go``)."""
    path = Path(prog).resolve()
    parent = path.parent
    return parent.parent if parent.name.lower() in ("bin", "scripts", "sbin") else parent
