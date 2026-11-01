"""Typed configuration models and their (tolerant) JSON serialisation."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any, ClassVar, Self, TypeVar


class Runtime(StrEnum):
    """The language / tool a service runs on. How each one launches lives in ``core.runtimes``."""

    PYTHON = "python"
    NODE = "node"
    BUN = "bun"
    DENO = "deno"
    GO = "go"
    JAVA = "java"
    DOTNET = "dotnet"
    RUBY = "ruby"
    PHP = "php"
    SHELL = "shell"
    POWERSHELL = "powershell"
    EXECUTABLE = "executable"

    @property
    def label(self) -> str:
        return _RUNTIME_LABELS[self]


_RUNTIME_LABELS = {
    Runtime.PYTHON: "Python",
    Runtime.NODE: "Node.js",
    Runtime.BUN: "Bun",
    Runtime.DENO: "Deno",
    Runtime.GO: "Go",
    Runtime.JAVA: "Java",
    Runtime.DOTNET: ".NET",
    Runtime.RUBY: "Ruby",
    Runtime.PHP: "PHP",
    Runtime.SHELL: "Shell script",
    Runtime.POWERSHELL: "PowerShell",
    Runtime.EXECUTABLE: "Program (any executable)",
}

NODE_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
# Which runtime a dropped / chosen file belongs to. Bun and Deno share Node's extensions, so
# they're picked in the service settings instead.
EXTENSION_RUNTIMES: dict[str, Runtime] = {
    ".py": Runtime.PYTHON, ".pyw": Runtime.PYTHON,
    **dict.fromkeys(NODE_EXTENSIONS, Runtime.NODE),
    ".go": Runtime.GO,
    ".jar": Runtime.JAVA, ".java": Runtime.JAVA,
    ".cs": Runtime.DOTNET, ".csproj": Runtime.DOTNET, ".dll": Runtime.DOTNET,
    ".rb": Runtime.RUBY,
    ".php": Runtime.PHP,
    ".sh": Runtime.SHELL, ".bash": Runtime.SHELL, ".command": Runtime.SHELL,
    ".bat": Runtime.SHELL, ".cmd": Runtime.SHELL,
    ".ps1": Runtime.POWERSHELL,
    ".exe": Runtime.EXECUTABLE, ".appimage": Runtime.EXECUTABLE, ".bin": Runtime.EXECUTABLE,
    ".out": Runtime.EXECUTABLE, ".app": Runtime.EXECUTABLE,
}
SCRIPT_EXTENSIONS = tuple(EXTENSION_RUNTIMES)


def runtime_for_path(path: str) -> Runtime:
    lower = path.lower()
    dot = lower.rfind(".")
    sep = max(lower.rfind("/"), lower.rfind("\\"))
    return EXTENSION_RUNTIMES.get(lower[dot:], Runtime.PYTHON) if dot > sep else Runtime.PYTHON


class LaunchMode(StrEnum):
    SCRIPT = "script"  # a file run by the runtime: python -u bot.py, node bot.js, go run main.go ...
    MODULE = "module"  # the runtime's "named entry point": python -m x, npm run x, deno task x ...
    COMMAND = "command"  # any executable, resolved against the environment's PATH


class RestartPolicy(StrEnum):
    NEVER = "never"
    ON_FAILURE = "on-failure"
    ALWAYS = "always"

    @property
    def label(self) -> str:
        return {
            RestartPolicy.NEVER: "Never",
            RestartPolicy.ON_FAILURE: "On failure (non-zero exit)",
            RestartPolicy.ALWAYS: "Always",
        }[self]


class ConsoleShell(StrEnum):
    """The shell behind the console's $ mode (see core/shells.py)."""

    AUTO = "auto"  # Windows: PowerShell 7, else Windows PowerShell. Elsewhere: bash, else sh
    POWERSHELL = "powershell"
    CMD = "cmd"  # Windows only
    BASH = "bash"  # Git Bash on Windows


class CloseAction(StrEnum):
    ASK = "ask"
    TRAY = "tray"
    QUIT = "quit"


_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def valid_id(value: str) -> bool:
    """Ids end up in file names (logs, package lists), so only letters, digits, ``-`` and ``_``."""
    return bool(_ID.fullmatch(value))


@dataclass(slots=True)
class ServiceConfig:
    """Everything needed to launch and supervise one long-running process."""

    name: str
    id: str = field(default_factory=new_id)
    runtime: Runtime = Runtime.PYTHON
    mode: LaunchMode = LaunchMode.SCRIPT
    target: str = ""  # script path, module / npm script name or command line, depending on ``mode``
    args: str = ""
    workdir: str = ""  # empty: the script's folder (script mode) or the home folder
    python: str = ""  # empty: auto-detect (.venv in workdir, then the system interpreter)
    node: str = ""  # empty: ``node`` from PATH
    interpreter: str = ""  # other runtimes: path to go / java / ruby / ...; empty: found on PATH
    runtime_args: str = ""  # options for the interpreter itself, e.g. -Xmx2G or --max-old-space-size=4096
    env: dict[str, str] = field(default_factory=dict)
    env_file: str = ""  # e.g. ".env", relative to the working directory; empty: none
    requirements: str = ""  # empty: requirements.txt / package.json in the working directory
    restart: RestartPolicy = RestartPolicy.ON_FAILURE
    autostart: bool = False
    stop_timeout: float = 10.0
    schedule: str = ""  # "every 15m", "09:00, 18:30" or a cron expression; empty: none
    install_confirmed: bool = False  # the user agreed to install this project's packages without asking again

    def copy(self, **changes: Any) -> ServiceConfig:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "runtime": self.runtime.value,
            "mode": self.mode.value,
            "target": self.target,
            "args": self.args,
            "workdir": self.workdir,
            "python": self.python,
            "node": self.node,
            "interpreter": self.interpreter,
            "runtime_args": self.runtime_args,
            "env": dict(self.env),
            "env_file": self.env_file,
            "requirements": self.requirements,
            "restart": self.restart.value,
            "autostart": self.autostart,
            "stop_timeout": self.stop_timeout,
            "schedule": self.schedule,
            "install_confirmed": self.install_confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        env_raw = data.get("env")
        env = (
            {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
        )
        raw_id = _str(data.get("id"))
        return cls(
            name=_str(data.get("name")) or "service",
            id=raw_id if valid_id(raw_id) else new_id(),
            runtime=_enum(Runtime, data.get("runtime"), Runtime.PYTHON),
            mode=_enum(LaunchMode, data.get("mode"), LaunchMode.SCRIPT),
            target=_str(data.get("target")),
            args=_str(data.get("args")),
            workdir=_str(data.get("workdir")),
            python=_str(data.get("python")),
            node=_str(data.get("node")),
            interpreter=_str(data.get("interpreter")),
            runtime_args=_str(data.get("runtime_args")),
            env=env,
            env_file=_str(data.get("env_file")),
            requirements=_str(data.get("requirements")),
            restart=_enum(RestartPolicy, data.get("restart"), RestartPolicy.ON_FAILURE),
            autostart=bool(data.get("autostart", False)),
            stop_timeout=_float(data.get("stop_timeout"), 10.0, lo=0.5, hi=600.0),
            schedule=_str(data.get("schedule")),
            install_confirmed=data.get("install_confirmed") is True,
        )


@dataclass(slots=True)
class AppSettings:
    console_lines: int = 10_000  # lines kept in memory / shown per service
    log_max_mb: int = 10  # rotate a service log once it reaches this size
    log_backups: int = 3  # rotated files kept per service
    show_timestamps: bool = True
    wrap_lines: bool = False
    close_action: CloseAction = CloseAction.ASK
    allow_elevated: bool = False  # don't warn when started as root / Administrator
    console_shell: ConsoleShell = ConsoleShell.AUTO
    window_geometry: str = ""
    splitter_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.value if isinstance(value, StrEnum) else value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        d = cls()
        return cls(
            console_lines=_int(data.get("console_lines"), d.console_lines, lo=500, hi=1_000_000),
            log_max_mb=_int(data.get("log_max_mb"), d.log_max_mb, lo=1, hi=10_000),
            log_backups=_int(data.get("log_backups"), d.log_backups, lo=0, hi=100),
            show_timestamps=bool(data.get("show_timestamps", d.show_timestamps)),
            wrap_lines=bool(data.get("wrap_lines", d.wrap_lines)),
            close_action=_enum(CloseAction, data.get("close_action"), d.close_action),
            allow_elevated=data.get("allow_elevated") is True,
            console_shell=_enum(ConsoleShell, data.get("console_shell"), d.console_shell),
            window_geometry=_str(data.get("window_geometry")),
            splitter_state=_str(data.get("splitter_state")),
        )


@dataclass(slots=True)
class AppConfig:
    VERSION: ClassVar[int] = 2

    services: list[ServiceConfig] = field(default_factory=list)
    settings: AppSettings = field(default_factory=AppSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "settings": self.settings.to_dict(),
            "services": [s.to_dict() for s in self.services],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        raw_services = data.get("services")
        services: list[ServiceConfig] = []
        seen: set[str] = set()
        if isinstance(raw_services, list):
            for item in raw_services:
                if not isinstance(item, dict):
                    continue
                svc = ServiceConfig.from_dict(item)
                if svc.id in seen:
                    svc.id = new_id()
                seen.add(svc.id)
                services.append(svc)
        raw_settings = data.get("settings")
        settings = AppSettings.from_dict(raw_settings if isinstance(raw_settings, dict) else {})
        return cls(services=services, settings=settings)


# -- coercion helpers -------------------------------------------------------------------------


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object, default: int, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(lo, min(hi, int(value)))


def _float(value: object, default: float, *, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(lo, min(hi, float(value)))


_E = TypeVar("_E", bound=StrEnum)


def _enum(kind: type[_E], value: object, default: _E) -> _E:
    if not isinstance(value, str):
        return default
    try:
        return kind(value)
    except ValueError:
        return default
