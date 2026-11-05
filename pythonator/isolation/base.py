"""What every isolation mode shares: the command to run, the folders a sandbox may use, the environment."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Service
from ..paths import FROZEN, IS_WINDOWS, DataDirs, source_root


class IsolationError(Exception):
    """The isolation a service asked for can't be set up here. The service is not started (fail closed)."""


@dataclass(slots=True)
class Command:
    """A program ready to start: its argv, folder and environment, after isolation was applied."""

    argv: list[str]
    cwd: Path
    env: dict[str, str]
    describe: str = ""  # one line for the console: what the isolation allows
    container: str = ""  # container mode: the container's name, to stop it
    engine: str = ""
    cmdline: str | None = None  # Windows: the exact command line, when argv can't express it
    shown: list[str] = field(default_factory=list)  # the service's own command, for the console


@dataclass(frozen=True, slots=True)
class Grant:
    path: Path
    write: bool


@dataclass(slots=True)
class Policy:
    """A sandbox's rules, built from a service (or a plugin)."""

    network: bool = True
    ports: tuple[int, ...] = ()
    lan: bool = False
    grants: list[Grant] = field(default_factory=list)


# Variables pointing into the desktop session or the real home folder: never passed into a sandbox.
SESSION_ENV = frozenset({
    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "SSH_AUTH_SOCK",
    "SSH_AGENT_PID", "GPG_AGENT_INFO", "GNOME_KEYRING_CONTROL", "SESSION_MANAGER", "KRB5CCNAME", "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_SESSION_COOKIE", "KDE_SESSION_UID",
})
# Variables of Pythonator's own interpreter and Qt: never passed to any service.
APP_ENV_PREFIXES = ("QT_", "PYTHONATOR_", "_PYI_", "_MEI")
APP_ENV = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONNOUSERSITE", "VIRTUAL_ENV", "CONDA_PREFIX",
                     "LD_PRELOAD", "LD_LIBRARY_PATH_ORIG", "TCL_LIBRARY", "TK_LIBRARY"})
# Folders in a home folder that hold credentials: never shared into a sandbox automatically.
SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".docker", ".kube", ".password-store", ".config/gh",
               ".config/gcloud", ".netrc", ".git-credentials", ".npmrc", ".pypirc")


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines (``#`` comments, optional quotes, ``export`` prefix). Bad lines are skipped."""
    from ..core import validate

    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for raw in text.splitlines()[:1000]:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        try:
            out[validate.env_key(key)] = validate.arg(value)
        except ValueError:
            continue
    return out


def base_env(service: Service, *, sandboxed: bool) -> dict[str, str]:
    """The service's environment: yours (minus the app's own and, in a sandbox, the desktop session's
    variables), then its ``.env`` file, then the variables set on the service."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in APP_ENV and not k.upper().startswith(APP_ENV_PREFIXES)
           and not (sandboxed and k.upper() in SESSION_ENV)}
    if FROZEN and "LD_LIBRARY_PATH_ORIG" in os.environ:  # PyInstaller changed it for itself
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
    elif FROZEN:
        env.pop("LD_LIBRARY_PATH", None)
    env["PYTHONUNBUFFERED"] = "1"  # output appears as it is printed
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("FORCE_COLOR", "1")
    if service.runtime == "ruby":
        env.setdefault("BUNDLE_PATH", "vendor/bundle")  # gems stay with the project, like a venv
    if service.env_file:
        env.update(read_env_file(Path(service.folder) / service.env_file))
    env.update(service.env)
    if service.network.ports:
        env.setdefault("PORT", str(service.network.ports[0]))
        if service.network.expose == "local":
            env.setdefault("HOST", "127.0.0.1")
    return env


def private_dirs(dirs: DataDirs, name: str) -> tuple[Path, Path]:
    """A sandbox's own home and temp folders, kept between runs."""
    root = dirs.sandboxes / name
    home, tmp = root / "home", root / "tmp"
    for d in (home, tmp):
        d.mkdir(parents=True, exist_ok=True)
        if not IS_WINDOWS:
            d.chmod(0o700)
    return home, tmp


def forbidden_write(path: Path, dirs: DataDirs) -> str:
    """Why a sandbox may not write ``path`` ("" if it may): the home folder itself, its parents, the system
    folders and Pythonator's own data would undo the sandbox."""
    p = path.resolve()
    home = Path.home().resolve()
    if p == home or home.is_relative_to(p):
        return "your whole home folder"
    if p.is_relative_to(dirs.root) or dirs.root.is_relative_to(p):
        return "Pythonator's own data folder"
    if len(p.parts) <= (2 if IS_WINDOWS else 1):
        return "a whole drive"
    for secret in SECRET_DIRS:
        if p.is_relative_to(home / secret):
            return "a folder of passwords or keys"
    if IS_WINDOWS:
        system = [Path(os.environ.get(v, "")) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)")]
        if any(s.parts and p.is_relative_to(s.resolve()) for s in system if str(s)):
            return "a Windows system folder"
    elif p.parts[1:2] and p.parts[1] in ("usr", "bin", "sbin", "lib", "lib64", "etc", "boot", "proc", "sys",
                                          "dev", "System", "Library"):
        return "a system folder"
    return ""


def forbidden_read(path: Path, dirs: DataDirs) -> str:
    p = path.resolve()
    home = Path.home().resolve()
    if home.is_relative_to(p):
        return "your whole home folder"
    if p.is_relative_to(dirs.root) or dirs.root.is_relative_to(p):
        return "Pythonator's own data folder"
    for secret in SECRET_DIRS:
        if p.is_relative_to(home / secret):
            return "a folder of passwords or keys"
    return ""


def runtime_folders(argv0: str) -> list[Path]:
    """The folders a program needs to read to run: its install folder and, for a venv, the base Python."""
    from ..core import runtimes

    out: list[Path] = []
    prog = Path(argv0)
    if not prog.is_absolute():
        found = runtimes.which(argv0)
        if not found:
            return out
        prog = Path(found)
    real = prog.resolve()
    for candidate in {prog.absolute().parent, real.parent}:
        root = candidate.parent if candidate.name.lower() in ("bin", "scripts", "sbin") else candidate
        venv = root if (root / "pyvenv.cfg").is_file() else None
        out.append(root)
        if venv and (home := runtimes.venv_home(venv)):
            out.append(runtimes.install_root(str(home / "python")))
    if IS_WINDOWS:
        return out
    # On macOS and Linux most runtimes live in system folders the sandbox reads anyway.
    return [p for p in out if p.parts[1:2] and p.parts[1] not in ("usr", "bin", "lib", "System")]


def launcher_argv() -> list[str]:
    """How to start the stdlib-only sandbox launcher: isolated Python that finds only Pythonator's package."""
    if FROZEN:
        return [sys.executable, "--sandbox-exec"]
    boot = (f"import sys; sys.path.append({str(source_root())!r}); "
            "from pythonator.launcher import main; sys.exit(main())")
    return [sys.executable, "-I", "-S", "-c", boot]


def launcher_files() -> list[Path]:
    """What the launcher itself must be able to read inside a sandbox."""
    if FROZEN:
        files = [Path(sys.executable).resolve()]
        if bundle := getattr(sys, "_MEIPASS", None):
            files.append(Path(bundle).resolve())
        return files
    return [source_root() / "pythonator", Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
            Path(os.path.realpath(sys.executable)).parent]


def merge(grants: list[Grant]) -> list[Grant]:
    """One entry per path (write wins), parents first so mounts and rules stack the right way."""
    merged: dict[Path, bool] = {}
    for g in grants:
        merged[g.path] = merged.get(g.path, False) or g.write
    return [Grant(p, w) for p, w in sorted(merged.items(), key=lambda kv: (len(kv[0].parts), str(kv[0])))]

