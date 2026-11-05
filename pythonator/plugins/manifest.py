"""``plugin.toml``: what a plugin is, what it asks for, and the settings form it wants."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from ..core import validate

API = 2  # 2: chat input, select and code in panels; read_files and propose_changes
MANIFEST = "plugin.toml"
Permission = Literal["network", "read_logs", "inspect", "control", "policy", "ui", "read_files", "propose_changes"]
PERMISSIONS: dict[str, str] = {
    "network": "Use the internet and your local network",
    "read_logs": "Read your services' output (it may contain things they print, such as addresses)",
    "inspect": "See ports, connections, memory and CPU of your services",
    "control": "Start, stop and restart your services",
    "policy": "Block services from starting",
    "ui": "Show a panel in the window",
    "read_files": "Read your services' files, read-only (never .env files, keys or other secrets)",
    "propose_changes": "Suggest changes to your services' files; nothing is written until you review and apply it",
}
Text = Annotated[str, AfterValidator(lambda v: validate.arg(v))]


class Setting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
    label: Annotated[Text, Field(max_length=80)]
    kind: Literal["text", "secret", "bool", "int", "choice", "lines"] = "text"
    default: str | int | bool = ""
    choices: list[Annotated[Text, Field(max_length=80)]] = Field(default_factory=list, max_length=50)
    help: Annotated[Text, Field(max_length=400)] = ""


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")]
    name: Annotated[str, AfterValidator(validate.name)]
    version: Annotated[str, Field(pattern=r"^\d+(\.\d+){0,3}([.-]?[a-z0-9]+)?$", max_length=32)]
    api: int = API
    entry: Annotated[str, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,120}\.py$")] = "plugin.py"
    description: Annotated[Text, Field(max_length=300)] = ""
    permissions: list[Permission] = Field(default_factory=list, max_length=8)
    settings: list[Setting] = Field(default_factory=list, max_length=40)
    homepage: Annotated[Text, Field(max_length=300)] = ""
    author: Annotated[Text, Field(max_length=100)] = ""


def load(folder: Path) -> Manifest:
    """Read and validate ``folder/plugin.toml``. Nothing in the plugin is imported. Raises ValueError."""
    path = folder / MANIFEST
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError("plugin.toml is larger than 64 KB")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Can't read {path}: {exc}") from None
    meta = data.get("plugin", data)
    api = meta.get("api", API) if isinstance(meta, dict) else API
    if isinstance(api, int) and api > API:  # before validating: a newer plugin may ask for permissions we don't know
        raise ValueError(f"{meta.get('name', folder.name)} needs a newer Pythonator (plugin API {api}, this one has "
                         f"{API})")
    try:
        manifest = Manifest.model_validate(meta)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValueError(f"{path}: {'.'.join(map(str, first['loc']))}: {first['msg']}") from None
    if manifest.api > API:
        raise ValueError(f"{manifest.name} needs a newer Pythonator (plugin API {manifest.api})")
    entry = (folder / manifest.entry).resolve()
    if not entry.is_relative_to(folder.resolve()) or not entry.is_file():
        raise ValueError(f"{manifest.name}: entry {manifest.entry} isn't a file in the plugin")
    return manifest
