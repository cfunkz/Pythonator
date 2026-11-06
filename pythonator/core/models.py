"""The typed, validated data Pythonator stores: services and app settings.

Every field is checked by :mod:`.validate` when a model is built, whether from the UI, the services file or
a message from the window, so a hand-edited or damaged file can't smuggle anything past the checks.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from . import validate
from .schedule import check as check_schedule


class Mode(StrEnum):
    """How much of the computer a service may use."""

    FULL = "full"  # runs as you, like in a terminal
    SANDBOX = "sandbox"  # the operating system's own sandbox: its folder, its runtime, nothing else
    CONTAINER = "container"  # a Docker or Podman container


class Expose(StrEnum):
    """Who may connect to the ports a service serves on."""

    LOCAL = "local"  # programs on this computer, the browser included
    LAN = "lan"  # also other computers on a home or work network


class Restart(StrEnum):
    NEVER = "never"
    ON_FAILURE = "on-failure"
    ALWAYS = "always"


class Model(BaseModel):
    # Stored files may come from newer versions: unknown fields are dropped, bad values refused.
    model_config = ConfigDict(extra="ignore", validate_assignment=True, frozen=False)


Name = Annotated[str, AfterValidator(validate.name)]
Ports = Annotated[list[int], AfterValidator(validate.ports)]
Args = Annotated[list[str], AfterValidator(validate.args)]
Env = Annotated[dict[str, str], AfterValidator(validate.env)]


class Network(Model):
    enabled: bool = True  # may use the network at all (sandbox and container modes)
    ports: Ports = Field(default_factory=list)  # the TCP ports it serves on; empty: any (Full) or none published
    expose: Expose = Expose.LOCAL


class FolderGrant(Model):
    path: Annotated[str, AfterValidator(lambda v: validate.folder(v, must_exist=False))]
    write: bool = False


class SandboxOptions(Model):
    write_project: bool = True  # may change files in its own folder
    folders: list[FolderGrant] = Field(default_factory=list, max_length=32)


class ContainerOptions(Model):
    engine: Literal["auto", "docker", "podman"] = "auto"
    image: Annotated[str, AfterValidator(validate.image)] = ""  # empty: the runtime's default image
    network: Annotated[str, AfterValidator(validate.network_name)] = ""  # join this network (to reach a database)
    read_only_root: bool = True  # the image itself can't be changed, only the project folder


class GitSource(Model):
    url: Annotated[str, AfterValidator(validate.git_url)]
    branch: Annotated[str, AfterValidator(validate.git_ref)] = ""
    pull_on_start: bool = False


def new_id() -> str:
    return secrets.token_hex(6)


class Service(Model):
    id: Annotated[str, AfterValidator(validate.ident)] = Field(default_factory=new_id)
    name: Name
    folder: Annotated[str, AfterValidator(lambda v: validate.folder(v, "The working folder", must_exist=False))]
    runtime: str = "python"  # a key of runtimes.RUNTIMES, or "custom" (``command`` runs as is)
    entry: str = ""  # the file to run, relative to ``folder``
    command: Args = Field(default_factory=list)  # runtime "custom": the full command line
    args: Args = Field(default_factory=list)
    env: Env = Field(default_factory=dict)
    env_file: str = ""  # e.g. ".env" in the project folder
    # Its own packages (a .venv, node_modules, a private Go/Deno/NuGet/Cargo cache), made on Start if missing,
    # so it never runs on the computer's global installs.
    own_env: bool = True
    mode: Mode = Mode.SANDBOX
    network: Network = Field(default_factory=Network)
    sandbox: SandboxOptions = Field(default_factory=SandboxOptions)
    container: ContainerOptions = Field(default_factory=ContainerOptions)
    restart: Restart = Restart.ON_FAILURE
    max_restarts: int = Field(default=5, ge=0, le=1000)  # automatic restarts within 10 minutes; 0: unlimited
    stop_timeout: float = Field(default=10.0, ge=0.5, le=600)
    autostart: bool = False
    memory_mb: int = Field(default=0, ge=0, le=1_048_576)  # 0: no limit
    cpu_percent: int = Field(default=0, ge=0, le=102_400)  # % of one core for the whole tree; 0: no limit
    git: GitSource | None = None
    schedule: Annotated[str, AfterValidator(check_schedule)] = ""  # "every 15m" or cron; empty: keep it running
    full_access_ok: bool = False  # the user confirmed Full mode for this service

    @model_validator(mode="after")
    def _consistent(self) -> Service:
        from .runtimes import CUSTOM, RUNTIMES

        if self.runtime != CUSTOM and self.runtime not in RUNTIMES:
            raise ValueError(f"Unknown runtime {self.runtime!r}")
        if self.runtime == CUSTOM and not self.command:
            raise ValueError("A custom command can't be empty")
        if self.runtime == "compose" and self.mode is not Mode.CONTAINER:
            raise ValueError("A Docker Compose project runs in Container mode (Isolation)")
        if self.entry:
            validate.arg(self.entry)
            if self.entry.startswith("-"):
                raise ValueError("The entry file can't start with '-'")
        if self.env_file:
            validate.arg(self.env_file)
        return self


class Settings(Model):
    theme: Literal["dark", "light", "system"] = "dark"
    editor_font_size: int = Field(default=11, ge=6, le=40)
    console_lines: int = Field(default=20_000, ge=1_000, le=500_000)
    log_max_mb: int = Field(default=10, ge=1, le=1024)
    log_backups: int = Field(default=3, ge=0, le=50)
    default_mode: Mode = Mode.SANDBOX
    start_at_once: int = Field(default=1, ge=0, le=64)  # Start all and login, in list order: how many together, 0 all
    tray: bool = False  # closing the window leaves Pythonator in the tray
    plugins_enabled: list[str] = Field(default_factory=lambda: ["guard", "notify"])
    plugin_settings: dict[str, dict[str, str | int | bool]] = Field(default_factory=dict)
    plugin_grants: dict[str, list[str]] = Field(default_factory=dict)  # third-party: permissions granted
    plugin_live: list[str] = Field(default_factory=list)  # third-party plugins taken out of test mode
    window_geometry: str = Field(default="", max_length=8192)
    window_state: str = Field(default="", max_length=65_536)  # where the plugin panels are (QMainWindow.saveState)
    panels_locked: bool = False  # plugin panels can't be dragged around or opened in windows of their own


class ServiceFile(Model):
    version: int = 1
    services: list[Service] = Field(default_factory=list)
