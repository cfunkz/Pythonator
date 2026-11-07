"""The daemon side of plugins: find them, decide their trust, run each one sandboxed, and police what they send.

* **Official** plugins (in the app's ``plugins`` folder and matching ``official.json``) get the permissions their
  manifest declares and run live, still in a sandbox of their own: least privilege, even when trusted.
* **Third-party** plugins (everything else, including an official one with a changed byte) start in **test mode**
  with no permissions: actions they try are recorded in their activity log, not carried out, until you grant
  permissions and take them out of test mode. They never run without the OS sandbox.

Every message is size-checked, schema-checked and rate-limited. A plugin that floods, sends garbage or keeps
crashing is stopped and disabled with the reason shown.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .. import isolation
from ..core import git, validate
from ..core.keychain import IN_FILE, IN_KEYCHAIN, Vault
from ..core.models import Mode, Service
from ..daemon import winproc
from ..isolation.base import Grant, Policy, launcher_files, runtime_folders
from ..paths import IS_LINUX, IS_MAC, IS_WINDOWS, DataDirs, official_plugins_dir, quiet_start, self_command
from ..sdk import ICONS
from . import files, trust
from .manifest import PERMISSIONS, Manifest, load

log = logging.getLogger(__name__)
RETIRED = ("health", "webhooks")  # official plugins that became Notify in 0.0.11
MAX_LINE = 1024 * 1024  # one message; a panel with a long AI answer is a few hundred KB
READ_BUFFER = 64 * 1024
RATE, BURST, MAX_DROPS = 200.0, 400.0, 2000
BYTE_RATE, BYTE_BURST = 2.0 * 1024 * 1024, 8.0 * 1024 * 1024  # and at most this much a second (panels are big)
MAX_TEXT = 2000  # characters of one log line, error or stderr line kept
ERR_RATE = 50  # stderr lines a second shown; more are counted, not shown
MAX_OUTBOX = 2000  # messages to a plugin it hasn't read yet; then it is stopped: it no longer reads them
CHECK_DEADLINE = 2.0
MAX_CRASHES = 3
REQUESTS_PER_MINUTE = 120  # list, read and propose, per plugin
CREATIONS_PER_HOUR = 10  # new projects (create_services), per plugin
MASK = "••••••"


# -- the panel design system, as the host validates it -------------------------------------------------------

Tone = Literal["", "ok", "warn", "error", "muted"]


class Message(BaseModel):
    """One line of a chat: the user's ("you"), the plugin's ("ai"), a small note or an error (API 3)."""

    model_config = ConfigDict(extra="forbid")
    role: Literal["you", "ai", "note", "error"]
    text: str = Field(default="", max_length=20_000)
    code: bool = False  # monospaced, like a code block
    markdown: bool = False  # "ai" text shown as Markdown, without HTML or images (API 4)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text", "stat", "table", "button", "row", "code", "input", "select", "chat", "tabs"]
    icon: str = Field(default="", max_length=20)  # a button's icon instead of its label (API 5)
    text: str = Field(default="", max_length=4000)
    placeholder: str = Field(default="", max_length=200)
    options: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=300)
    tone: Tone = ""
    label: str = Field(default="", max_length=200)
    value: str = Field(default="", max_length=400)
    action: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_.:-]*$")
    columns: list[str] = Field(default_factory=list, max_length=12)
    rows: list[list[str]] = Field(default_factory=list, max_length=500)
    tones: list[Tone] = Field(default_factory=list, max_length=500)
    items: list[Node] = Field(default_factory=list, max_length=20)
    messages: list[Message] = Field(default_factory=list, max_length=200)

    @field_validator("icon")
    @classmethod
    def _known_icon(cls, value: str) -> str:
        if value and value not in ICONS:
            raise ValueError(f"not one of Pythonator's icons: {', '.join(ICONS)}")
        return value

    @model_validator(mode="after")
    def _tabs_are_short(self) -> Node:
        if self.type == "tabs" and (not 2 <= len(self.options) <= 6 or any(len(o) > 30 for o in self.options)):
            raise ValueError("tabs have 2 to 6 options of at most 30 characters")
        return self


class Panel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=80)
    items: list[Node] = Field(max_length=200)


def _count(nodes: list[Node]) -> int:
    return sum(1 + len(n.rows) + len(n.messages) + _count(n.items) for n in nodes)


# -- one plugin ----------------------------------------------------------------------------------------------


@dataclass
class Plugin:
    folder: Path
    manifest: Manifest
    official: bool
    sha256: str = ""
    error: str = ""  # why it isn't running
    proc: subprocess.Popen[bytes] | None = None
    ready: bool = False
    crashes: deque[float] = field(default_factory=deque)
    tokens: float = BURST
    byte_tokens: float = BYTE_BURST
    last: float = field(default_factory=time.monotonic)
    outbox: queue.Queue[bytes | None] | None = None  # what its writer thread sends it
    drops: int = 0
    activity: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    panel: dict[str, Any] | None = None
    stopping: bool = False
    requests: deque[float] = field(default_factory=deque)
    creations: deque[float] = field(default_factory=deque)

    @property
    def id(self) -> str:
        return self.manifest.id


class PluginHost:
    def __init__(self, dirs: DataDirs, supervisor: Any) -> None:
        self.dirs = dirs
        self.sup = supervisor
        self.plugins: dict[str, Plugin] = {}
        self._checks: dict[int, tuple[set[str], list[str], Callable[[str], None]]] = {}
        self._req = 0
        self._last_tick = 0.0
        self.vault = Vault(dirs.root / "secrets.json")
        self.proposals: dict[str, files.Proposal] = {}
        self._jobs: dict[int, winproc.Job] = {}  # Windows: each running plugin's job object, by pid
        self.discover()
        self._migrate()
        supervisor.listeners.append(self._on_supervisor_event)
        supervisor.gate = self.check_start

    # -- discovery and trust ------------------------------------------------------------------------------

    def discover(self) -> None:
        found: dict[str, Plugin] = {}
        for base in (official_plugins_dir(), self.dirs.plugins):
            if not base.is_dir():
                continue
            for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
                if base == official_plugins_dir() and folder.name in RETIRED:
                    continue  # left behind by an update over an older version: Notify does their work now
                try:
                    manifest = load(folder)
                except ValueError as exc:
                    log.warning("plugin %s skipped: %s", folder.name, exc)
                    continue
                if manifest.id in found:
                    continue  # an official plugin wins over a copy with the same id
                found[manifest.id] = Plugin(folder, manifest, trust.is_official(manifest.id, folder),
                                            _safe_hash(folder))
        for pid, old in self.plugins.items():
            if pid in found and old.proc:
                found[pid] = old
        self.plugins = found

    def _migrate(self) -> None:
        """0.0.11: Health checks and Webhooks became one plugin, Notify. Whoever had either on gets Notify on, with
        their webhook URLs, alert choices and health check settings carried over."""
        old = [pid for pid in ("webhooks", "health") if pid not in self.plugins]
        if "notify" not in self.plugins or not old:
            return
        settings = self.settings
        was_on = [pid for pid in old if pid in settings.plugins_enabled]
        values: dict[str, str | int | bool] = dict(settings.plugin_settings.get("notify", {}))
        for pid in old:
            renamed = {"path": "health_path", "interval": "health_interval", "timeout": "health_timeout",
                       "alert": "on_unhealthy"} if pid == "health" else {}
            for key, value in settings.plugin_settings.get(pid, {}).items():
                values.setdefault(renamed.get(key, key), value)
        if not was_on and not any(pid in settings.plugin_settings for pid in old):
            return
        enabled = [pid for pid in settings.plugins_enabled if pid not in old]
        if was_on and "notify" not in enabled:
            enabled.append("notify")
        rest = {k: v for k, v in settings.plugin_settings.items() if k not in old}
        self.sup.save_settings(settings.model_copy(update={"plugins_enabled": enabled,
                                                           "plugin_settings": {**rest, "notify": values}}))

    @property
    def settings(self) -> Any:
        return self.sup.settings

    def enabled(self, p: Plugin) -> bool:
        return p.id in self.settings.plugins_enabled

    def test_mode(self, p: Plugin) -> bool:
        return not p.official and p.id not in self.settings.plugin_live

    def granted(self, p: Plugin) -> set[str]:
        if p.official:
            return set(p.manifest.permissions)
        return set(self.settings.plugin_grants.get(p.id, [])) & set(p.manifest.permissions)

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for p in self.plugins.values():
            values = {s.key: s.default for s in p.manifest.settings}
            values.update(self.settings.plugin_settings.get(p.id, {}))
            stored: dict[str, str] = {}
            for s in p.manifest.settings:  # secrets never leave the daemon
                if s.kind == "secret" and values.get(s.key):
                    marker = str(values[s.key])
                    stored[s.key] = {IN_KEYCHAIN: "keychain", IN_FILE: "file"}.get(marker, "settings")
                    values[s.key] = MASK
            out.append({
                "id": p.id, "name": p.manifest.name, "version": p.manifest.version,
                "description": p.manifest.description, "official": p.official, "folder": str(p.folder),
                "enabled": self.enabled(p), "running": bool(p.proc and p.ready), "error": p.error,
                "test_mode": self.test_mode(p), "permissions": list(p.manifest.permissions),
                "granted": sorted(self.granted(p)), "settings": [s.model_dump() for s in p.manifest.settings],
                "values": values, "activity": list(p.activity)[-50:], "homepage": p.manifest.homepage,
                "sha256": p.sha256, "secrets_stored": stored, "api": p.manifest.api,
            })
        return out

    def changed(self) -> None:
        self.sup.emit({"ev": "plugins", "plugins": self.describe()})

    # -- life cycle --------------------------------------------------------------------------------------

    def start_enabled(self) -> None:
        for p in self.plugins.values():
            if self.enabled(p) and not p.proc:
                self.launch(p)
        self.changed()

    def launch(self, p: Plugin) -> None:
        p.error, p.ready, p.stopping, p.drops = "", False, False, 0
        if (now := _safe_hash(p.folder)) != p.sha256:  # trust and grants were for the files checked before
            p.sha256, p.official = now, trust.is_official(p.id, p.folder)
            if not p.official:
                self._forget(p.id, keep_values=True)
                p.error = "Its files changed, so it is off, in test mode and granted nothing: turn it on to try them"
                self._activity(p, f"Not started: {p.error}")
                self.changed()
                return
        try:
            cmd = self._command(p)
            # Its own job object on Windows (its own process group elsewhere), so ending it ends everything it
            # started: on Windows the process started here is the launcher, and the worker runs below it.
            job = winproc.Job() if IS_WINDOWS else None
            try:
                p.proc = subprocess.Popen(cmd.argv, cwd=cmd.cwd, env=cmd.env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                          creationflags=(0x08000000 | 0x4) if IS_WINDOWS else 0,  # no window, suspended
                                          startupinfo=quiet_start(), start_new_session=not IS_WINDOWS)
                if job is not None:
                    try:
                        job.adopt(p.proc)
                    except OSError:
                        p.proc.kill()
                        raise
                    self._jobs[p.proc.pid] = job
                    job = None
            finally:
                if job is not None:
                    job.close()
        except (isolation.IsolationError, OSError) as exc:
            p.proc, p.error = None, str(exc)
            self._activity(p, f"Not started: {exc}")
            return
        proc = p.proc
        outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=MAX_OUTBOX)
        p.outbox = outbox
        threading.Thread(target=self._write, args=(proc, outbox), daemon=True, name=f"plugin-{p.id}-in").start()
        threading.Thread(target=self._read, args=(p, proc, outbox), daemon=True, name=f"plugin-{p.id}").start()
        threading.Thread(target=self._read_err, args=(p, proc), daemon=True, name=f"plugin-{p.id}-err").start()
        self._activity(p, f"Started ({'official' if p.official else 'third-party'}"
                          f"{', test mode' if self.test_mode(p) else ''}; {cmd.describe or 'no sandbox'})")

    def _command(self, p: Plugin) -> isolation.Command:
        data = self.dirs.plugin_data / p.id
        data.mkdir(parents=True, exist_ok=True)
        argv = self_command("--plugin-worker", str(p.folder), str(data))
        env = {k: v for k, v in isolation.base.base_env(_pseudo_service(p, data), sandboxed=True).items()
               if k not in ("PORT", "HOST")}
        grants = [Grant(p.folder.resolve(), False), Grant(data.resolve(), True),
                  *(Grant(f, False) for f in launcher_files()),
                  *(Grant(f, False) for f in runtime_folders(argv[0]) if f.exists())]
        policy = Policy(network="network" in self.granted(p), grants=grants)
        home, tmp = isolation.base.private_dirs(self.dirs, f"plugin-{p.id}")
        try:
            if IS_WINDOWS:
                from ..isolation import windows

                return windows.wrap(argv, data, env, policy, f"plugin-{p.id}", self.dirs)
            if IS_MAC:
                from ..isolation import macos

                return macos.wrap(argv, data, env, policy, home, tmp)
            if IS_LINUX:
                from ..isolation import linux

                return linux.wrap(argv, data, env, policy, home, tmp)
        except isolation.IsolationError as exc:
            # Official ones too: a plugin may read logs and files, and the AI assistant reads text anyone could
            # have written. Without its sandbox it would run with all of your rights.
            raise isolation.IsolationError(f"Plugins only run sandboxed, and the sandbox isn't available on this "
                                           f"computer: {exc}") from None
        raise isolation.IsolationError("Plugins only run sandboxed, and this system has no sandbox for them")

    def stop(self, p: Plugin) -> None:
        for prop in [x for x in self.proposals.values() if x.plugin == p.id]:
            self._end_proposal(prop, "expired", "The plugin stopped", tell_plugin=False)
        proc, p.proc = p.proc, None
        outbox, p.outbox = p.outbox, None
        p.ready, p.stopping, p.panel = False, True, None
        if proc is None:
            return
        if outbox is not None:
            with contextlib.suppress(queue.Full):  # full: it isn't reading, and is killed in 3 s anyway
                outbox.put_nowait(b'{"t": "stop"}\n')

        def finish() -> None:
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                self._end(proc)

        threading.Thread(target=finish, daemon=True).start()
        self.sup.emit({"ev": "panel", "plugin": p.id, "panel": None})

    def shutdown(self) -> None:
        for p in self.plugins.values():
            self.stop(p)

    # -- reading (threads) → handling (supervisor thread) -------------------------------------------------

    def _read(self, p: Plugin, proc: subprocess.Popen[bytes], outbox: queue.Queue[bytes | None]) -> None:
        if proc.stdout is None:
            return
        # Buffered: the pipe itself is unbuffered (bufsize=0), and readline() on it reads one byte per system
        # call. A 1 MB message then took seconds on Windows, and a big panel held up the plugin.
        out = io.BufferedReader(proc.stdout, READ_BUFFER)  # type: ignore[type-var]
        while True:
            line = out.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                # Killed here, at once: nobody reads the rest of the message, so a plugin still writing it is
                # stuck, and proc.wait() below would wait for it forever (Windows pipes hold only a few KB).
                self._end(proc)
                self.sup.post(lambda: self._kill(p, proc, "sent a message larger than 1 MB"))
                break
            self.sup.post(lambda line=line: self._on_line(p, proc, line))
        code = proc.wait()
        self._end(proc)  # whatever it left behind goes with it
        with contextlib.suppress(queue.Full):
            outbox.put_nowait(None)  # its writer thread ends too
        self.sup.post(lambda: self._on_exit(p, proc, code))

    @staticmethod
    def _write(proc: subprocess.Popen[bytes], outbox: queue.Queue[bytes | None]) -> None:
        """Its own thread: a plugin that stops reading blocks this one, never the background service."""
        while (data := outbox.get()) is not None and proc.stdin is not None:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _read_err(self, p: Plugin, proc: subprocess.Popen[bytes]) -> None:
        if proc.stderr is None:
            return
        err = io.BufferedReader(proc.stderr, READ_BUFFER)  # type: ignore[type-var]
        second, shown, dropped = 0, 0, 0
        for raw in iter(lambda: err.readline(8192), b""):
            now = int(time.monotonic())
            if now != second:
                if dropped:
                    self.sup.post(lambda n=dropped: self._activity(p, f"(skipped {n} more lines of its errors)"))
                second, shown, dropped = now, 0, 0
            text = raw.decode("utf-8", "replace").rstrip()[:MAX_TEXT]
            if not text:
                continue
            if shown >= ERR_RATE:
                dropped += 1
                continue
            shown += 1
            self.sup.post(lambda text=text: self._activity(p, _clean(text)))

    def _on_exit(self, p: Plugin, proc: subprocess.Popen[bytes], code: int) -> None:
        if p.proc is not proc and not p.stopping:
            return
        p.proc, p.ready, p.panel = None, False, None  # the window clears it below: a restart's panel is new again
        if p.stopping:
            p.stopping = False
            self.changed()
            return
        now = time.monotonic()
        p.crashes.append(now)
        while p.crashes and now - p.crashes[0] > 600:
            p.crashes.popleft()
        if not p.error:
            p.error = f"Stopped unexpectedly (exit code {code})"
        self._activity(p, p.error)
        if len(p.crashes) >= MAX_CRASHES:
            p.error = f"Disabled: crashed {MAX_CRASHES} times in 10 minutes. Last: {p.error}"
            self._disable(p)
        elif self.enabled(p) and not p.error.startswith("Disabled"):
            delay = 2.0 ** len(p.crashes)
            threading.Timer(delay, lambda: self.sup.post(
                lambda: self.launch(p) if self.enabled(p) and not p.proc else None)).start()
        self.sup.emit({"ev": "panel", "plugin": p.id, "panel": None})
        self.changed()

    def _end(self, proc: subprocess.Popen[bytes]) -> None:
        """End a plugin process and everything it started: its job object on Windows, its process group
        elsewhere (it was started in a session of its own). Safe to call from any thread, and more than once."""
        if (job := self._jobs.pop(proc.pid, None)) is not None:
            job.terminate()
            job.close()
        if not IS_WINDOWS:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()

    def _kill(self, p: Plugin, proc: subprocess.Popen[bytes], why: str) -> None:
        p.error = f"Disabled: it {why}"
        self._activity(p, p.error)
        self._end(proc)
        self._disable(p)

    def _disable(self, p: Plugin) -> None:
        enabled = [i for i in self.settings.plugins_enabled if i != p.id]
        self.sup.save_settings(self.settings.model_copy(update={"plugins_enabled": enabled}))
        self.changed()

    def _rate_ok(self, p: Plugin, size: int) -> bool:
        now = time.monotonic()
        p.tokens = min(BURST, p.tokens + (now - p.last) * RATE)
        p.byte_tokens = min(BYTE_BURST, p.byte_tokens + (now - p.last) * BYTE_RATE)
        p.last = now
        if p.tokens >= 1 and p.byte_tokens >= size:
            p.tokens -= 1
            p.byte_tokens -= size
            p.drops = max(0, p.drops - 1)  # a burst now and then is forgiven; only a flood adds up
            return True
        p.drops += 1
        return False

    def _on_line(self, p: Plugin, proc: subprocess.Popen[bytes], line: bytes) -> None:
        if p.proc is not proc:
            return
        if not self._rate_ok(p, len(line)):
            if p.drops >= MAX_DROPS:
                self._kill(p, proc, f"sent more than {int(RATE)} messages a second for too long")
            return
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
                raise ValueError
        except ValueError:
            self._kill(p, proc, "sent something that isn't a protocol message")
            return
        kind = msg["t"]
        granted = self.granted(p)
        if kind == "hello":
            if p.ready:
                return  # once per start: init reads its secrets from the keychain
            p.ready = True
            self._send(p, {"t": "init", "settings": self._values(p), "permissions": sorted(granted),
                           "test_mode": self.test_mode(p), "services": self._views(p)})
            self.changed()
        elif kind in ("log", "error"):
            self._activity(p, _clean(str(msg.get("text", ""))[:MAX_TEXT]))
        elif kind == "notify":
            level = msg.get("level") if msg.get("level") in ("info", "ok", "warn", "error") else "info"
            self.sup.emit({"ev": "notice", "level": level, "text": f"{p.manifest.name}: "
                           f"{_clean(str(msg.get('text', '')))[:500]}", "plugin": p.id})
        elif kind == "panel":
            self._on_panel(p, msg.get("panel"), granted)
        elif kind == "control":
            self._on_control(p, msg, granted)
        elif kind == "verdict":
            self._on_verdict(p, msg)
        elif kind == "request":
            self._on_request(p, msg, granted)
        else:
            self._activity(p, f"Ignored an unknown message: {_clean(kind)[:40]}")

    def _on_panel(self, p: Plugin, raw: Any, granted: set[str]) -> None:
        if "ui" not in granted:
            self._activity(p, "Wanted to show a panel (needs the ui permission)")
            return
        try:
            panel = Panel.model_validate(raw)
        except ValidationError as exc:
            self._activity(p, f"Panel refused: {exc.errors()[0]['msg']}")
            return
        if _count(panel.items) > 500:
            self._activity(p, "Panel refused: more than 500 items")
            return
        shown = panel.model_dump(exclude_defaults=True)
        if shown == p.panel:
            return  # the same panel again: the window has it
        p.panel = shown
        self.sup.emit({"ev": "panel", "plugin": p.id, "name": p.manifest.name, "panel": p.panel})

    def _on_control(self, p: Plugin, msg: dict[str, Any], granted: set[str]) -> None:
        op, sid = msg.get("op"), msg.get("id")
        if op not in ("start", "stop", "restart") or not isinstance(sid, str) or sid not in self.sup.slots:
            self._activity(p, "Ignored a control request for an unknown service")
            return
        name = self.sup.slots[sid].service.name
        if "control" not in granted:
            self._activity(p, f"Wanted to {op} {name} (needs the control permission)")
            return
        if self.test_mode(p):
            self._activity(p, f"Test mode: would {op} {name}")
            return
        self._activity(p, f"{op.capitalize()} {name}")
        with contextlib.suppress(Exception):
            getattr(self.sup, op)(sid)

    # -- requests: the service's files, and proposed changes (API 2) ----------------------------------------

    def _on_request(self, p: Plugin, msg: dict[str, Any], granted: set[str]) -> None:
        req = msg.get("req")
        if not isinstance(req, int):
            return
        if msg.get("op") == "templates" and "create_services" in granted:
            # Looks for every template's tools on PATH (slow on Windows, with a long PATH): never on the event
            # loop, which the window and every service wait on. It reads no state of the supervisor's.
            threading.Thread(target=self._reply, args=(p, req, msg, granted, True), daemon=True,
                             name=f"plugin-{p.id}-templates").start()
            return
        self._reply(p, req, msg, granted)

    def _reply(self, p: Plugin, req: int, msg: dict[str, Any], granted: set[str], later: bool = False) -> None:
        try:
            data = self._answer(p, msg, granted)
            reply: dict[str, Any] = {"t": "reply", "req": req, "ok": True, "data": data}
        except files.FileRefused as exc:
            reply = {"t": "reply", "req": req, "ok": False, "error": str(exc)}
        except OSError as exc:
            reply = {"t": "reply", "req": req, "ok": False, "error": f"Couldn't read it: {exc.strerror or exc}"}
        if later:
            self.sup.post(lambda: self._send(p, reply))
        else:
            self._send(p, reply)

    def _answer(self, p: Plugin, msg: dict[str, Any], granted: set[str]) -> dict[str, Any]:
        op, sid = msg.get("op"), msg.get("id")
        now = time.monotonic()
        while p.requests and now - p.requests[0] > 60:
            p.requests.popleft()
        if len(p.requests) >= REQUESTS_PER_MINUTE:
            raise files.FileRefused(f"Too many requests: at most {REQUESTS_PER_MINUTE} a minute")
        p.requests.append(now)
        if op in ("templates", "create", "projects", "delete", "leftovers", "delete_leftovers"):
            return self._create(p, msg, granted)
        if op not in ("list", "read", "propose"):
            raise files.FileRefused("Unknown request")
        if not isinstance(sid, str) or sid not in self.sup.slots:
            raise files.FileRefused("No such service")
        service = self.sup.slots[sid].service
        if op == "propose":
            if "propose_changes" not in granted:
                self._activity(p, f"Wanted to suggest a change to {service.name} (needs propose_changes)")
                raise files.FileRefused("Needs the propose_changes permission")
            return self._propose(p, service, msg)
        if "read_files" not in granted:
            self._activity(p, f"Wanted to read {service.name}'s files (needs read_files)")
            raise files.FileRefused("Needs the read_files permission")
        view = self._files(service)
        if op == "list":
            return {**view.listing(), "entry": service.entry}
        result = view.read(str(msg.get("path", "")))
        self._activity(p, f"Read {service.name}: {result['path']}")
        return result

    def _create(self, p: Plugin, msg: dict[str, Any], granted: set[str]) -> dict[str, Any]:
        """The Project builder: Pythonator's own templates only; the plugin picks one and names it (API 4)."""
        from ..core import templates

        if "create_services" not in granted:
            self._activity(p, "Wanted to create a project (needs create_services)")
            raise files.FileRefused("Needs the create_services permission")
        if msg.get("op") == "templates":
            return {"templates": templates.catalog(), "folder": str(templates.default_parent())}
        if msg.get("op") == "projects":
            return {"projects": [{"id": s.service.id, "name": s.service.name, "template": s.service.made_by.template,
                                  "folder": s.service.folder, "state": s.state.value,
                                  "port": (s.service.network.ports or [0])[0]}
                                 for s in self.sup.slots.values() if s.service.made_by is not None]}
        if msg.get("op") == "delete":
            return self._ask_delete(p, str(msg.get("id", "")))
        parent = self._projects_folder(msg)
        if msg.get("op") in ("leftovers", "delete_leftovers"):
            return self._leftovers(p, parent, msg)
        key, name = str(msg.get("template", "")), str(msg.get("name", ""))
        now = time.monotonic()
        while p.creations and now - p.creations[0] > 3600:
            p.creations.popleft()
        if len(p.creations) >= CREATIONS_PER_HOUR:
            raise files.FileRefused(f"At most {CREATIONS_PER_HOUR} new projects an hour")
        if self.test_mode(p):
            self._activity(p, f"Test mode: would create a {key} project {name!r} in {parent}")
            raise files.FileRefused("Test mode: the project was only recorded in the plugin's Activity")
        try:
            service = self.sup.create_project(key, name, parent, start=bool(msg.get("start")))
        except (ValueError, OSError) as exc:
            raise files.FileRefused(str(exc)) from None
        p.creations.append(now)
        self._activity(p, f"Created {service.name} ({key}) in {service.folder}")
        return {"id": service.id, "folder": service.folder, "port": service.network.ports[0]}

    @staticmethod
    def _projects_folder(msg: dict[str, Any]) -> Path:
        from ..core import templates

        raw = str(msg.get("folder", "") or "").strip()
        try:
            return Path(validate.folder(raw, "The projects folder", must_exist=False)) if raw \
                else templates.default_parent()
        except ValueError as exc:
            raise files.FileRefused(str(exc)) from None

    def _leftovers(self, p: Plugin, parent: Path, msg: dict[str, Any]) -> dict[str, Any]:
        """Folders in the projects folder no service uses any more. Listing them reads nothing inside;
        deleting them, like a project, is only ever asked of the user in Pythonator's own window."""
        found = self.sup.leftover_projects(parent)
        if msg.get("op") == "leftovers":
            return {"folder": str(parent), "leftovers": found}
        wanted = msg.get("folders")
        keys = {os.path.normcase(str(x)) for x in wanted} if isinstance(wanted, list) else set()
        chosen = [x["folder"] for x in found if os.path.normcase(x["folder"]) in keys]
        if not chosen:
            raise files.FileRefused("None of those folders is a leftover any more: press Refresh")
        if self.test_mode(p):
            self._activity(p, f"Test mode: would ask to delete {len(chosen)} leftover folders in {parent}")
            raise files.FileRefused("Test mode: the request was only recorded in the plugin's Activity")
        self._activity(p, f"Asks you to delete {len(chosen)} leftover project folders in {parent}")
        self.sup.emit({"ev": "confirm_leftovers", "folders": chosen, "plugin": p.manifest.name})
        return {"asked": len(chosen)}

    def _ask_delete(self, p: Plugin, sid: str) -> dict[str, Any]:
        """A plugin never deletes anything itself: Pythonator asks the user, in its own window, whether to delete
        the project and its folder. Only projects the Project builder made, still in the folder it made."""
        if sid in self.sup.slots and self.sup.slots[sid].removing:
            raise files.FileRefused("It is being removed already")
        try:
            folder = self.sup.project_folder(sid)
        except ValueError as exc:
            raise files.FileRefused(str(exc)) from None
        service = self.sup.slots[sid].service
        if self.test_mode(p):
            self._activity(p, f"Test mode: would ask to delete {service.name} and {folder}")
            raise files.FileRefused("Test mode: the request was only recorded in the plugin's Activity")
        self._activity(p, f"Asks you to delete {service.name} and its folder {folder}")
        self.sup.emit({"ev": "confirm_delete", "id": sid, "name": service.name, "folder": str(folder),
                       "plugin": p.manifest.name})
        return {"asked": True}

    def _files(self, service: Service) -> files.ServiceFiles:
        values = {**service.env, **isolation.base.env_file_values(service)}
        return files.ServiceFiles(Path(service.folder), service.env_file, values)

    def _propose(self, p: Plugin, service: Service, msg: dict[str, Any]) -> dict[str, Any]:
        if any(x.plugin == p.id for x in self.proposals.values()):
            raise files.FileRefused("A change is already waiting for the user's review")
        if len(self.proposals) >= 5:
            raise files.FileRefused("Too many changes are waiting for review")
        summary = _clean(str(msg.get("summary", "")))[:300]
        if "changes" in msg:  # several files, one review (API 5)
            proposal = self._files(service).propose_many(p.id, service.id, msg.get("changes"), summary)
        else:
            proposal = self._files(service).propose(p.id, service.id, str(msg.get("path", "")), msg.get("edits"),
                                                   summary)
        names = ", ".join(proposal.paths)
        if self.test_mode(p):
            self._activity(p, f"Test mode: would suggest a change to {service.name}: {names}")
            raise files.FileRefused("Test mode: the change was only recorded in the plugin's Activity")
        self.proposals[proposal.id] = proposal
        self._activity(p, f"Suggests a change to {service.name}: {names} (waiting for review)")
        self.sup.emit(self._proposal_event(proposal))
        return {"proposal": proposal.id}

    def _proposal_event(self, x: files.Proposal) -> dict[str, Any]:
        plugin = self.plugins.get(x.plugin)
        slot = self.sup.slots.get(x.service)
        return {"ev": "proposal", "proposal": {
            "id": x.id, "plugin": x.plugin, "plugin_name": plugin.manifest.name if plugin else x.plugin,
            "service": x.service, "service_name": slot.service.name if slot else x.service, "path": x.path,
            "paths": x.paths, "new_files": [part.path for part in x.parts if part.before is None],
            "folder": slot.service.folder if slot else "", "summary": x.summary,
            "new_file": all(part.before is None for part in x.parts),
            "warnings": [f"{part.path}: {w}" if x.others else w for part in x.parts for w in part.warnings],
            "diff": "\n\n".join(files.diff(part) for part in x.parts)}}

    def pending_proposals(self) -> list[dict[str, Any]]:
        return [self._proposal_event(x) for x in self.proposals.values()]

    def decide(self, pid: str, proposal_id: str, apply: bool) -> str:
        """The user's answer from the review window. Returns what happened, for the window."""
        x = self.proposals.get(proposal_id)
        if x is None or x.plugin != pid:
            raise KeyError("That change is no longer waiting (it expired or was answered)")
        if not apply:
            self._end_proposal(x, "rejected", "")
            return "Rejected"
        slot = self.sup.slots.get(x.service)
        if slot is None:
            self._end_proposal(x, "failed", "The service was removed")
            raise ValueError("The service was removed")
        try:
            backups = self._files(slot.service).apply_all(x, self.dirs.root / "backups" / x.service)
        except (files.FileRefused, OSError) as exc:
            self._end_proposal(x, "failed", str(exc))
            raise ValueError(str(exc)) from None
        if not x.others:
            note = f"Saved {x.path}" + (f"; the old version is in {backups[0]}" if backups[0] else " (new file)")
        else:
            new = [part.path for part, backup in zip(x.parts, backups, strict=True) if not backup]
            kept = [backup for backup in backups if backup]
            note = (f"Saved {len(x.parts)} files: {', '.join(x.paths)}" + (f" ({', '.join(new)} new)" if new else "")
                    + (f"; the old versions are in {Path(kept[0]).parent}" if kept else ""))
        self._end_proposal(x, "applied", note)
        return note

    def _end_proposal(self, x: files.Proposal, status: str, note: str, *, tell_plugin: bool = True) -> None:
        self.proposals.pop(x.id, None)
        p = self.plugins.get(x.plugin)
        if p is not None:
            self._activity(p, f"Change to {', '.join(x.paths)}: {status}{f' ({note})' if note else ''}")
            if tell_plugin:
                self._send(p, {"t": "proposal", "id": x.id, "status": status, "note": note})
        self.sup.emit({"ev": "proposal_done", "id": x.id, "status": status, "note": note})

    def _expire(self) -> None:
        now = time.monotonic()
        for x in [x for x in self.proposals.values() if now - x.created > files.PROPOSAL_TTL]:
            self._end_proposal(x, "expired", "Not reviewed within 30 minutes")

    # -- policy -------------------------------------------------------------------------------------------

    def check_start(self, service: Service, done: Callable[[str], None]) -> None:
        asking = [p for p in self.plugins.values() if p.proc and p.ready and "policy" in self.granted(p)]
        if not asking:
            done("")
            return
        self._req += 1
        req = self._req
        self._checks[req] = ({p.id for p in asking}, [], done)
        for p in asking:
            self._send(p, {"t": "check", "req": req, "service": self._view(p, service.id)})
        threading.Timer(CHECK_DEADLINE, lambda: self.sup.post(lambda: self._finish_check(req, timeout=True))).start()

    def _on_verdict(self, p: Plugin, msg: dict[str, Any]) -> None:
        req = msg.get("req")
        entry = self._checks.get(req) if isinstance(req, int) else None
        if entry is None or p.id not in entry[0]:
            return
        entry[0].discard(p.id)
        deny = _clean(str(msg.get("deny", "")))[:300]
        if deny:
            if self.test_mode(p):
                self._activity(p, f"Test mode: would block a start ({deny})")
            else:
                entry[1].append(f"{p.manifest.name}: {deny}")
        if not entry[0]:
            self._finish_check(req)  # type: ignore[arg-type]

    def _finish_check(self, req: int, *, timeout: bool = False) -> None:
        entry = self._checks.pop(req, None)
        if entry is None:
            return
        for pid in entry[0] if timeout else ():
            if pid in self.plugins:
                self._activity(self.plugins[pid], "Didn't answer a start check within 2 s")
        entry[2]("; ".join(entry[1]))

    # -- events to plugins --------------------------------------------------------------------------------

    def _on_supervisor_event(self, event: dict[str, Any]) -> None:
        ev = event.get("ev")
        now = time.monotonic()
        if now - self._last_tick > 5:
            self._last_tick = now
            self._expire()
            for p in self._live():
                self._send(p, {"t": "tick"})
        if ev == "state" and event.get("kind"):
            note = self._masked(event["id"], str(event.get("note", "")))
            for p in self._live():
                self._send(p, {"t": "event", "kind": event["kind"], "exit": event.get("exit"),
                               "note": note, "service": self._view(p, event["id"])})
        elif ev == "output":
            readers = [p for p in self._live() if "read_logs" in self.granted(p)]
            if readers:
                lines = self._masked(event["id"], str(event.get("text", ""))).splitlines()[-200:]
                for p in readers:
                    self._send(p, {"t": "output", "id": event["id"], "lines": lines})
        elif ev == "stats":
            for p in self._live():
                if "inspect" in self.granted(p):
                    self._send(p, {"t": "stats", "services": self._views(p), "system": event.get("system", {})})
        elif ev == "services":
            for p in self._live():
                self._send(p, {"t": "services", "services": self._views(p)})

    def _masked(self, sid: str, text: str) -> str:
        """``text`` with every value of the service's variables and env file masked, whatever its name: plugins
        pass logs on (to an AI provider, a webhook), and a ``DB_PASS`` or a token in ``DISCORD`` is as secret as
        a ``*_TOKEN``. The console and the log file keep the name-based mask only."""
        slot = self.sup.slots.get(sid)
        return slot.log.plugin_mask(text) if slot is not None else text

    def _live(self) -> list[Plugin]:
        return [p for p in self.plugins.values() if p.proc and p.ready]

    def _views(self, p: Plugin) -> list[dict[str, Any]]:
        return [self._view(p, sid) for sid in self.sup.slots]

    def _view(self, p: Plugin, sid: str) -> dict[str, Any]:
        slot = self.sup.slots.get(sid)
        if slot is None:
            return {"id": sid, "name": "?", "runtime": "", "mode": "", "state": "removed"}
        s = slot.service
        view: dict[str, Any] = {"id": s.id, "name": s.name, "runtime": s.runtime, "mode": s.mode.value,
                                "state": slot.state.value, "network": s.network.enabled,
                                "ports": list(s.network.ports), "expose": s.network.expose.value,
                                "memory_limit_mb": s.memory_mb,
                                "cpu_limit": s.cpu_percent, "restarts": len(slot.restarts)}
        if "inspect" in self.granted(p):
            st = slot.stats
            view.update(cpu=st.cpu, memory=st.memory, listening=list(st.ports), remote=list(st.remote[:50]))
        return view

    def _values(self, p: Plugin) -> dict[str, Any]:
        values: dict[str, Any] = {s.key: s.default for s in p.manifest.settings}
        values.update(self.settings.plugin_settings.get(p.id, {}))
        for s in p.manifest.settings:  # the real secret, from the keychain, only to the plugin itself
            if s.kind == "secret" and values.get(s.key):
                values[s.key] = self.vault.load(_account(p.id, s.key), str(values[s.key]))
        return values

    def _send(self, p: Plugin, msg: dict[str, Any]) -> None:
        if p.proc is None or p.outbox is None:
            return
        try:
            p.outbox.put_nowait(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
        except queue.Full:
            self._kill(p, p.proc, f"stopped reading what Pythonator sends it ({MAX_OUTBOX} messages waiting)")

    def _activity(self, p: Plugin, text: str) -> None:
        p.activity.append(f"{time.strftime('%H:%M:%S')} {text}")
        self.sup.emit({"ev": "plugin_activity", "plugin": p.id, "text": p.activity[-1]})

    # -- requests from the window ------------------------------------------------------------------------

    def get(self, pid: str) -> Plugin:
        if pid not in self.plugins:
            raise KeyError("No such plugin")
        return self.plugins[pid]

    def set_enabled(self, pid: str, on: bool) -> None:
        p = self.get(pid)
        enabled = [i for i in self.settings.plugins_enabled if i != pid] + ([pid] if on else [])
        self.sup.save_settings(self.settings.model_copy(update={"plugins_enabled": enabled}))
        if on:
            p.crashes.clear()
            if not p.proc:
                self.launch(p)
        else:
            self.stop(p)
        self.changed()

    def set_values(self, pid: str, values: dict[str, str | int | bool]) -> None:
        p = self.get(pid)
        known = {s.key: s for s in p.manifest.settings}
        old = self.settings.plugin_settings.get(pid, {})
        clean: dict[str, str | int | bool] = {}
        for key, value in values.items():
            setting = known.get(key)
            if setting is None:
                continue
            if setting.kind == "secret":
                if value == MASK:
                    if key in old:
                        clean[key] = old[key]
                    continue
                secret = str(value).strip()
                if not secret:
                    self.vault.delete(_account(pid, key))
                    clean[key] = ""
                else:
                    clean[key] = self.vault.store(_account(pid, key), secret[:8000])
                continue
            if setting.kind == "bool":
                clean[key] = bool(value)
            elif setting.kind == "int":
                clean[key] = int(value)
            else:
                text = validate.arg(str(value))[:8000] if setting.kind == "lines" else validate.arg(str(value))[:2000]
                if setting.kind == "choice" and setting.choices and text not in setting.choices:
                    continue
                clean[key] = text
        all_values = {**self.settings.plugin_settings, pid: clean}
        self.sup.save_settings(self.settings.model_copy(update={"plugin_settings": all_values}))
        self._send(p, {"t": "settings", "settings": self._values(p)})
        self.changed()

    def grant(self, pid: str, permissions: list[str], live: bool) -> None:
        p = self.get(pid)
        if p.official:
            return
        perms = sorted(set(permissions) & set(p.manifest.permissions) & set(PERMISSIONS))
        grants = {**self.settings.plugin_grants, pid: perms}
        lives = [i for i in self.settings.plugin_live if i != pid] + ([pid] if live else [])
        self.sup.save_settings(self.settings.model_copy(update={"plugin_grants": grants, "plugin_live": lives}))
        if p.proc:  # permissions are fixed per process (the sandbox's network too): restart it
            self.stop(p)
            threading.Timer(1.0, lambda: self.sup.post(lambda: self.launch(p) if self.enabled(p) else None)).start()
        self.changed()

    def action(self, pid: str, action: str, value: str) -> None:
        p = self.get(pid)
        self._send(p, {"t": "action", "action": action, "value": value})

    def install(self, source: str) -> str:
        """Copy a plugin from a folder or an https git URL into the user's plugins folder. Returns its id."""
        with tempfile.TemporaryDirectory(prefix="pythonator-plugin-") as tmp:
            if source.startswith(("https://", "ssh://", "git@")):
                folder = Path(tmp) / "src"
                git.clone(source, folder)
                shutil.rmtree(folder / ".git", ignore_errors=True)
            else:
                folder = Path(validate.folder(source, "The plugin folder"))
            manifest = load(folder)
            trust.tree_hash(folder)  # refuses links and huge trees before anything is copied
            if manifest.id in self.plugins and self.plugins[manifest.id].official:
                raise ValueError(f"{manifest.id} is the id of an official plugin")
            dest = self.dirs.plugins / manifest.id
            if manifest.id in self.plugins:
                self.stop(self.plugins[manifest.id])
                self._forget(manifest.id, keep_values=True)  # new code: off, in test mode, nothing granted
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(folder, dest, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        self.discover()
        self.changed()
        return manifest.id

    def remove(self, pid: str) -> None:
        p = self.get(pid)
        if p.official:
            raise ValueError("Official plugins can be turned off, not removed")
        self.set_enabled(pid, False)
        for s in p.manifest.settings:
            if s.kind == "secret":
                self.vault.delete(_account(pid, s.key))
        self._forget(pid)
        shutil.rmtree(p.folder, ignore_errors=True)
        shutil.rmtree(self.dirs.plugin_data / pid, ignore_errors=True)
        self.discover()
        self.changed()

    def _forget(self, pid: str, *, keep_values: bool = False) -> None:
        """What the user allowed a third-party plugin: never carried over to other code with the same id."""
        s = self.settings
        self.sup.save_settings(s.model_copy(update={
            "plugins_enabled": [i for i in s.plugins_enabled if i != pid],
            "plugin_grants": {k: v for k, v in s.plugin_grants.items() if k != pid},
            "plugin_live": [i for i in s.plugin_live if i != pid],
            "plugin_settings": s.plugin_settings if keep_values else
            {k: v for k, v in s.plugin_settings.items() if k != pid}}))


def _account(pid: str, key: str) -> str:
    return f"plugin.{pid}.{key}"


def _pseudo_service(p: Plugin, data: Path) -> Service:
    return Service(name="plugin", folder=str(data), runtime="custom", command=[sys.executable], mode=Mode.SANDBOX)


def _clean(text: str) -> str:
    """Plugin text shown in the window: control and bidi characters removed."""
    import unicodedata

    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))


def _safe_hash(folder: Path) -> str:
    try:
        return trust.tree_hash(folder)
    except (OSError, ValueError) as exc:
        return f"(can't hash: {exc})"
