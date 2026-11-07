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
        steps.append([prog, "-m", "venv", "--without-pip", str(venv)])
    elif not venv_python(venv).is_file() or (home is not None and not home.exists()):
        steps.append([prog, "-m", "venv", "--clear", "--without-pip", str(venv)])
    py = str(venv_python(venv))
    needs = (folder / "requirements.txt").is_file() or (folder / "pyproject.toml").is_file()
    if needs and (steps or not venv_has_pip(venv)):
        # pip only when there is something to install: making a venv is a second, setting up pip ten more on
        # Windows. And as a step of its own: venv's own ensurepip call drops PYTHONPATH, and with it the Windows
        # sandbox's fix-ups (Python 3.13 makes private temp folders an AppContainer can't open), so it fails there.
        steps.append([py, "-m", "ensurepip", "--upgrade", "--default-pip"])
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
    Runtime("node", "Node.js", ("node",), (".js", ".mjs", ".cjs"), ("package.json",), "node:24-slim",
            run=_node_run, install=_node_install, packages="node_modules in the project"),
    Runtime("bun", "Bun", ("bun",), (".ts", ".tsx"), ("bun.lockb", "bun.lock"), "oven/bun:1",
            run=lambda p, e, a: [p, "run", e, *a], install=lambda f, p: [[p, "install"]],
            packages="node_modules in the project"),
    Runtime("deno", "Deno", ("deno",), (), ("deno.json", "deno.jsonc"), "denoland/deno:debian",
            run=_deno_run, install=None, packages="its own Deno cache (DENO_DIR)"),
    Runtime("go", "Go", ("go",), (".go",), ("go.mod",), "golang:1.27",
            run=_go_run, install=lambda f, p: [[p, "mod", "download"]],
            packages="its own Go module cache (GOMODCACHE)"),
    Runtime("java", "Java", ("java",), (".java", ".jar"), (), "eclipse-temurin:21",
            run=_java_run, install=None, packages="your build tool's folders (a .jar has them inside)"),
    Runtime("dotnet", ".NET", ("dotnet",), (".csproj", ".fsproj"), (), "mcr.microsoft.com/dotnet/sdk:10.0",
            run=_dotnet_run, install=lambda f, p: [[p, "restore"]], packages="its own NuGet cache"),
    Runtime("ruby", "Ruby", ("ruby",), (".rb",), ("Gemfile",), "ruby:3.4-slim",
            install=lambda f, p: [[which("bundle") or "bundle", "install"]],
            packages="vendor/bundle in the project (BUNDLE_PATH)"),
    Runtime("php", "PHP", ("php",), (".php",), ("composer.json",), "php:8.4-cli",
            install=lambda f, p: [[which("composer") or "composer", "install", "--no-interaction"]],
            packages="vendor in the project"),
    Runtime("rust", "Rust", ("cargo",), (".rs",), ("Cargo.toml",), "rust:1-slim",
            run=_cargo_run, install=lambda f, p: [[p, "fetch"]], packages="target and its own Cargo cache"),
    Runtime("shell", "Shell", ("bash", "sh"), (".sh",), (), "debian:stable-slim"),
    Runtime("powershell", "PowerShell", ("pwsh", "powershell"), (".ps1",), (), "mcr.microsoft.com/powershell",
            run=_powershell_run),
    # Container mode only: the entry is the compose file, run with the engine's own `compose` (isolation/compose).
    Runtime("compose", "Docker Compose", ("docker", "podman"), (),
            ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"),
            packages="the images Compose pulls and builds (Packages ▾ → Build and pull images)"),
)}
CUSTOM = "custom"  # a full command line instead of an entry file
COMPOSE = "compose"  # a Docker Compose project


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
            elif runtime.key == COMPOSE:
                entry = next(m for m in runtime.markers if (folder / m).is_file())
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


def project_bins(folder: Path) -> list[Path]:
    """The project's own program folders, as an activated environment puts them first on PATH: the venv's
    Scripts (or bin) and node_modules/.bin."""
    bins = [venv / ("Scripts" if IS_WINDOWS else "bin")] if (venv := project_venv(folder)) else []
    if (folder / "node_modules" / ".bin").is_dir():
        bins.append(folder / "node_modules" / ".bin")
    return bins


def which_in_project(program: str, folder: Path) -> str | None:
    """A custom command's program: the project's own (``python``, ``uvicorn``, ``pip`` from its venv,
    ``vite`` from node_modules) before the computer's."""
    bins = project_bins(folder)
    found = shutil.which(program, path=os.pathsep.join(map(str, bins))) if bins else None
    return found or which(program)


def activated(env: dict[str, str], folder: Path) -> dict[str, str]:
    """``env`` as in an activated environment: the project's program folders first on PATH, and VIRTUAL_ENV,
    so what the service itself starts (``pip``, ``python -m``) comes from its own environment too."""
    bins = project_bins(folder)
    if not bins:
        return env
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    out = {**env, key: os.pathsep.join([*map(str, bins), env.get(key, "")]).rstrip(os.pathsep)}
    if venv := project_venv(folder):
        out["VIRTUAL_ENV"] = str(venv)
    return out


def shell_argv(line: str, *, container: bool = False) -> tuple[list[str], str | None]:
    """A command line typed into the console's shell: ``(argv, Windows command line)``. cmd.exe on Windows (its
    ``/s /c "…"`` takes the line exactly as typed), sh elsewhere and in containers."""
    if IS_WINDOWS and not container:
        comspec = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "cmd.exe"))
        return [comspec, "/d", "/s", "/c", line], f'"{comspec}" /d /s /c "{line}"'
    return ["/bin/sh", "-c", line], None


def direct_python(argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Windows: run a venv's Python as its own ``python.exe`` redirector would, without the redirector. That
    small launcher re-finds pyvenv.cfg from its own path and fails with exit code 106 when it can't; Python
    itself does the same lookup from ``__PYVENV_LAUNCHER__``, with one process less in the service's tree."""
    prog = Path(argv[0]) if argv else Path()
    venv = prog.parent.parent
    if not IS_WINDOWS or prog.name.lower() != "python.exe" or prog.parent.name.lower() != "scripts" \
            or not (venv / "pyvenv.cfg").is_file():
        return argv, env
    home = venv_home(venv)
    base = home / "python.exe" if home else None
    if base is None or not base.is_file():
        return argv, env
    return [str(base), *argv[1:]], {**env, "__PYVENV_LAUNCHER__": str(prog)}


def venv_has_pip(venv: Path) -> bool:
    return (venv / "Lib" / "site-packages" / "pip").is_dir() or any(venv.glob("lib/python*/site-packages/pip"))


def venv_config(venv: Path, name: str) -> str:
    """A setting of a venv's pyvenv.cfg (``home``, ``version``), "" when it has none."""
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == name and value.strip():
                return value.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def venv_home(venv: Path) -> Path | None:
    """The Python installation a venv was made from (``home =`` in pyvenv.cfg)."""
    home = venv_config(venv, "home")
    return Path(home) if home else None


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


# -- each service's own environment ----------------------------------------------------------------------

# Runtimes whose packages go to a cache shared by the whole computer: with "Own environment" each service
# gets its own, in Pythonator's data folder (``Env.cache``).
CACHE_VARS: dict[str, str] = {"deno": "DENO_DIR", "go": "GOMODCACHE", "dotnet": "NUGET_PACKAGES", "rust": "CARGO_HOME"}
OWN_ENV = frozenset({"python", "node", "bun", "ruby", "php", *CACHE_VARS})  # runtimes "Own environment" applies to


def env_folders(runtime: str, folder: Path, *, container: bool = False) -> list[Path]:
    """The folders in the project that hold its environment: what Recreate environment deletes."""
    if runtime == "python":
        if container:
            return [folder / ".venv-container"]
        venv = project_venv(folder)
        return [venv] if venv is not None else []
    names = {"node": ("node_modules",), "bun": ("node_modules",), "ruby": ("vendor/bundle",),
             "php": ("vendor",)}.get(runtime, ())
    return [folder / n for n in names]


def _has_dependencies(package_json: Path) -> bool:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and any(data.get(k) for k in ("dependencies", "devDependencies"))


def env_missing(runtime: str, folder: Path) -> str:
    """Why the project's own environment has to be made before it can run ("" if it's ready)."""
    if runtime == "python":
        venv = project_venv(folder)
        if venv is None:
            return "it has no virtual environment yet"
        home = venv_home(venv)
        if not venv_python(venv).is_file() or (home is not None and not home.exists()):
            return f"{venv.name} is broken (its Python is gone)"
    elif runtime in ("node", "bun"):
        if _has_dependencies(folder / "package.json") and not (folder / "node_modules").is_dir():
            return "node_modules is missing"
    elif runtime == "ruby":
        if (folder / "Gemfile").is_file() and not (folder / "vendor" / "bundle").is_dir():
            return "vendor/bundle is missing"
    elif runtime == "php" and (folder / "composer.json").is_file() and not (folder / "vendor").is_dir():
        return "vendor is missing"
    return ""


def env_summary(runtime: str, folder: Path, *, own: bool, container: bool = False) -> str:
    """One line for the UI: where this service's packages live and whether they're ready."""
    if container:
        return "Packages live inside the container and the project folder, never on this computer's runtime."
    rt = RUNTIMES.get(runtime)
    if rt is None or runtime not in OWN_ENV:
        return f"Packages go to {rt.packages if rt and rt.packages else 'wherever the program puts them'}."
    if not own:
        if runtime == "python":
            venv = project_venv(folder)
            return (f"Uses {venv.name} in the project." if venv else
                    "Own environment is off: it runs on the system Python and sees its global packages.")
        if runtime in CACHE_VARS:
            return "Own environment is off: packages go to the computer's shared cache."
        if runtime == "ruby":
            return "Own environment is off: gems go to the system Ruby."
        return f"Packages go to {rt.packages}."
    if runtime in CACHE_VARS:
        return f"Packages go to {rt.packages}, private to this service."
    if why := env_missing(runtime, folder):
        return f"Own environment: {why}. Start or Install packages makes it."
    where = env_folders(runtime, folder)
    if where and where[0].exists():
        return f"Own environment: {where[0].relative_to(folder).as_posix()} in the project, ready."
    return f"Own environment: packages go to {rt.packages}."
