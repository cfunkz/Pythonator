"""The service spec: what you want a service to be. The reconciler makes reality match it."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pythonator.policy.paths import is_absolute

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
GROUP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,47}")
HOST_RE = re.compile(r"^(?=.{1,253}$)(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Variables a service may never set: they change how the runtime or the loader behaves.
FORBIDDEN_ENV = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "NODE_OPTIONS",
        "NODE_PATH",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "PATH",
        "HOME",
        "IFS",
        # host-side names: the engine CLI, the Windows jail launcher and wsl.exe start with the service's variables
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "GCONV_PATH",
        "LOCPATH",
        "NLSPATH",
        "WSLENV",
        "SSH_AUTH_SOCK",
        "MYSQL_PWD",
    }
)
# Prefixes a service may never set, for the same reason (DOCKER_HOST would point the engine CLI somewhere else).
FORBIDDEN_PREFIXES = ("LD_", "DYLD_", "DOCKER_", "BUILDKIT_", "CONTAINER_", "CONTAINERS_", "PODMAN_", "XDG_",
                      "PYTHONATOR", "WSL_", "COMPOSE_", "BUILDX_", "BAKE_", "SSH_", "GPG_", "GIT_", "_PYI_", "_MEI")

def bad_text(v: str, *, lines: bool = False) -> bool:
    """NUL, control characters and invisible format characters (bidi overrides, zero-width): they fake console
    lines and hide what an argument really is. Values may hold tabs and line breaks (a PEM key); arguments may not."""
    import unicodedata

    ok = "\t\n\r" if lines else ""
    return any((unicodedata.category(c) in ("Cc", "Cf") and c not in ok) for c in v)

def forbidden_env(k: str) -> bool:
    u = k.upper()
    return u in FORBIDDEN_ENV or u.startswith(FORBIDDEN_PREFIXES)

class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

class Mode(StrEnum):
    SANDBOX = "sandbox"
    CONTAINER = "container"
    WSL = "wsl"
    FULL = "full"

class Desired(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"

class Reach(StrEnum):
    THIS_COMPUTER = "this-computer"
    LAN = "lan"

class Traffic(Strict):
    """What a service may reach. `hosts` is used only when mode is "hosts"."""

    mode: Literal["off", "hosts", "any"] = "off"
    hosts: tuple[str, ...] = ()
    allow_lan: bool = False

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        out = []
        for h in v:
            host, _, port = h.lower().partition(":")
            if not HOST_RE.fullmatch(host):
                raise ValueError(f"not a host name: {h!r}")
            if port and not (port.isdigit() and 0 < int(port) < 65536):
                raise ValueError(f"bad port in {h!r}")
            out.append(h.lower())
        if len(out) > 256:
            raise ValueError("at most 256 hosts")
        return tuple(sorted(set(out)))

class PortRule(Strict):
    port: int = Field(ge=1024, le=65535)
    reach: Reach = Reach.THIS_COMPUTER

class Network(Strict):
    traffic: Traffic = Traffic()
    ports: tuple[PortRule, ...] = ()

    @field_validator("ports")
    @classmethod
    def _unique(cls, v: tuple[PortRule, ...]) -> tuple[PortRule, ...]:
        if len({p.port for p in v}) != len(v):
            raise ValueError("each port once")
        return tuple(sorted(v, key=lambda p: p.port))

class Limits(Strict):
    memory_mb: int = Field(default=256, ge=16, le=262_144)
    cpu: float = Field(default=1.0, gt=0, le=256)
    tasks: int = Field(default=128, ge=4, le=65_536)

class Restart(Strict):
    policy: Literal["always", "on-failure", "never"] = "on-failure"
    backoff_s: float = Field(default=2.0, ge=0.5, le=300)
    max_backoff_s: float = Field(default=300.0, ge=1, le=3600)
    give_up_after: int = Field(default=5, ge=1, le=1000)
    stable_after_s: float = Field(default=30.0, ge=1, le=3600)

class Health(Strict):
    """Is it really working? A TCP connect or an HTTP GET to one of its own open ports, from this computer."""

    kind: Literal["off", "tcp", "http"] = "off"
    port: int = Field(default=0, ge=0, le=65535)
    path: str = Field(default="/", max_length=512)
    every_s: float = Field(default=10.0, ge=2, le=3600)
    timeout_s: float = Field(default=3.0, ge=0.5, le=60)
    fails: int = Field(default=3, ge=1, le=100)  # this many in a row: restart it (listed under Healed today)
    grace_s: float = Field(default=20.0, ge=0, le=600)  # after a start, before the first check

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or any(c in v for c in "\r\n\x00 "):
            raise ValueError("the path starts with / and has no spaces")
        return v

class Schedule(Strict):
    cron: str
    catch_up: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        from datetime import datetime

        from cronsim import CronSim, CronSimError  # small, imported only when a schedule exists

        if len(v.split()) != 5:  # a sixth field meant "every second" to cronsim
            raise ValueError("cron: exactly five fields (minute hour day month weekday)")
        try:
            CronSim(v, datetime(2026, 1, 1))
        except CronSimError as e:
            raise ValueError(f"cron: {e}") from None
        return v

class FolderGrant(Strict):
    path: str
    write: bool = False

    @field_validator("path")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not is_absolute(v):  # checked as text: a spec for Linux is valid when read on Windows and back
            raise ValueError("folder must be an absolute path")
        return v

# An OCI image reference: [registry[:port]/]path[:tag][@sha256:digest]. Nothing that starts with '-' (an option).
IMAGE_RE = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?/)?[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?$"
)

class ContainerOpts(Strict):
    image: str = Field(max_length=512)
    engine: Literal["podman", "docker", "auto"] = "auto"

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        if not IMAGE_RE.fullmatch(v):
            raise ValueError("not an image reference (for example python:3.13-slim or redis:7@sha256:…)")
        return v

ServiceName = Annotated[str, Field(min_length=1, max_length=48)]

class ServiceSpec(Strict):
    """One service. Saved as `services/<name>.json`; every save is a new generation."""

    name: ServiceName
    folder: str
    command: tuple[str, ...] = Field(min_length=1, max_length=256)
    runtime: Literal["python", "node", "bun", "deno", "go", "java", "dotnet", "rust", "ruby", "powershell", "binary"] = "python"
    runtime_version: str = ""
    mode: Mode = Mode.SANDBOX
    desired: Desired = Desired.STOPPED
    network: Network = Network()
    limits: Limits = Limits()
    restart: Restart = Restart()
    schedule: Schedule | None = None
    health: Health = Health()
    env: dict[str, str] = Field(default_factory=dict)
    secrets: tuple[str, ...] = ()
    folders: tuple[FolderGrant, ...] = ()
    container: ContainerOpts | None = None
    group: str = Field(default="", max_length=48)
    gui: bool = False  # it opens windows (a desktop app): see compile._display
    generation: int = Field(default=1, ge=1)

    @field_validator("group")
    @classmethod
    def _group(cls, v: str) -> str:
        if v and not GROUP_RE.fullmatch(v):
            raise ValueError("a group is letters, digits, spaces, '-' and '_'")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.fullmatch(v):
            raise ValueError("lowercase letters, digits and '-' only, starting with a letter or digit")
        return v

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        if not is_absolute(v):  # checked as text: a spec for Linux is valid when read on Windows and back
            raise ValueError("folder must be an absolute path")
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for a in v:
            if "\x00" in a or len(a) > 4096 or bad_text(a):
                raise ValueError("bad argument: no control or invisible characters")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 256:
            raise ValueError("at most 256 variables")
        for k, val in v.items():
            if not ENV_KEY_RE.fullmatch(k):
                raise ValueError(f"bad variable name {k!r}")
            if forbidden_env(k):
                raise ValueError(f"{k} can't be set for a service")
            if "\x00" in val or len(val) > 32_768 or bad_text(val, lines=True):
                raise ValueError(f"bad value for {k}")
        return v

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for k in v:
            if not ENV_KEY_RE.fullmatch(k) or forbidden_env(k):
                raise ValueError(f"bad secret name {k!r}")
        return tuple(sorted(set(v)))

    @model_validator(mode="after")
    def _consistent(self) -> ServiceSpec:
        if self.mode is Mode.CONTAINER and self.container is None:
            raise ValueError("container mode needs an image")
        if self.mode is not Mode.CONTAINER and self.container is not None:
            raise ValueError("an image is only used in container mode")
        if self.health.kind != "off" and self.health.port not in {p.port for p in self.network.ports}:
            raise ValueError("the health check uses one of the service's open ports")
        overlap = set(self.env) & set(self.secrets)
        if overlap:
            raise ValueError(f"{sorted(overlap)[0]} is both a variable and a secret")
        return self

    def evolve(self, **changes: object) -> ServiceSpec:
        """A copy with changes, validated again (frozen models are never edited in place)."""
        data = self.model_dump()
        data.update(changes)
        return ServiceSpec.model_validate(data, strict=False)
