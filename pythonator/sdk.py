"""The Pythonator plugin SDK: everything a plugin imports. Standard library only.

A plugin is a folder with a ``plugin.toml`` and a Python file defining one :class:`Plugin` subclass::

    from pythonator.sdk import Plugin, ServiceEvent

    class Hello(Plugin):
        def on_event(self, event: ServiceEvent) -> None:
            if event.kind == "crashed":
                self.notify(f"{event.service.name} crashed")

Every plugin runs in a process of its own, inside the same sandbox services use, and talks to Pythonator only
through the methods below. Each method that acts needs the matching permission in ``plugin.toml``; without it the
call is refused (and in test mode, actions are recorded instead of carried out).
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Plugin", "PluginError", "ServiceEvent", "ServiceInfo", "SystemInfo", "ui"]


class PluginError(Exception):
    """Pythonator refused a request (``read_file``, ``propose_change``...); the message says why."""


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """A read-only view of a service. Never its environment variables or files."""

    id: str
    name: str
    runtime: str
    mode: str  # "full", "sandbox" or "container"
    state: str  # "stopped", "starting", "running", "stopping", "waiting", "failed", "task"
    network: bool = True
    ports: tuple[int, ...] = ()  # the ports it is set up to serve on
    expose: str = "local"  # "local" (this computer) or "lan"
    memory_limit_mb: int = 0
    cpu_limit: int = 0  # percent of one core for the whole tree, 0: none
    restarts: int = 0
    # With the "inspect" permission:
    cpu: float = 0.0
    memory: int = 0  # bytes, whole process tree
    listening: tuple[int, ...] = ()  # ports it actually listens on
    remote: tuple[str, ...] = ()  # "address:port" it is connected to

    @classmethod
    def of(cls, data: dict[str, Any]) -> ServiceInfo:
        names = set(cls.__dataclass_fields__)
        clean = {k: (tuple(v) if isinstance(v, list) else v) for k, v in data.items() if k in names}
        return cls(**clean)  # type: ignore[arg-type]  # the host sends what it documents


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """The whole computer, refreshed with ``on_stats`` (``inspect``)."""

    cpu: float = 0.0  # percent of all cores together (0-100)
    memory: float = 0.0  # percent of memory in use (0-100)
    memory_total: int = 0  # bytes
    cores: int = 1  # logical cores; a service's ``cpu`` can reach cores * 100

    @classmethod
    def of(cls, data: dict[str, Any]) -> SystemInfo:
        try:
            return cls(float(data.get("cpu", 0)), float(data.get("memory", 0)), int(data.get("memory_total", 0)),
                       max(1, int(data.get("cores", 1))))
        except (TypeError, ValueError):
            return cls()


@dataclass(frozen=True, slots=True)
class ServiceEvent:
    """Something happened to a service. ``kind``: started, stopped, exited, crashed, gave_up, blocked,
    installed, install_failed."""

    kind: str
    service: ServiceInfo
    exit_code: int | None = None
    note: str = ""


# The icons a button may show instead of its label (API 5): Pythonator draws them, a plugin sends only the name.
ICONS = ("add", "refresh", "delete", "play", "stop", "send", "check", "info", "lock", "settings", "save", "open",
         "close", "up", "down", "more", "dots", "log", "package")


class ui:
    """The design system for plugin panels: plain data the window renders with native widgets.

    No HTML, no scripts. Text is shown as text. A panel holds at most 500 items.

    Sizing is Pythonator's, never the plugin's (API 5): a panel has no sizes, fonts or colours to set, and every
    element fits the narrowest dock (240 px). Text wraps, long words and paths included; buttons are as wide as
    their label and end in "…" when there's no room; drop-downs take the rest of their row; a row wraps onto
    more lines; a table scrolls sideways inside itself. So a panel looks right at any width without trying.
    For more than one screenful, give the panel views with :meth:`tabs`, not one long page.
    """

    @staticmethod
    def panel(title: str, *items: dict[str, Any]) -> dict[str, Any]:
        return {"title": title, "items": list(items)}

    @staticmethod
    def text(text: str, tone: str = "") -> dict[str, Any]:
        """``tone``: "", "ok", "warn", "error" or "muted"."""
        return {"type": "text", "text": text, "tone": tone}

    @staticmethod
    def stat(label: str, value: str, tone: str = "") -> dict[str, Any]:
        return {"type": "stat", "label": label, "value": value, "tone": tone}

    @staticmethod
    def table(columns: list[str], rows: list[list[str]], tones: list[str] | None = None) -> dict[str, Any]:
        return {"type": "table", "columns": columns, "rows": rows, "tones": tones or []}

    @staticmethod
    def button(label: str, action: str, value: str = "", icon: str = "") -> dict[str, Any]:
        """Pressing it calls ``Plugin.on_action(action, value)``. ``icon`` (one of :data:`ICONS`, API 5) shows a
        small icon instead, with ``label`` as its tooltip: use it for common actions (refresh, delete, stop…) so
        the panel keeps its room for what matters."""
        out = {"type": "button", "label": label, "action": action, "value": value}
        if icon:
            out["icon"] = icon
        return out

    @staticmethod
    def tabs(action: str, options: list[str], value: str) -> dict[str, Any]:
        """A switch between the panel's views (2 to 6 short names, API 5): choosing one calls
        ``on_action(action, option)``; ``value`` is the one shown. Put it first and show only that view's items."""
        return {"type": "tabs", "action": action, "options": list(options), "value": value}

    @staticmethod
    def row(*items: dict[str, Any]) -> dict[str, Any]:
        return {"type": "row", "items": list(items)}

    @staticmethod
    def code(text: str) -> dict[str, Any]:
        """Monospaced, selectable text, up to 4000 characters (API 2)."""
        return {"type": "code", "text": text}

    @staticmethod
    def input(action: str, placeholder: str = "", button: str = "Send") -> dict[str, Any]:
        """A text box with a button: Enter (or the button) calls ``on_action(action, text)``; Shift+Enter is a new
        line. What the user typed survives panel updates (API 2)."""
        return {"type": "input", "action": action, "placeholder": placeholder, "label": button}

    @staticmethod
    def select(action: str, options: list[str], value: str = "", label: str = "") -> dict[str, Any]:
        """A drop-down list of up to 300 options; choosing one calls ``on_action(action, option)`` (API 2)."""
        return {"type": "select", "action": action, "options": list(options), "value": value, "label": label}

    @staticmethod
    def chat(*messages: dict[str, Any]) -> dict[str, Any]:
        """A chat view of up to 200 ``ui.message`` items that fills the panel's height. It scrolls on its own and
        follows new messages while it is scrolled to the end; give it a stable place in the panel so a redraw only
        updates what changed (API 3)."""
        return {"type": "chat", "messages": list(messages)}

    @staticmethod
    def message(role: str, text: str, code: bool = False, markdown: bool = False) -> dict[str, Any]:
        """``role``: "you", "ai", "note" or "error"; up to 20,000 characters. Messages of the same role in a row
        share one bubble, so an answer can be text and ``code=True`` blocks in turn (API 3). ``markdown=True``
        shows an "ai" message's Markdown formatted: headings, lists, bold, tables; never HTML or images (API 4).
        A long answer is best sent as several messages (paragraphs): while it streams, only the last one
        changes."""
        out: dict[str, Any] = {"role": role, "text": text, "code": code}
        if markdown:
            out["markdown"] = True
        return out


@dataclass
class Plugin:
    """Subclass this and override the ``on_*`` methods you need. Create no threads before ``on_start``."""

    settings: dict[str, Any] = field(default_factory=dict)
    services: dict[str, ServiceInfo] = field(default_factory=dict)
    system: SystemInfo = field(default_factory=SystemInfo)  # the whole computer (``inspect``)
    permissions: frozenset[str] = frozenset()
    test_mode: bool = False
    data_dir: Path = Path()

    # -- override these -------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Called once, after settings and services are filled in."""

    def on_stop(self) -> None:
        """Called before the plugin is stopped (keep it short)."""

    def on_event(self, event: ServiceEvent) -> None:
        """A service started, stopped, crashed..."""

    def on_output(self, service: ServiceInfo, lines: list[str]) -> None:
        """New output lines of a service (``read_logs``)."""

    def on_stats(self) -> None:
        """``self.services`` and ``self.system`` were refreshed with CPU, memory and ports (``inspect``, every 2 s)."""

    def check_start(self, service: ServiceInfo) -> str | None:
        """Return a reason to keep ``service`` from starting, or None (``policy``; answer within 2 s)."""
        return None

    def on_settings(self) -> None:
        """``self.settings`` changed."""

    def on_action(self, action: str, value: str) -> None:
        """A button of your panel was pressed."""

    def on_tick(self) -> None:
        """Every 5 seconds."""

    def on_proposal(self, proposal_id: str, status: str, note: str) -> None:
        """The user decided on a change you proposed: ``status`` is applied, rejected, failed or expired."""

    # -- call these -------------------------------------------------------------------------------------

    def log(self, text: str) -> None:
        """A line in the plugin's own log (Plugins → the plugin → Activity)."""
        _send({"t": "log", "text": str(text)[:4000]})

    def notify(self, text: str, level: str = "info") -> None:
        """A notice in the window. ``level``: info, ok, warn, error."""
        _send({"t": "notify", "text": str(text)[:2000], "level": level})

    def show(self, panel: dict[str, Any]) -> None:
        """Show or replace this plugin's panel (``ui``). Build it with :class:`ui`."""
        _send({"t": "panel", "panel": panel})

    def start(self, service_id: str) -> None:
        _send({"t": "control", "op": "start", "id": service_id})

    def stop(self, service_id: str) -> None:
        _send({"t": "control", "op": "stop", "id": service_id})

    def restart(self, service_id: str) -> None:
        _send({"t": "control", "op": "restart", "id": service_id})

    # API 2: requests that wait for Pythonator's answer. Call them from your own thread, not from an on_* method
    # that must answer quickly. They raise PluginError when refused, and after ``timeout`` seconds.

    def list_files(self, service_id: str, timeout: float = 15.0) -> dict[str, Any]:
        """``{"files": [{"path", "size"}], "hidden": n, "truncated": bool, "entry": "main.py"}`` (``read_files``).
        Secret files are left out and counted in ``hidden``."""
        return dict(_request({"op": "list", "id": service_id}, timeout))

    def read_file(self, service_id: str, path: str, timeout: float = 15.0) -> dict[str, Any]:
        """``{"path", "text", "truncated", "size"}`` of a text file in the service's folder, at most 256 KB
        (``read_files``). The values of the service's environment variables are hidden in ``text``."""
        return dict(_request({"op": "read", "id": service_id, "path": path}, timeout))

    def propose_change(self, service_id: str, path: str, edits: list[tuple[str, str]], summary: str = "",
                       timeout: float = 15.0) -> str:
        """Suggest a change: exact ``(old, new)`` replacements, each ``old`` found once in the file (one edit with an
        empty ``old`` creates a new file). The window shows the user a diff; nothing is written unless they apply
        it. Returns the proposal's id; the answer comes later as ``on_proposal`` (``propose_changes``)."""
        data = _request({"op": "propose", "id": service_id, "path": path, "summary": summary,
                         "edits": [{"old": o, "new": n} for o, n in edits]}, timeout)
        return str(data.get("proposal", ""))

    def propose_changes(self, service_id: str, changes: list[tuple[str, list[tuple[str, str]]]], summary: str = "",
                        timeout: float = 15.0) -> str:
        """Suggest a change to several files at once, as ``[(path, edits)...]`` (each like ``propose_change``; new
        files may be in new folders). The user reviews them in one window and applies all or none. Returns the
        proposal's id; the answer comes as one ``on_proposal`` (``propose_changes``, API 5)."""
        data = _request({"op": "propose", "id": service_id, "summary": summary,
                         "changes": [{"path": path, "edits": [{"old": o, "new": n} for o, n in edits]}
                                     for path, edits in changes]}, timeout)
        return str(data.get("proposal", ""))


    # API 4: new projects as services (``create_services``), from Pythonator's own templates only.

    def project_templates(self, timeout: float = 15.0) -> dict[str, Any]:
        """``{"templates": [{"key", "label", "tool", "port", "ready", "needs"}], "folder": default parent}``."""
        return dict(_request({"op": "templates"}, timeout))

    def create_project(self, template: str, name: str, folder: str = "", start: bool = False,
                       timeout: float = 30.0) -> dict[str, Any]:
        """Make a new service from a template: ``name`` is a new folder in ``folder`` (empty: the default
        projects folder), made by the framework's official tool in the new service's sandbox. Returns
        ``{"id", "folder", "port"}``; the tool's output shows in the new service's console."""
        return dict(_request({"op": "create", "template": template, "name": name, "folder": folder,
                              "start": start}, timeout))

    def list_projects(self, timeout: float = 15.0) -> list[dict[str, Any]]:
        """The services made from templates: ``[{"id", "name", "template", "folder", "state", "port"}]``."""
        return list(dict(_request({"op": "projects"}, timeout)).get("projects", []))

    def delete_project(self, sid: str, timeout: float = 15.0) -> None:
        """Ask the user, in Pythonator's window, to delete a project made from a template and its folder. Nothing
        is deleted unless the user says so there."""
        _request({"op": "delete", "id": sid}, timeout)

    # Leftovers in the projects folder (create_services).

    def leftover_projects(self, folder: str = "", timeout: float = 15.0) -> dict[str, Any]:
        """Folders in the projects folder (``folder``; empty: the default one) that no service uses any more:
        ``{"folder", "leftovers": [{"folder", "name", "made"}]}``. ``made``: the Project builder made it (a project
        removed with its folder kept, or a delete a held file stopped). In the default projects folder every
        unused folder is listed; elsewhere only the ones the builder made. Nothing inside them is read."""
        return dict(_request({"op": "leftovers", "folder": folder}, timeout))

    def delete_leftovers(self, folders: list[str], folder: str = "", timeout: float = 15.0) -> int:
        """Ask the user, in Pythonator's window, to delete some of :meth:`leftover_projects`; they pick which.
        Returns how many were asked about. Nothing is deleted unless the user says so there."""
        data = _request({"op": "delete_leftovers", "folder": folder, "folders": list(folders)}, timeout)
        return int(data.get("asked", 0))


_out_lock = threading.Lock()
_out: Any = None  # the protocol stream; set by the worker (print() goes to the plugin's log instead)
_waiting: dict[int, tuple[threading.Event, list[dict[str, Any]]]] = {}
_next_req = [0]


def _request(message: dict[str, Any], timeout: float) -> dict[str, Any]:
    event: threading.Event = threading.Event()
    box: list[dict[str, Any]] = []
    with _out_lock:
        _next_req[0] += 1
        req = _next_req[0]
        _waiting[req] = (event, box)
    try:
        _send({"t": "request", "req": req, **message})
        if not event.wait(timeout):
            raise PluginError("Pythonator didn't answer in time")
    finally:
        _waiting.pop(req, None)
    reply = box[0] if box else {}
    if not reply.get("ok"):
        raise PluginError(str(reply.get("error") or "Refused"))
    data = reply.get("data")
    return data if isinstance(data, dict) else {}


def _deliver(reply: dict[str, Any]) -> None:
    """The worker hands each ``reply`` here, from its reader thread."""
    entry = _waiting.get(reply.get("req", -1)) if isinstance(reply.get("req"), int) else None
    if entry is not None:
        entry[1].append(reply)
        entry[0].set()


def _send(message: dict[str, Any]) -> None:
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _out_lock:
        stream = _out or sys.__stdout__
        if stream is None:
            return
        try:
            stream.write(line)
            stream.flush()
        except (OSError, ValueError):  # Pythonator closed the pipe (it quit or stopped the plugin): end quietly
            _gone()


def _gone() -> None:
    """The host is gone: nobody reads what this process says any more. Exit at once, from any thread, without a
    traceback (a frozen Windows app would show one in an "Unhandled exception" window)."""
    import os

    with contextlib.suppress(Exception):
        sys.stderr.flush()
    os._exit(0)
