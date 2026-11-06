"""The window ↔ background service protocol: one JSON object per line, every request validated.

Requests are a tagged union on ``op``; unknown ops and unknown fields are refused. Events from the service are
plain dicts with an ``ev`` key (they come from trusted code; the window still treats them as data).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ..core.models import Service, Settings

PROTOCOL = 1
MAX_FRAME = 1024 * 1024  # bytes per line
MAX_QUEUED = 8 * 1024 * 1024  # a window this far behind gets only the newest output
MAX_BACKLOG = 64 * 1024 * 1024  # and this far behind is disconnected


class Req(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    req: int = 0  # echoed in the reply


class Hello(Req):
    op: Literal["hello"]
    token: str = Field(max_length=128)
    protocol: int = PROTOCOL


class Ident(Req):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class Add(Req):
    op: Literal["add"]
    service: Service


class Update(Req):
    op: Literal["update"]
    service: Service


class Remove(Ident):
    op: Literal["remove"]


class Start(Ident):
    op: Literal["start"]
    confirmed: bool = False  # the user confirmed Full access


class Stop(Ident):
    op: Literal["stop"]


class Restart(Ident):
    op: Literal["restart"]


class Input(Ident):
    op: Literal["input"]
    text: str = Field(max_length=65_536)


class Shell(Ident):
    """A command typed into the console's shell (cmd.exe on Windows, sh elsewhere), run in the service's
    isolation; while one runs, further lines go to its input."""

    op: Literal["shell"]
    line: str = Field(min_length=1, max_length=8192, pattern=r"^[^\r\n\x00]*$")


class Install(Ident):
    op: Literal["install"]


class Recreate(Ident):
    op: Literal["recreate"]


class Pull(Ident):
    op: Literal["pull"]


class Compose(Ident):
    """A Docker Compose project's actions: build or pull its images, down, or list its containers."""

    op: Literal["compose"]
    action: Literal["build", "pull", "down", "ps"]


class Tail(Ident):
    op: Literal["tail"]


class Clear(Ident):
    op: Literal["clear"]


class Watch(Req):
    """Send this connection the output of one service only ("" for none). Without it, every service's output."""

    op: Literal["watch"]
    id: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]{0,62})?$")


class Check(Ident):
    op: Literal["check"]  # the environment check of one service


class Reorder(Req):
    op: Literal["reorder"]
    ids: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]] = Field(max_length=10_000)


class StartAll(Req):
    """Start every service, or only these, in list order and as many at a time as the settings say."""

    op: Literal["start_all"]
    ids: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]] | None = Field(default=None,
                                                                                         max_length=10_000)


class SaveSettings(Req):
    op: Literal["settings"]
    settings: Settings


class Simple(Req):
    op: Literal["stop_all", "snapshot", "capabilities", "ping"]


class Shutdown(Req):
    op: Literal["shutdown"]
    stop_services: bool = True


class PluginOp(Req):
    plugin: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")


class PluginEnable(PluginOp):
    op: Literal["plugin_enable"]
    enabled: bool


class PluginSettings(PluginOp):
    op: Literal["plugin_settings"]
    values: dict[str, str | int | bool] = Field(max_length=64)


class PluginGrant(PluginOp):
    op: Literal["plugin_grant"]
    permissions: list[str] = Field(max_length=16)
    live: bool = False  # leave test mode


class PluginAction(PluginOp):
    op: Literal["plugin_action"]
    action: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    value: str = Field(default="", max_length=16_000)  # a panel's text box


class PluginProposal(PluginOp):
    """The user's answer to a change a plugin proposed (the review window)."""

    op: Literal["plugin_proposal"]
    proposal: str = Field(pattern=r"^[0-9a-f]{16}$")
    apply: bool


class PluginInstall(Req):
    op: Literal["plugin_install"]
    source: str = Field(max_length=4096)  # a folder or an https git URL


class PluginRemove(PluginOp):
    op: Literal["plugin_remove"]


Request = Annotated[
    Hello | Add | Update | Remove | Start | Stop | Restart | Input | Shell | Install | Recreate | Pull | Compose | Tail
    | Clear
    | Watch
    | Check
    | Reorder | StartAll | SaveSettings | Simple | Shutdown | PluginEnable | PluginSettings | PluginGrant | PluginAction
    | PluginProposal | PluginInstall | PluginRemove,
    Field(discriminator="op"),
]
_adapter: TypeAdapter[Any] = TypeAdapter(Request)


def parse(line: bytes) -> Req:
    """One request line → a validated request. Raises ValueError for anything else."""
    if len(line) > MAX_FRAME:
        raise ValueError("message too large")
    try:
        result: Req = _adapter.validate_json(line)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first["loc"][1:]) or "request"
        raise ValueError(f"Invalid {where}: {first['msg']}") from None
    except ValueError as exc:
        raise ValueError(f"Invalid request: {str(exc).splitlines()[0]}") from None
    return result


def encode(message: dict[str, Any] | BaseModel) -> bytes:
    if isinstance(message, BaseModel):
        return message.model_dump_json().encode() + b"\n"
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
