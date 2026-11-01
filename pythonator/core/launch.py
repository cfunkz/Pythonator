"""Turning a :class:`ServiceConfig` into a concrete command line, working directory and env."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LaunchError
from .models import LaunchMode, Runtime, ServiceConfig
from .paths import IS_FROZEN, IS_WINDOWS
from .runtimes import Ctx, find_program, package_manager, path_of, runtime_info, split_options
from .safety import trusted_dir
from .shells import Shell

__all__ = ["LaunchError", "LaunchSpec", "package_manager"]

VENV_NAMES = (".venv", "venv", "env")
_VAR = re.compile(r"\$(?:\{(\w+)\}|(\w+))|%(\w+)%")


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    venv: Path | None = None
    shell: bool = False  # argv[0] is a shell running an ad-hoc command
    cmdline: str | None = None  # Windows only: exact command line, bypassing list2cmdline quoting

    def __post_init__(self) -> None:
        if IS_WINDOWS and self.cmdline is None and self.argv and is_batch_file(self.argv[0]):
            object.__setattr__(self, "cmdline", batch_cmdline(self.argv))

    def display(self) -> str:
        if self.cmdline is not None:
            return self.cmdline
        return subprocess_list2cmdline(self.argv) if IS_WINDOWS else shlex.join(self.argv)


def subprocess_list2cmdline(argv: list[str]) -> str:
    import subprocess  # noqa: PLC0415 - only needed on Windows

    return subprocess.list2cmdline(argv)


# -- cmd.exe quoting (Windows) ------------------------------------------------------------------

_CMD_PLAIN = re.compile(r"[\w\-./\\:@+~]+")


def is_batch_file(program: str) -> bool:
    return Path(program).suffix.lower() in (".bat", ".cmd")


def system_comspec() -> str:
    """cmd.exe, taken from Pythonator's own environment: never from a project's .env file."""
    if comspec := os.environ.get("COMSPEC"):
        return comspec
    return str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe")


def cmd_quote(arg: str, *, force: bool = False) -> str:
    """Quote one argument for a command line that cmd.exe parses before the program sees it.

    Inside double quotes cmd.exe takes ``& | < > ^ ( )`` literally, but still expands
    ``%VAR%``, so each ``%`` becomes ``%%cd:~,%`` (``%cd:~,%`` expands to nothing). A quote
    or a line break can't be passed through cmd.exe safely, so those are refused.
    """
    if any(c in arg for c in '"\r\n\0'):
        raise LaunchError(f"An argument passed through cmd.exe can't contain quotes or line breaks: {arg!r}")
    if not force and _CMD_PLAIN.fullmatch(arg):
        return arg
    trailing = len(arg) - len(arg.rstrip("\\"))
    arg += "\\" * trailing  # so a trailing backslash doesn't escape the closing quote
    return '"' + arg.replace("%", "%%cd:~,%") + '"'


def batch_cmdline(argv: list[str], comspec: str | None = None) -> str:
    """The command line that runs a .bat / .cmd file with ``argv[1:]`` as plain arguments.

    Windows always runs batch files through cmd.exe, which re-parses their arguments: with the
    usual quoting, ``run.bat "a&calc"`` would also start calc ("BatBadBut"). This quotes every
    argument for cmd.exe and turns off delayed ``!VAR!`` expansion and AutoRun commands.
    """
    inner = " ".join([cmd_quote(argv[0], force=True), *(cmd_quote(a) for a in argv[1:])])
    return f'"{comspec or system_comspec()}" /d /v:off /s /c "{inner}"'


# -- argument handling --------------------------------------------------------------------------


def split_args(text: str) -> list[str]:
    """Split a command line the way the user expects on the current platform."""
    text = text.strip()
    if not text:
        return []
    try:
        if not IS_WINDOWS:
            return shlex.split(text)
        parts = shlex.split(text, posix=False)
    except ValueError as exc:
        raise LaunchError(f"Could not parse arguments: {exc}") from exc
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, the way ``.env`` files are written.

    ``#`` starts a comment (at the start of a line, or after whitespace in an unquoted value),
    ``export`` is allowed, quotes are removed and ``\\n`` in double quotes becomes a newline.
    """
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        env[key] = _env_value(value.strip())
    return env


def _env_value(value: str) -> str:
    if value[:1] in ("\"", "'"):
        quote = value[0]
        end = value.find(quote, 1)
        while quote == '"' and end > 0 and value[end - 1] == "\\":
            end = value.find(quote, end + 1)
        if end > 0:
            inner = value[1:end]
            if quote == '"':
                inner = inner.replace("\\n", "\n").replace('\\"', '"')
            return inner
        return value
    for marker in (" #", "\t#"):
        if (cut := value.find(marker)) >= 0:
            value = value[:cut]
    return value.rstrip()


def env_file_path(cfg: ServiceConfig, workdir: Path) -> Path | None:
    name = cfg.env_file.strip().strip('"')
    if not name:
        return None
    path = Path(name).expanduser()
    return path if path.is_absolute() else workdir / path


def load_env_file(cfg: ServiceConfig, workdir: Path) -> dict[str, str]:
    """Variables from the service's ``.env`` file (empty when none is configured)."""
    path = env_file_path(cfg, workdir)
    if path is None:
        return {}
    try:
        return parse_env(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError as exc:
        raise LaunchError(f"Could not read the env file {path}: {exc.strerror or exc}") from exc


def format_env(env: Mapping[str, str]) -> str:
    """The inverse of :func:`parse_env`: quote values that would not survive a round trip."""
    return "\n".join(f"{k}={_quote_env(v)}" for k, v in env.items())


def _quote_env(value: str) -> str:
    plain = value == value.strip() and "\n" not in value and value[:1] not in ("\"", "'")
    if plain and " #" not in value and "\t#" not in value:
        return value
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', '\\"').replace("\n", "\\n") + '"'


def expand_vars(value: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR``, ``${VAR}`` and ``%VAR%`` against ``env``; unknown names are kept."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        return env.get(name, m.group(0))

    return _VAR.sub(sub, value)


# -- interpreters and virtual environments ------------------------------------------------------


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if IS_WINDOWS else "bin")


def find_venv(directory: Path) -> Path | None:
    for name in VENV_NAMES:
        candidate = directory / name
        if (candidate / "pyvenv.cfg").is_file() and venv_python(candidate).exists():
            return candidate
    return None


def venv_of_interpreter(python: Path) -> Path | None:
    """Return the venv root if ``python`` lives inside one."""
    root = python.parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def system_python() -> str | None:
    """A base (non-venv) interpreter to run scripts that have no venv."""
    if not IS_FROZEN:
        base = getattr(sys, "_base_executable", "") or sys.executable
        if base and Path(base).exists():
            return base
    # A frozen build has no Python of its own: use the one a terminal would find (not macOS's
    # /usr/bin/python3 placeholder when Apple's developer tools aren't installed).
    try:
        return find_program(runtime_info(Runtime.PYTHON), "", os.environ)
    except LaunchError:
        return shutil.which("py") if IS_WINDOWS else None


def resolve_workdir(cfg: ServiceConfig) -> Path:
    if cfg.workdir.strip():
        return Path(cfg.workdir.strip()).expanduser()
    if cfg.mode is LaunchMode.SCRIPT and cfg.target.strip():
        return _script_path(cfg, Path.cwd()).parent
    return Path.home()


def _script_path(cfg: ServiceConfig, base: Path) -> Path:
    script = Path(cfg.target.strip().strip('"')).expanduser()
    return script if script.is_absolute() else (base / script)


def resolve_python(cfg: ServiceConfig, workdir: Path) -> tuple[Path, Path | None]:
    """Return ``(interpreter, venv)`` for the service."""
    if cfg.python.strip():
        python = Path(cfg.python.strip().strip('"')).expanduser()
        if not python.exists():
            found = shutil.which(cfg.python.strip())
            if not found:
                raise LaunchError(f"Python interpreter not found: {cfg.python}")
            python = Path(found)
        return python, venv_of_interpreter(python)
    if venv := find_venv(workdir):
        return venv_python(venv), venv
    if base := system_python():
        return Path(base), None
    raise LaunchError("No Python interpreter found. Create a venv or set the interpreter path.")


# -- Node.js ----------------------------------------------------------------------------------

def node_bin_dirs(workdir: Path) -> list[Path]:
    """``node_modules/.bin`` of the working directory and its parents, nearest first (like npm)."""
    dirs: list[Path] = []
    for directory in (workdir, *workdir.parents):
        candidate = directory / "node_modules" / ".bin"
        # Skip folders other users can write to (e.g. /tmp/node_modules): a program planted
        # there would shadow node, npm, git, ... for this service.
        if candidate.is_dir() and trusted_dir(candidate.parent) and trusted_dir(candidate):
            dirs.append(candidate)
    return dirs


def resolve_node(cfg: ServiceConfig, env: Mapping[str, str]) -> Path:
    return Path(find_program(runtime_info(Runtime.NODE), cfg.node, env))


def explicit_interpreter(cfg: ServiceConfig) -> str:
    """The interpreter path the user set for the service's runtime ("" for auto)."""
    if cfg.runtime is Runtime.PYTHON:
        return cfg.python.strip()
    if cfg.runtime is Runtime.NODE:
        return cfg.node.strip()
    return cfg.interpreter.strip()


def _path_of(env: Mapping[str, str]) -> str | None:
    return path_of(env)


def base_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A clean copy of the environment for child processes."""
    env = dict(os.environ if source is None else source)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("__PYVENV_LAUNCHER__", None)
    if IS_FROZEN:
        # PyInstaller rewrites the library path for its own bundle; give children the original.
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            original = env.pop(var + "_ORIG", None)
            if original is not None:
                env[var] = original
            else:
                env.pop(var, None)
    return env


def build_env(
    cfg: ServiceConfig,
    venv: Path | None,
    base: Mapping[str, str] | None = None,
    workdir: Path | None = None,
) -> dict[str, str]:
    env = base_environment(base)
    env.update(
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        FORCE_COLOR="1",
        CLICOLOR_FORCE="1",
        PYTHONATOR="1",
        PYTHONATOR_SERVICE=cfg.name,
    )
    env.setdefault("TERM", "xterm-256color")
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    prepend: list[str] = []
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
        prepend.append(str(venv_bin(venv)))
    if workdir is not None:
        prepend.extend(str(d) for d in node_bin_dirs(workdir))
    explicit = explicit_interpreter(cfg) if cfg.runtime is not Runtime.PYTHON else ""
    if explicit and Path(explicit).expanduser().parent != Path():
        prepend.append(str(Path(explicit).expanduser().parent))  # so npm / bundle / ... use the same version
    if prepend:
        env[path_key] = os.pathsep.join(filter(None, [*prepend, env.get(path_key, "")]))
    # The .env file first, so variables set on the service itself win.
    for source in (load_env_file(cfg, workdir or resolve_workdir(cfg)), cfg.env):
        for key, value in source.items():
            env[key] = expand_vars(value, env)
    return env


# -- commands -----------------------------------------------------------------------------------


def build_launch(cfg: ServiceConfig, base_env: Mapping[str, str] | None = None) -> LaunchSpec:
    """Resolve the command that runs the service itself."""
    workdir = resolve_workdir(cfg)
    if not workdir.is_dir():
        raise LaunchError(f"Working directory does not exist: {workdir}")
    info = runtime_info(cfg.runtime)
    mode = info.modes.get(cfg.mode)
    if mode is None:
        raise LaunchError(f"{info.label} services can't run in {cfg.mode.value} mode. Choose another “Run” option.")
    target = cfg.target.strip()
    if not target:
        what = "script selected" if cfg.mode is LaunchMode.SCRIPT else f"{mode.field.lower()} set"
        raise LaunchError(f"No {what}.")
    args = split_args(cfg.args)

    if cfg.mode is LaunchMode.COMMAND:
        cmd_venv = _python_venv(cfg, workdir)
        env = build_env(cfg, cmd_venv, base_env, workdir)
        argv = split_args(target)
        if not argv:
            raise LaunchError("No command set.")
        argv[0] = _which(argv[0], env, workdir)
        return LaunchSpec([*argv, *args], workdir, env, cmd_venv)

    venv: Path | None = None
    python: Path | None = None
    if cfg.runtime is Runtime.PYTHON:
        python, venv = resolve_python(cfg, workdir)
    env = build_env(cfg, venv, base_env, workdir)

    def find() -> str:
        return str(python) if python is not None else find_program(info, explicit_interpreter(cfg), env)

    if cfg.mode is LaunchMode.SCRIPT:
        script = _script_path(cfg, workdir if cfg.workdir.strip() else Path.cwd())
        if not script.exists():
            raise LaunchError(f"Script not found: {script}")
        target = str(script)
    ctx = Ctx(cfg, workdir, env, target, args, split_options(cfg.runtime_args), find)
    builder = info.script if cfg.mode is LaunchMode.SCRIPT else info.module
    assert builder is not None  # modes without a builder aren't listed in ``info.modes``
    argv = builder(ctx)
    if cfg.runtime is not Runtime.PYTHON:
        _ensure_on_path(env, ctx.resolved_program)
    return LaunchSpec(argv, workdir, env, venv)


def _ensure_on_path(env: dict[str, str], program: str) -> None:
    """Put a runtime found outside PATH (e.g. ~/.bun/bin) on it, so its own tools find it too."""
    if not program:
        return
    folder = str(Path(program).parent)
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    current = env.get(key, "")
    if folder not in current.split(os.pathsep):
        env[key] = os.pathsep.join(filter(None, [current, folder]))


def build_shell(
    cfg: ServiceConfig, command: str, base_env: Mapping[str, str] | None = None, *, shell: Shell | None = None
) -> LaunchSpec:
    """Run an ad-hoc shell command inside the service's environment (venv on PATH).

    ``shell`` is the console's shell (PowerShell, cmd, bash, ...); without one, internal command
    chains run in cmd.exe on Windows and bash / sh elsewhere.
    """
    workdir = resolve_workdir(cfg)
    if not workdir.is_dir():
        raise LaunchError(f"Working directory does not exist: {workdir}")
    venv = _python_venv(cfg, workdir)
    env = build_env(cfg, venv, base_env, workdir)
    # The shell itself comes from Pythonator's own environment, not the project's PATH or .env;
    # the command it runs still sees the project's environment (venv first on PATH).
    if shell is not None:
        argv, cmdline = shell.argv(command)
        return LaunchSpec(argv, workdir, env, venv, shell=True, cmdline=cmdline)
    if IS_WINDOWS:
        comspec = system_comspec()
        argv = [comspec, "/d", "/s", "/c", command]
        # cmd.exe has its own quoting rules: wrap the whole command once and let /s unwrap it.
        return LaunchSpec(argv, workdir, env, venv, shell=True, cmdline=f'"{comspec}" /d /s /c "{command}"')
    argv = [shutil.which("bash") or "/bin/sh", "-c", command]
    return LaunchSpec(argv, workdir, env, venv, shell=True)


def _python_venv(cfg: ServiceConfig, workdir: Path) -> Path | None:
    """The venv to put on PATH for commands and shells (any runtime: mixed projects are common)."""
    if cfg.runtime is Runtime.PYTHON and cfg.python.strip():
        try:
            return resolve_python(cfg, workdir)[1]
        except LaunchError:
            return None
    return find_venv(workdir)


def build_create_venv(cfg: ServiceConfig, base_env: Mapping[str, str] | None = None) -> LaunchSpec:
    workdir = resolve_workdir(cfg)
    if not workdir.is_dir():
        raise LaunchError(f"Working directory does not exist: {workdir}")
    if cfg.runtime is not Runtime.PYTHON:
        raise LaunchError(f"{cfg.runtime.label} services don't use a Python venv. Use “Install dependencies”.")
    if existing := find_venv(workdir):
        raise LaunchError(f"A virtual environment already exists: {existing}")
    creator: str | None = None
    if cfg.python.strip():
        python, venv = resolve_python(cfg, workdir)
        creator = str(python) if venv is None else None
    creator = creator or system_python()
    if not creator:
        raise LaunchError("No Python interpreter found to create the virtual environment.")
    env = build_env(cfg, None, base_env)
    return LaunchSpec([creator, "-m", "venv", str(workdir / ".venv")], workdir, env)


def build_install(
    cfg: ServiceConfig, requirements_file: Path, base_env: Mapping[str, str] | None = None
) -> LaunchSpec:
    """``pip install`` / ``npm install`` the packages typed into the service, or the project's own list."""
    workdir = resolve_workdir(cfg)
    if not workdir.is_dir():
        raise LaunchError(f"Working directory does not exist: {workdir}")
    if cfg.runtime is not Runtime.PYTHON:
        return _build_runtime_install(cfg, workdir, base_env)
    python, venv = resolve_python(cfg, workdir)
    if venv is None:
        raise LaunchError("No virtual environment. Create one first so packages are not "
                          "installed into the system interpreter.")
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if cfg.requirements.strip():
        requirements_file.parent.mkdir(parents=True, exist_ok=True)
        requirements_file.write_text(cfg.requirements.strip() + "\n", encoding="utf-8")
        argv = [*pip, "-r", str(requirements_file)]
    elif (workdir / "requirements.txt").is_file():
        argv = [*pip, "-r", "requirements.txt"]
    elif (workdir / "pyproject.toml").is_file() or (workdir / "setup.py").is_file():
        argv = [*pip, "-e", "."]
    else:
        raise LaunchError("Nothing to install: add requirements in the service settings or a "
                          "requirements.txt to the working directory.")
    return LaunchSpec(argv, workdir, build_env(cfg, venv, base_env, workdir), venv)


def _build_runtime_install(cfg: ServiceConfig, workdir: Path, base_env: Mapping[str, str] | None) -> LaunchSpec:
    info = runtime_info(cfg.runtime)
    if info.install is None:
        raise LaunchError(info.no_install or f"{info.label} has no package manager to run.")
    env = build_env(cfg, None, base_env, workdir)
    packages = [ln.strip() for ln in cfg.requirements.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    ctx = Ctx(cfg, workdir, env, "", [], [], lambda: find_program(info, explicit_interpreter(cfg), env))
    commands = info.install(ctx, packages)
    _ensure_on_path(env, ctx.resolved_program)
    if len(commands) == 1:
        return LaunchSpec(commands[0], workdir, env)
    # Several commands (e.g. one `dotnet add package` per package): chain them in a shell, quoted
    # for that shell so a package name can never become a command of its own.
    join = _cmd_join if IS_WINDOWS else shlex.join
    spec = build_shell(cfg, " && ".join(join(c) for c in commands), base_env)
    return LaunchSpec(spec.argv, spec.cwd, env, None, shell=True, cmdline=spec.cmdline)


def _cmd_join(argv: list[str]) -> str:
    return " ".join(cmd_quote(a, force=i == 0) for i, a in enumerate(argv))


def _which(program: str, env: Mapping[str, str], cwd: Path) -> str:
    candidate = Path(program).expanduser()
    if candidate.parent != Path():
        resolved = candidate if candidate.is_absolute() else cwd / candidate
        if resolved.exists():
            return str(resolved)
        raise LaunchError(f"Program not found: {program}")
    found = shutil.which(program, path=_path_of(env))
    if not found:
        raise LaunchError(f"'{program}' was not found on PATH (venv and node_modules/.bin included).")
    return found
