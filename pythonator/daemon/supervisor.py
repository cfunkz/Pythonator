"""The supervisor: owns every service, starts and stops it, restarts it when it crashes, and records its output.

All state lives on the daemon's main (Qt) thread. Runner threads and the stats thread only put events into one
queue, which :meth:`Supervisor.tick` drains every 50 ms, so nothing here needs a lock.
"""

from __future__ import annotations

import codecs
import contextlib
import json
import logging
import os
import queue
import shutil
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import psutil

from .. import isolation
from ..core import envcheck, git, limits, runtimes, schedule
from ..core.logs import Masker, ServiceLog, secrets_of
from ..core.models import Mode, Restart, Service, Settings
from ..core.store import Store, write_atomic
from ..isolation import container as containers
from ..paths import DataDirs
from .runner import Event, Exited, Output, Runner

log = logging.getLogger(__name__)
Listener = Callable[[dict[str, Any]], None]
RESTART_WINDOW = 600.0  # seconds in which max_restarts counts
WARMUP = 2.0  # seconds a service counts as still starting once running: imports and first connections
LAUNCH_WAIT = 60.0  # the longest one slow start (its environment being made) holds its place in the queue
LISTEN_AFTER = 30  # seconds after a start: a service not listening on its port yet gets a note
LIVE_LIMIT = 128 * 1024  # characters of output per service per tick sent to windows; the log gets everything


class State(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    WAITING = "waiting"  # a restart or the next scheduled run is due later
    FAILED = "failed"
    TASK = "task"  # installing packages or pulling code


# Exit codes of the small python.exe a venv has on Windows, when it can't hand over to the real Python.
VENV_LAUNCHER = {101: "it couldn't start the real Python", 103: "the Python the venv was made from is gone",
                 106: "it couldn't find the venv's pyvenv.cfg", 107: "the venv's pyvenv.cfg is unreadable or wrong"}
QUEUED = "Starting in turn"  # the note of a service waiting in the start queue
MAKING_ENV = "Setting up its environment"  # the task that makes a project's own environment
DISCARDED = "discarded"  # the key suffix of a runner nobody waits for any more (stopped while it started)
CREATING = "Creating the project"
# A task's words when it ends well, and when it fails; anything else installs packages.
TASK_DONE = {"Building images": "Images built", "Pulling images": "Images pulled", "Taking it down": "Taken down",
             "Listing containers": "Done"}
TASK_FAILED = {CREATING: "Couldn't create the project", "Building images": "Build failed",
               "Pulling images": "Pull failed", "Taking it down": "Down failed", "Listing containers": "ps failed"}


class NeedsConfirmation(Exception):
    """Full access must be confirmed by the user once per service."""


@dataclass
class Stats:
    cpu: float = 0.0
    memory: int = 0
    processes: int = 0
    ports: list[int] = field(default_factory=list)
    remote: list[str] = field(default_factory=list)  # outgoing connections, "host:port"
    procs: list[psutil.Process] = field(default_factory=list)  # the process tree it was measured on


@dataclass
class Slot:
    service: Service
    log: ServiceLog
    state: State = State.STOPPED
    runner: Runner | None = None
    task: Runner | None = None
    steps: list[list[str]] = field(default_factory=list)
    task_name: str = ""
    restarts: deque[float] = field(default_factory=deque)
    restart_at: float = 0.0
    next_run: float = 0.0
    note: str = ""
    exit_code: int | None = None
    started_wall: float = 0.0
    listen_checked: bool = False  # this start's "not listening yet" check is done
    stats: Stats = field(default_factory=Stats)
    pending: list[str] = field(default_factory=list)
    partial: str = ""
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    stop_then: Callable[[], None] | None = None  # what to do once it stopped (restart, remove)
    live_skipped: int = 0  # output too fast for the live view, not yet noted there
    live_noted: float = 0.0
    after_steps: Callable[[], None] | None = None  # what to do once its task steps succeeded (start it)
    shell: Runner | None = None  # a command typed into the console's shell, running next to the service
    shell_partial: str = ""
    shell_decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    removing: bool = False  # on its way out: a second Remove or Delete does nothing
    pulling: bool = False  # a git pull is running for it
    after_pull: Callable[[], None] | None = None  # start it once the pull worked (pull on start)


class Supervisor:
    def __init__(self, dirs: DataDirs, store: Store) -> None:
        self.dirs = dirs
        self.store = store
        self.settings: Settings = store.load_settings()
        self.slots: dict[str, Slot] = {}
        self.deleting: set[str] = set()  # folders being deleted in the background (_key): never leftovers
        self.removed: list[str] = []  # the last ids removed: asking again (a second click or window) is a no-op
        self.listeners: list[Listener] = []
        self.gate: Callable[[Service, Callable[[str], None]], None] | None = None  # policy plugins
        self.queue: queue.SimpleQueue[Event | Callable[[], None]] = queue.SimpleQueue()
        self._last_second = 0.0
        self._running_saved: dict[str, list[float]] | None = None  # what running.json holds
        self._launches: list[tuple[str, bool]] = []  # services waiting their turn to start: (id, automatic)
        self._launching: dict[str, float] = {}  # started from the queue and still starting: id -> when
        self._env_waits: set[str] = set()  # waiting for another service to make their shared folder's environment
        self._stop = threading.Event()
        for service in store.load_services():
            self.slots[service.id] = Slot(service, self._new_log(service))
        threading.Thread(target=self._stats_loop, name="stats", daemon=True).start()

    # -- plumbing ---------------------------------------------------------------------------------------

    def _new_log(self, service: Service) -> ServiceLog:
        log_ = ServiceLog(self.dirs.logs / f"{service.id}.log", max_bytes=self.settings.log_max_mb * 1024 * 1024,
                          backups=self.settings.log_backups)
        env = {**isolation.base.env_file_values(service), **service.env}
        log_.mask = Masker(secrets_of(env))
        return log_

    def emit(self, event: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:  # a bad listener never stops the supervisor
                log.exception("listener failed")

    def post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the supervisor's thread (from any thread)."""
        self.queue.put(fn)

    def note(self, slot: Slot, text: str) -> None:
        slot.pending.append(slot.log.note(text))

    def state_event(self, slot: Slot, kind: str = "") -> dict[str, Any]:
        runner = slot.runner or slot.task
        return {"ev": "state", "id": slot.service.id, "state": slot.state.value, "kind": kind,
                "pid": runner.pid if runner else 0, "exit": slot.exit_code, "note": slot.note,
                "restarts": len(slot.restarts), "started": slot.started_wall, "task": slot.task_name,
                "next_run": slot.next_run}

    def _set(self, slot: Slot, state: State, kind: str = "", note: str | None = None) -> None:
        slot.state = state
        if note is not None:
            slot.note = note
        slot.log.flush()  # the file is complete by the time anyone hears of the change (else once a second)
        self._save_running()
        self.emit(self.state_event(slot, kind))

    def snapshot(self) -> dict[str, Any]:
        return {"ev": "snapshot", "services": [s.service.model_dump(mode="json") for s in self.slots.values()],
                "states": [self.state_event(s) for s in self.slots.values()],
                "settings": self.settings.model_dump(mode="json")}

    def _services_changed(self) -> None:
        self.store.save_services([s.service for s in self.slots.values()])
        self.emit({"ev": "services", "services": [s.service.model_dump(mode="json") for s in self.slots.values()]})

    # -- the clock ----------------------------------------------------------------------------------------

    def tick(self) -> None:
        """Handle what the threads reported, flush output, and once a second run restarts and schedules."""
        deadline = time.monotonic() + 0.03
        while time.monotonic() < deadline:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Output):
                self._on_output(item)
            elif isinstance(item, Exited):
                self._on_exit(item)
            else:
                try:
                    item()
                except Exception:
                    log.exception("posted call failed")
        for slot in self.slots.values():
            if slot.pending:
                text = "".join(slot.pending)
                slot.pending.clear()
                if len(text) > LIVE_LIMIT:
                    slot.live_skipped += len(text) - LIVE_LIMIT // 2
                    text = text[-LIVE_LIMIT // 2:]
                    if (cut := text.find("\n") + 1) and cut < len(text):
                        slot.live_skipped += cut
                        text = text[cut:]  # from a line start
                    if time.monotonic() - slot.live_noted >= 1.0:  # one note a second, not one every tick
                        slot.live_noted = time.monotonic()
                        text = f"\x1b[2m[… {slot.live_skipped:,} characters skipped in the live view, " \
                               f"all in the log]\x1b[0m\n" + text
                        slot.live_skipped = 0
                self.emit({"ev": "output", "id": slot.service.id, "text": text})
        if self._launches:
            self._next_launch()
        for sid in list(self._env_waits):
            waiting = self.slots.get(sid)
            if waiting is None or waiting.state is not State.STARTING or waiting.runner or waiting.task:
                self._env_waits.discard(sid)
            elif self._env_maker(waiting) is None:
                self._env_waits.discard(sid)
                self._prepare_env(waiting)
        now = time.monotonic()
        if now - self._last_second >= 1.0:
            self._last_second = now
            self._each_second(now)

    def _each_second(self, now: float) -> None:
        for slot in list(self.slots.values()):
            slot.log.flush()
            if slot.state is State.WAITING and slot.restart_at and now >= slot.restart_at:
                slot.restart_at = 0.0
                self.start(slot.service.id, auto=True)
            elif slot.service.schedule and slot.state in (State.WAITING, State.STOPPED) and slot.next_run \
                    and time.time() >= slot.next_run:
                self.start(slot.service.id, auto=True)

    # -- output and exits ---------------------------------------------------------------------------------

    def _on_output(self, item: Output) -> None:
        data = item.source.take()  # always: the program may be waiting for it
        sid, _, kind = item.key.partition(":")
        slot = self.slots.get(sid)
        if slot is None or not data or kind == DISCARDED:
            return
        shell = kind == "shell"  # its own line buffer: its lines and the program's don't cut into each other
        text = (slot.shell_partial if shell else slot.partial) + \
            (slot.shell_decoder if shell else slot.decoder).decode(data)
        cut = text.rfind("\n") + 1
        if cut == 0 and len(text) < 4096:
            cut = 0
        elif cut == 0:
            cut = len(text)
        if shell:
            slot.shell_partial = text[cut:]
        else:
            slot.partial = text[cut:]
        if cut:
            slot.pending.append(slot.log.write(text[:cut]))

    def _flush_partial(self, slot: Slot, *, shell: bool = False) -> None:
        if shell:
            rest, slot.shell_partial = slot.shell_partial + slot.shell_decoder.decode(b"", final=True), ""
        else:
            rest, slot.partial = slot.partial + slot.decoder.decode(b"", final=True), ""
        if rest:
            slot.pending.append(slot.log.write(rest + "\n"))

    def _on_exit(self, item: Exited) -> None:
        sid, _, kind = item.key.partition(":")
        slot = self.slots.get(sid)
        if slot is None or kind == DISCARDED:
            return
        runner = slot.task if kind == "task" else slot.shell if kind == "shell" else slot.runner
        if runner is not None:  # the last of its output, before the exit is reported
            self._on_output(Output(item.key, runner))
        self._flush_partial(slot, shell=kind == "shell")
        if kind == "shell":
            slot.shell = None
            self.note(slot, f"$ done ({_describe_exit(item.code)})")
            return
        if kind == "task":
            self._task_step_done(slot, item.code)
            return
        runner, slot.runner = slot.runner, None
        slot.exit_code = item.code
        requested = runner is not None and runner.stop_requested
        limit = runner.limit_hit if runner else ""
        described = _describe_exit(item.code)
        if limit:
            described += f", over its {limit}"
        then, slot.stop_then = slot.stop_then, None
        if requested:
            self.note(slot, f"Stopped ({described}) after {_duration(item.runtime)}")
            self._set(slot, State.STOPPED, "stopped", "")
            self._plan_schedule(slot)
            if then:
                then()
            return
        if item.code in (126, 127) and item.runtime < 5 and slot.service.mode is Mode.SANDBOX:
            # The launcher's own codes: the sandbox couldn't be set up, or the program couldn't start in it.
            self.note(slot, "The sandbox couldn't start it (see the line above). Nothing ran unsandboxed.")
            self._set(slot, State.FAILED, "crashed", "Sandbox couldn't start it")
            return
        crashed = item.code != 0
        self.note(slot, f"{'Crashed' if crashed else 'Exited'} ({described}) after {_duration(item.runtime)}")
        if sys.platform == "win32" and item.code in VENV_LAUNCHER and item.runtime < 5 \
                and slot.service.runtime == "python":
            self.note(slot, f"That code is from the venv's python.exe launcher: {VENV_LAUNCHER[item.code]}. "
                            "Packages ▾ → Recreate environment makes the venv again.")
        policy = slot.service.restart
        if slot.service.schedule:
            self._set(slot, State.WAITING, "crashed" if crashed else "exited", described)
            self._plan_schedule(slot)
            return
        if policy is Restart.ALWAYS or (policy is Restart.ON_FAILURE and crashed):
            now = time.monotonic()
            if item.runtime > 60:
                slot.restarts.clear()  # it ran fine for a while: start counting again
            while slot.restarts and now - slot.restarts[0] > RESTART_WINDOW:
                slot.restarts.popleft()
            limit_n = slot.service.max_restarts
            if limit_n and len(slot.restarts) >= limit_n:
                self.note(slot, f"Gave up after {limit_n} restarts in 10 minutes. Fix it and press Start.")
                self._set(slot, State.FAILED, "gave_up", f"Gave up after {limit_n} restarts")
                return
            delay = min(60.0, 2.0 ** len(slot.restarts))
            slot.restarts.append(now)
            slot.restart_at = now + delay
            self.note(slot, f"Restarting in {delay:.0f} s")
            self._set(slot, State.WAITING, "crashed" if crashed else "exited", f"Restarting ({described})")
            return
        self._set(slot, State.FAILED if crashed else State.STOPPED, "crashed" if crashed else "exited", described)

    def _plan_schedule(self, slot: Slot) -> None:
        if slot.service.schedule:
            with contextlib.suppress(ValueError):
                slot.next_run = schedule.next_run(slot.service.schedule, datetime.now()).timestamp()
                if slot.state is State.STOPPED:
                    slot.state = State.WAITING
                self.emit(self.state_event(slot))

    # -- commands -----------------------------------------------------------------------------------------

    def slot(self, sid: str) -> Slot:
        if sid not in self.slots:
            raise KeyError("No such service")
        return self.slots[sid]

    def add(self, service: Service) -> None:
        if service.id in self.slots:
            raise ValueError("A service with this id exists")
        if service.id in self.removed:  # back again (an import): a remove of it means this one now
            self.removed.remove(service.id)
        self.slots[service.id] = Slot(service, self._new_log(service))
        self._services_changed()
        self.emit(self.state_event(self.slots[service.id]))
        self._plan_schedule(self.slots[service.id])

    def update(self, service: Service) -> None:
        slot = self.slot(service.id)
        slot.service = service = service.model_copy(update={"made_by": slot.service.made_by})
        slot.log.close()
        slot.log.mask = self._new_log(service).mask
        self._services_changed()
        if slot.runner:
            self.note(slot, "Saved. Restart it to use the new settings.")
        self._plan_schedule(slot)

    def remove(self, sid: str, *, then: Callable[[], None] | None = None, deleted: Path | None = None) -> None:
        """Remove a service (stopped first). Its clean-up (on Windows, taking its sandbox's permissions back file
        by file) runs in the background, then ``then``: the window and the other services never wait for it.
        A service already being removed, or just removed, is left alone: asking twice never kills it or errors."""
        if sid in self.removed:
            return
        slot = self.slot(sid)
        if slot.removing:
            return
        slot.removing = True
        if slot.service.made_by is not None and deleted is None:  # its folder stays: Refresh can find it later
            self._remember_made(Path(slot.service.made_by.folder))
        if slot.shell:
            slot.shell.kill()

        def clean() -> None:
            with contextlib.suppress(OSError):
                isolation.forget(slot.service, self.dirs, deleted=deleted)
            if then is not None:
                then()

        def gone() -> None:
            slot.log.close()
            self.slots.pop(sid, None)
            self.removed = [*self.removed[-99:], sid]
            self._services_changed()
            threading.Thread(target=clean, name=f"remove-{sid}", daemon=True).start()

        if slot.runner or slot.task:
            slot.stop_then = gone
            self.stop(sid)
        else:
            gone()

    def project_folder(self, sid: str) -> Path:
        """The folder of a project the Project builder made, if it may be deleted: still the folder it was made
        in, a real folder (not a link), and nothing Pythonator protects. Raises ValueError."""
        service = self.slot(sid).service
        made = service.made_by
        if made is None:
            raise ValueError(f"{service.name} wasn't made by the Project builder: remove it, its folder stays")
        folder = Path(service.folder)
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError(f"{folder} isn't a plain folder any more")
        if folder.resolve() != Path(made.folder).resolve():
            raise ValueError(f"{service.name} was moved to another folder since it was made: its folder stays")
        if why := isolation.base.forbidden_write(folder, self.dirs):
            raise ValueError(f"{folder} is {why}")
        return folder

    def delete_project(self, sid: str) -> None:
        """Remove a project the Project builder made, and delete its folder (stopped first). Raises ValueError."""
        if sid in self.removed or self.slot(sid).removing:  # asked twice (two clicks, two windows): under way or done
            return
        folder = self.project_folder(sid)
        name = self.slot(sid).service.name
        self._remember_made(folder)  # until it's gone: if a file is held, Refresh finds what's left
        self.deleting.add(_key(folder))

        def delete() -> None:  # in remove's background thread
            error = _delete_tree(folder)
            self.post(lambda: self._deleted(name, [folder], [error] if error else []))

        self.emit({"ev": "notice", "level": "info", "text": f"{name}: deleting {folder} in the background…"})
        self.remove(sid, then=delete, deleted=folder)

    def _deleted(self, what: str, folders: list[Path], errors: list[str]) -> None:
        """After a delete in the background: forget the folders that went, and say how it went. A folder that
        couldn't go is a warning in the status bar, not a second pop-up: the Project builder's Refresh finds it."""
        self.deleting.difference_update(_key(f) for f in folders)
        self._forget_made([f for f in folders if not f.exists()])
        if errors:
            self.emit({"ev": "notice", "level": "warn",
                       "text": f"{what}: couldn't delete {'; '.join(errors[:3])}"
                               f"{f' and {len(errors) - 3} more' if len(errors) > 3 else ''}. "
                               "The Project builder's Refresh offers to clean up what's left."})
        else:
            gone = str(folders[0]) if len(folders) == 1 else f"{len(folders)} folders"
            self.emit({"ev": "notice", "level": "ok", "text": f"{what}: deleted, with {gone}"})

    # -- leftovers: project folders no service uses any more ----------------------------------------------

    @property
    def _made_file(self) -> Path:
        return self.dirs.root / "made-projects.json"

    def _made(self) -> set[str]:
        """The folders the Project builder made (normalised), whether or not a service still uses them."""
        try:
            data = json.loads(self._made_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(x) for x in data if isinstance(x, str)} if isinstance(data, list) else set()

    def _save_made(self, made: set[str]) -> None:
        with contextlib.suppress(OSError):
            write_atomic(self._made_file, json.dumps(sorted(made), indent=1))

    def _remember_made(self, folder: Path) -> None:
        made = self._made()
        key = _key(folder)
        if key and key not in made:
            self._save_made(made | {key})

    def _forget_made(self, folders: Iterable[Path]) -> None:
        made = self._made()
        left = made - {_key(f) for f in folders}
        if left != made:
            self._save_made(left)

    def leftover_projects(self, parent: Path) -> list[dict[str, Any]]:
        """The folders in a projects folder that no service uses any more: ones the Project builder made (a
        project removed with its folder kept, or a delete a held file stopped halfway), and, in Pythonator's own
        default projects folder, any. Never a link, a hidden folder, or a folder Pythonator protects. Quick: one
        listing of ``parent``, nothing inside the folders is read."""
        from ..core import templates

        try:
            if not parent.is_absolute() or not parent.is_dir():
                return []
            root = parent.resolve()
            children = sorted(root.iterdir(), key=lambda c: c.name.lower())
        except OSError:
            return []
        used: list[Path] = []
        for slot in self.slots.values():
            with contextlib.suppress(OSError, ValueError):
                used.append(Path(slot.service.folder).resolve())
        if root in used:  # a service uses the projects folder itself: everything in it is its own
            return []
        made = self._made()
        own = _key(root) == _key(templates.default_parent())
        out: list[dict[str, Any]] = []
        for child in children:
            try:
                if child.name.startswith(".") or os.path.normcase(str(child)) in self.deleting:
                    continue  # checked before resolve(): on Windows a folder mid-delete may resolve to another spelling
                if child.is_symlink() or child.is_junction() or not child.is_dir():
                    continue
                real = child.resolve()
            except OSError:
                continue
            if any(u == real or u.is_relative_to(real) for u in used) or _key(real) in self.deleting:
                continue
            recorded = _key(real) in made
            if not (recorded or own) or isolation.base.forbidden_write(real, self.dirs):
                continue
            out.append({"folder": str(real), "name": child.name, "made": recorded})
            if len(out) >= 200:
                break
        return out

    def delete_leftovers(self, folders: list[str]) -> None:
        """Delete leftover project folders the user picked, in the background. Each must still be a leftover
        (:meth:`leftover_projects`), checked again here; nothing is deleted if one isn't. Raises ValueError."""
        found: dict[str, dict[str, Path]] = {}
        targets: list[Path] = []
        for raw in folders:
            path = Path(raw)
            if not path.is_absolute():
                raise ValueError(f"{raw} isn't a full path")
            listed = found.setdefault(_key(path.parent), {
                _key(Path(x["folder"])): Path(x["folder"]) for x in self.leftover_projects(path.parent)})
            target = listed.get(_key(path))
            if target is None:
                raise ValueError(f"{raw} isn't a leftover project folder (any more): press Refresh")
            if target not in targets:
                targets.append(target)
        if not targets:
            return
        what = "Leftover projects"
        self.deleting.update(_key(t) for t in targets)

        def delete() -> None:
            errors = [f"{t}: {e}" for t in targets if (e := _delete_tree(t))]
            self.post(lambda: self._deleted(what, targets, errors))

        self.emit({"ev": "notice", "level": "info",
                   "text": f"{what}: deleting {len(targets)} folder{'s' if len(targets) > 1 else ''} in the "
                           "background…"})
        threading.Thread(target=delete, name="delete-leftovers", daemon=True).start()

    def start(self, sid: str, *, confirmed: bool = False, auto: bool = False) -> None:
        slot = self.slot(sid)
        if slot.runner or slot.task or slot.state in (State.STARTING, State.STOPPING, State.TASK):
            return
        service = slot.service
        if service.mode is Mode.FULL and not service.full_access_ok:
            if not confirmed:
                raise NeedsConfirmation(service.name)
            slot.service = service = service.model_copy(update={"full_access_ok": True})
            self._services_changed()
        if not auto:
            slot.restarts.clear()
        slot.restart_at = 0.0
        self._set(slot, State.STARTING, "", "")
        if self.gate is None:
            self._after_gate(slot, "")
        else:
            self.gate(service, lambda verdict: self.post(lambda: self._after_gate(slot, verdict)))

    def _after_gate(self, slot: Slot, verdict: str) -> None:
        if slot.state is not State.STARTING:
            return
        if verdict:
            self.note(slot, f"Not started: {verdict}")
            self._set(slot, State.FAILED, "blocked", verdict)
            return
        if slot.pulling:  # started while git pulls its folder: it starts once the pull is done
            slot.after_pull = lambda: self._prepare_env(slot)
            return
        if slot.service.git and slot.service.git.pull_on_start:
            self._pull(slot, then=lambda: self._prepare_env(slot))
            return
        self._prepare_env(slot)

    def _prepare_env(self, slot: Slot) -> None:
        """With "Own environment", make the project's environment first if it's missing or broken, so the
        service never falls back to the computer's global packages."""
        service = slot.service
        why = ""
        if service.own_env and service.mode is not Mode.CONTAINER and service.runtime in runtimes.OWN_ENV:
            why = runtimes.env_missing(service.runtime, Path(service.folder))
        if not why:
            self._launch(slot)
            return
        try:
            steps = self._install_steps(service)
        except ValueError as exc:
            self.note(slot, f"Not started: {exc}")
            self._set(slot, State.FAILED, "crashed", str(exc))
            return
        if not steps:
            self._launch(slot)
            return
        if (maker := self._env_maker(slot)) is not None:  # two services, one folder: one makes it, the other waits
            if slot.service.id not in self._env_waits:
                self._env_waits.add(slot.service.id)
                self.note(slot, f"Waiting for {maker.service.name}, which is making this folder's environment")
            return
        self.note(slot, f"Making its own environment first: {why}")
        self._run_steps(slot, steps, MAKING_ENV, then=lambda: self._launch(slot))

    def _env_maker(self, slot: Slot) -> Slot | None:
        """Another service that is making the environment of this service's folder right now."""
        folder = Path(slot.service.folder)
        return next((o for o in self.slots.values() if o is not slot and o.state is State.TASK
                     and o.task_name == MAKING_ENV and Path(o.service.folder) == folder), None)

    def _set_up_first(self, slot: Slot, task: list[str] | None, again: Callable[[], None]) -> bool:
        """True when slow set-up (Windows: Node.js copied for sandboxes the first time) runs first, in the
        background; ``again`` then runs on this thread, and the caller returns. The window and the other services
        never wait for it."""
        try:
            work = isolation.slow_setup(slot.service, self.dirs, task=task)
        except Exception:  # prepare() says what is wrong
            work = None
        if work is None:
            return False
        self.note(slot, "Copying Node.js for sandboxes (once per Node version, in the background)…")

        def run() -> None:
            with contextlib.suppress(Exception):  # prepare() tries again and says why
                work()
            self.post(again)

        threading.Thread(target=run, name=f"set-up-{slot.service.id}", daemon=True).start()
        return True

    def _launch(self, slot: Slot, *, set_up: bool = False) -> None:
        service = slot.service
        if not set_up and self._set_up_first(slot, None, lambda: self._launch_after_set_up(slot)):
            return
        if service.mode is not Mode.CONTAINER:
            self._started(slot, *self._start(service))
            return

        state = slot.state

        def work() -> None:  # the engine's CLI can take many seconds (Docker Desktop still starting): not here
            result = self._start(service)
            self.post(lambda: self._started(slot, *result, state=state))

        threading.Thread(target=work, name=f"start-{service.id}", daemon=True).start()

    def _start(self, service: Service) -> tuple[list[str], Runner | None, str]:
        """Check, prepare and start ``service`` (any thread): (notes, the runner or None, why it didn't start)."""
        notes = [f"{'Problem' if level == 'error' else 'Note'}: {text}"
                 for level, text in envcheck.service_report(service) if level != "ok"]
        notes += [f"Problem: {text}" for text in envcheck.port_problems(service)]
        try:
            cmd = isolation.prepare(service, self.dirs)
        except (isolation.IsolationError, OSError) as exc:
            return [*notes, f"Not started: {exc}"], None, str(exc)
        runner = Runner(service.id, cmd, self.queue, memory_mb=service.memory_mb,  # type: ignore[arg-type]
                        cpu_percent=service.cpu_percent)
        try:
            runner.start()
        except OSError as exc:
            return [*notes, f"Couldn't start: {exc}"], None, str(exc)
        return notes, runner, ""

    def _started(self, slot: Slot, notes: list[str], runner: Runner | None, error: str, *,
                 state: State | None = None) -> None:
        """``state``: the slot's state when the start began on another thread; it must not have changed since."""
        if state is not None and (self.slots.get(slot.service.id) is not slot or slot.state is not state
                                  or slot.runner):
            if runner is not None:  # stopped or removed while a container started: its exit is no news
                runner.key = f"{runner.key}:{DISCARDED}"
                runner.kill()
            return
        for text in notes:
            self.note(slot, text)
        if runner is None:
            self._set(slot, State.FAILED, "crashed", error)
            return
        service, cmd = slot.service, runner.cmd
        slot.log.mask = Masker(secrets_of({**cmd.env, **isolation.base.env_file_values(service)}))  # as it is now
        slot.runner = runner
        slot.exit_code = None
        slot.started_wall = time.time()
        slot.listen_checked = False
        slot.next_run = 0.0
        self.note(slot, f"Started {_shown(cmd.shown or cmd.argv)} · pid {runner.pid}")
        self.note(slot, cmd.describe)
        if service.cpu_percent or service.memory_mb:
            self.note(slot, _limits_note(service, runner))
        if sys.platform == "win32" and service.mode is Mode.SANDBOX and service.network.enabled \
                and service.network.ports:
            self._reach_note(slot)
        self._set(slot, State.RUNNING, "started", "")

    def _reach_note(self, slot: Slot) -> None:
        """Windows: a sandboxed server nobody may reach yet says so, with what to press (checked off the
        supervisor's thread: it starts CheckNetIsolation)."""
        from ..isolation import windows

        def check() -> None:
            if windows.loopback_exempt(slot.service.id) is False:
                self.post(lambda: self.note(slot, f"Note: {windows.REACH_NOTE}")
                          if self.slots.get(slot.service.id) is slot else None)

        threading.Thread(target=check, name=f"reach-{slot.service.id}", daemon=True).start()

    def _launch_after_set_up(self, slot: Slot) -> None:
        if self.slots.get(slot.service.id) is slot and slot.state is State.STARTING and slot.runner is None:
            self._launch(slot, set_up=True)  # not when stopped or removed meanwhile

    def stop(self, sid: str) -> None:
        slot = self.slot(sid)
        slot.restart_at = 0.0
        if slot.shell:  # Stop ends a shell command running next to it too
            slot.shell.kill()
        if slot.task:
            slot.steps.clear()
            slot.task.stop(5)
            return
        if slot.state is State.TASK and slot.steps:  # between two steps: set-up runs in the background
            slot.steps.clear()
            self._task_failed(slot, "stopped")
            return
        if slot.runner and slot.state is State.STOPPING:  # Stop pressed again: don't wait out the grace period
            self.note(slot, "Killed")
            slot.runner.kill()
        elif slot.runner:
            self._set(slot, State.STOPPING, "", "")
            slot.runner.stop(slot.service.stop_timeout)
        elif slot.state in (State.WAITING, State.FAILED, State.STARTING):
            slot.next_run = 0.0
            self._set(slot, State.STOPPED, "stopped", "")

    def restart(self, sid: str) -> None:
        slot = self.slot(sid)
        if slot.runner:
            slot.stop_then = lambda: self.start(sid)
            self.stop(sid)
        else:
            self.start(sid)

    def shell(self, sid: str, line: str) -> None:
        """Run a command typed into the console's shell: in the service's folder and isolation, with its own
        environment first on PATH, next to the running service. While one runs, lines go to its input."""
        slot = self.slot(sid)
        if slot.shell is not None:
            if not slot.shell.write(line + "\n"):
                raise ValueError("The shell command is ending")
            slot.pending.append(slot.log.write(f"\x1b[36m> {line}\x1b[0m\n"))
            return
        service = slot.service
        if slot.task or slot.state is State.TASK:
            raise ValueError("Wait for its install to finish")
        if service.mode is Mode.FULL and not service.full_access_ok:
            raise ValueError("Start it once and confirm Full access first: its shell runs with the same access")
        argv, cmdline = runtimes.shell_argv(line, container=service.mode is Mode.CONTAINER)
        try:
            cmd = isolation.prepare(service, self.dirs, task=argv, cmdline=cmdline)
        except (isolation.IsolationError, OSError) as exc:
            raise ValueError(str(exc)) from exc
        runner = Runner(f"{sid}:shell", cmd, self.queue)  # type: ignore[arg-type]
        try:
            runner.start()
        except OSError as exc:
            raise ValueError(f"Couldn't start the shell: {exc}") from exc
        slot.shell = runner
        slot.pending.append(slot.log.write(f"\x1b[36m$ {line}\x1b[0m\n"))

    def write(self, sid: str, text: str) -> None:
        slot = self.slot(sid)
        runner = slot.runner or slot.task
        if runner is None or not runner.write(text if text.endswith("\n") else text + "\n"):
            raise ValueError("It isn't running")
        slot.pending.append(slot.log.write(f"\x1b[36m> {text.rstrip()}\x1b[0m\n"))

    def start_all(self, ids: list[str] | None = None) -> None:
        """Start every service (or these), in list order, as Settings → Starting services says."""
        wanted = set(ids) if ids is not None else None
        self._start_in_turn([i for i in self.slots if wanted is None or i in wanted], auto=False)

    def _start_in_turn(self, sids: list[str], *, auto: bool) -> None:
        """Start these in list order, one after another by default (Settings: start at once): each start
        (imports, a new environment) is a burst of CPU and disk, and twenty at once stall the whole computer."""
        for sid in sids:
            slot = self.slots.get(sid)
            if slot is None or slot.runner or slot.task or slot.state in (State.STARTING, State.STOPPING, State.TASK):
                continue
            if slot.service.mode is Mode.FULL and not slot.service.full_access_ok:
                continue  # needs the user's consent: Start on that service asks for it
            self._launches = [(s, a) for s, a in self._launches if s != sid]
            self._launches.append((sid, auto))
            slot.restart_at = 0.0
            self._set(slot, State.WAITING, "", QUEUED)
        self._next_launch()

    def _next_launch(self) -> None:
        now = time.monotonic()
        for sid, at in list(self._launching.items()):  # which of those started earlier are still starting
            slot = self.slots.get(sid)
            starting = slot is not None and (slot.state in (State.STARTING, State.TASK) or (
                slot.state is State.RUNNING and now - at < WARMUP))
            if not starting or now - at > LAUNCH_WAIT:
                del self._launching[sid]
        limit = self.settings.start_at_once  # 0: all at once
        while self._launches and (not limit or len(self._launching) < limit):
            sid, auto = self._launches.pop(0)
            slot = self.slots.get(sid)
            if slot is None or slot.state is not State.WAITING or slot.note != QUEUED:
                continue  # stopped, removed or started by hand meanwhile
            self._launching[sid] = now
            with contextlib.suppress(NeedsConfirmation, ValueError, KeyError):
                self.start(sid, auto=auto)

    def reorder(self, ids: list[str]) -> None:
        """The services' order: the list's, and the order Start all and login start them in."""
        known = [i for i in dict.fromkeys(ids) if i in self.slots]
        order = known + [i for i in self.slots if i not in known]
        self.slots = {i: self.slots[i] for i in order}
        self._services_changed()

    def stop_all(self) -> None:
        for sid in list(self.slots):
            self.stop(sid)

    def autostart(self) -> None:
        for slot in self.slots.values():
            if not slot.service.autostart:
                self._plan_schedule(slot)
        self._start_in_turn([s.service.id for s in self.slots.values() if s.service.autostart], auto=True)

    # -- tasks: installing packages, pulling code ---------------------------------------------------------

    def install(self, sid: str) -> None:
        slot = self.slot(sid)
        if slot.runner or slot.task or slot.state is State.TASK:
            raise ValueError("Stop it first")
        steps = self._install_steps(slot.service)
        if not steps:
            raise ValueError("Nothing to install for this project")
        self._run_steps(slot, steps, "Installing packages")

    def compose(self, sid: str, verb: str) -> None:
        """A Compose project's own actions: build or pull its images, take it down (containers and networks; its
        volumes stay), or list its containers. Down stops it first."""
        from ..isolation import compose

        slot = self.slot(sid)
        service = slot.service
        if service.runtime != runtimes.COMPOSE or service.mode is not Mode.CONTAINER:
            raise ValueError("Only for Docker Compose projects")
        if verb not in ("build", "pull", "down", "ps"):
            raise ValueError("Unknown Compose action")
        if slot.task or slot.state is State.TASK:
            raise ValueError("Wait for what it is doing to finish")
        prog = containers.engine(service.container.engine)
        if not prog:
            raise ValueError("No container engine is running: start Docker or Podman")
        try:
            steps = compose.steps(prog, service, verb)
        except isolation.IsolationError as exc:
            raise ValueError(str(exc)) from None
        title = {"build": "Building images", "pull": "Pulling images", "down": "Taking it down",
                 "ps": "Listing containers"}[verb]
        if slot.runner and verb == "down":
            slot.stop_then = lambda: self._run_steps(slot, steps, title)
            self.stop(sid)
            return
        if slot.runner and verb != "ps":
            raise ValueError("Stop it first")
        if slot.runner:  # ps next to the running project: a shell-like side command, its output in the console
            if slot.shell is not None:
                raise ValueError("A console command is still running")
            self.note(slot, "$ compose ps --all")
            try:
                cmd = isolation.prepare(service, self.dirs, task=steps[0])
                runner = Runner(f"{sid}:shell", cmd, self.queue)  # type: ignore[arg-type]
                runner.start()
            except (isolation.IsolationError, OSError) as exc:
                raise ValueError(str(exc)) from None
            slot.shell = runner
            return
        self._run_steps(slot, steps, title)

    def _install_steps(self, service: Service) -> list[list[str]]:
        if service.mode is Mode.CONTAINER:
            return containers.install_steps(service)
        if service.runtime in runtimes.RUNTIMES and (rt := runtimes.RUNTIMES[service.runtime]).install:
            system = next((p for name in rt.programs if (p := runtimes.which(name))), None)
            if not system:
                raise ValueError(f"{rt.label} isn't installed")
            return rt.install(Path(service.folder), system)
        return []

    def _run_steps(self, slot: Slot, steps: list[list[str]], name: str,
                   then: Callable[[], None] | None = None) -> None:
        slot.steps = list(steps)
        slot.task_name = name
        slot.after_steps = then
        self._set(slot, State.TASK, "installing", name)
        self._next_step(slot)

    def recreate(self, sid: str) -> None:
        """Delete the service's environment (its venv, node_modules, vendor or private cache) and install its
        packages again."""
        slot = self.slot(sid)
        if slot.runner or slot.task or slot.state is State.TASK:
            raise ValueError("Stop it first")
        service = slot.service
        folder = Path(service.folder)
        container = service.mode is Mode.CONTAINER
        targets = runtimes.env_folders(service.runtime, folder, container=container)
        if not container and service.runtime in runtimes.CACHE_VARS:
            targets.append(isolation.base.env_cache(self.dirs, service))
        targets = [t for t in targets if t.exists() or t.is_symlink()]
        if not targets and not self._install_steps(service):  # also: refuses if the runtime isn't installed
            raise ValueError("This service has no environment to recreate")
        slot.task_name = "Recreating environment"
        self._set(slot, State.TASK, "installing", "Recreating environment")
        allowed = [folder.resolve(), self.dirs.sandboxes.resolve()]

        def work() -> None:
            error = ""
            for target in targets:
                try:
                    _remove_env(target, allowed)
                except OSError as exc:
                    error = f"couldn't delete {target.name}: {exc.strerror or exc}"
                    break

            def done() -> None:
                if error:
                    self._task_failed(slot, error)
                    return
                if targets:
                    self.note(slot, "Deleted " + ", ".join(_relative(t, folder) for t in targets))
                try:
                    steps = self._install_steps(slot.service)  # now, so a new venv is made first
                except ValueError as exc:
                    self._task_failed(slot, str(exc))
                    return
                if steps:
                    self._run_steps(slot, steps, "Recreating environment")
                    return
                slot.task_name = ""
                self._set(slot, State.STOPPED, "installed", "")
                self.emit({"ev": "notice", "level": "ok", "id": service.id,
                           "text": f"{service.name}: environment cleared, it fills again on the next start"})

            self.post(done)

        threading.Thread(target=work, name=f"recreate-{service.id}", daemon=True).start()

    def create_project(self, key: str, name: str, parent: Path, *, start: bool = False) -> Service:
        """A new service from a built-in template (the Project builder): a new folder, the framework's official
        tool run in the new service's own sandbox, then (``start``) its first start. Raises ValueError."""
        from ..core import templates

        if not parent.is_absolute():
            raise ValueError("The projects folder must be a full path")
        folder = parent / name
        if why := isolation.base.forbidden_write(folder, self.dirs):
            raise ValueError(f"A project can't go in {why}")
        taken = {port for s in self.slots.values() for port in s.service.network.ports}
        plan = templates.plan(key, name, parent, taken)
        service = Service.model_validate({**plan.service, "made_by": {"template": key, "folder": str(folder)}})
        parent.mkdir(parents=True, exist_ok=True)
        templates.write_files(folder, plan.files)
        self._remember_made(folder)
        self.add(service)
        slot = self.slots[service.id]
        self.note(slot, f"New {templates.TEMPLATES[key].label} project in {folder}, made by "
                        f"{templates.TEMPLATES[key].tool}")

        def made() -> None:
            if plan.flatten:
                try:
                    templates.flatten(folder, plan.flatten)
                except OSError as exc:
                    self._task_failed(slot, str(exc))
                    return
            self.note(slot, "Project ready" + (": starting it (its packages are installed first)" if start else
                                               ". Press Start: its packages are installed first"))
            slot.task_name = ""
            self._set(slot, State.STOPPED, "installed", "")
            self.emit({"ev": "notice", "level": "ok", "id": service.id, "text": f"{service.name}: project ready"})
            if start:
                self.start(service.id)

        self._run_steps(slot, plan.steps, CREATING, then=made)
        return service

    def _steps_after_set_up(self, slot: Slot) -> None:
        if self.slots.get(slot.service.id) is slot and slot.state is State.TASK and slot.task is None and slot.steps:
            self._next_step(slot, set_up=True)  # not when stopped or removed meanwhile

    def _next_step(self, slot: Slot, *, set_up: bool = False) -> None:
        if not slot.steps:
            name, slot.task, slot.task_name = slot.task_name, None, ""
            if (then := slot.after_steps) is not None:
                slot.after_steps = None
                if name == MAKING_ENV:
                    self.note(slot, "Environment ready")
                then()
                return
            done = TASK_DONE.get(name, "Packages installed")
            self.note(slot, done)
            self._set(slot, State.STOPPED, "installed", "")
            if name != "Listing containers":
                self.emit({"ev": "notice", "level": "ok", "id": slot.service.id,
                           "text": f"{slot.service.name}: {done.lower()}"})
            return
        if not set_up and self._set_up_first(slot, slot.steps[0], lambda: self._steps_after_set_up(slot)):
            return
        step = slot.steps.pop(0)
        try:
            cmd = isolation.prepare(slot.service, self.dirs, task=step)
        except (isolation.IsolationError, OSError) as exc:
            self._task_failed(slot, str(exc))
            return
        # As it runs (Windows: node.exe npm-cli.js of the private Node copy, not npm.cmd); a container's as typed.
        self.note(slot, f"$ {_shown(cmd.shown if cmd.shown and slot.service.mode is not Mode.CONTAINER else step)}")
        runner = Runner(f"{slot.service.id}:task", cmd, self.queue)  # type: ignore[arg-type]
        try:
            runner.start()
        except OSError as exc:
            self._task_failed(slot, str(exc))
            return
        slot.task = runner

    def _task_step_done(self, slot: Slot, code: int | None) -> None:
        stopped = slot.task is not None and slot.task.stop_requested
        if code != 0 or stopped:
            self._task_failed(slot, "stopped" if stopped else f"the step failed ({_describe_exit(code)})")
            return
        self._next_step(slot)

    def _task_failed(self, slot: Slot, why: str) -> None:
        what = TASK_FAILED.get(slot.task_name, "Install failed")
        slot.task, slot.steps, slot.task_name, slot.after_steps = None, [], "", None
        self.note(slot, f"{what}: {why}")
        self._set(slot, State.FAILED, "install_failed", f"{what}: {why}")
        if slot.stop_then:
            then, slot.stop_then = slot.stop_then, None
            then()

    def pull(self, sid: str) -> None:
        slot = self.slot(sid)
        if slot.task or slot.pulling:
            raise ValueError("Busy")
        if not (Path(slot.service.folder) / ".git").exists():
            raise ValueError("This service's folder isn't a git checkout")
        if slot.service.mode is not Mode.FULL and (slot.runner or slot.shell):
            # git runs as you, outside the sandbox: nothing sandboxed may change the checkout while it does
            raise ValueError("Stop it first: a sandboxed service can't run while git pulls its folder")
        self._pull(slot, then=None)

    def _pull(self, slot: Slot, then: Callable[[], None] | None) -> None:
        self.note(slot, "git pull (fast-forward only)")
        slot.pulling, slot.after_pull = True, then
        if then is None:
            slot.task_name = "Pulling"
            self.emit(self.state_event(slot, "pulling"))

        def work() -> None:
            try:
                changed = git.pull(Path(slot.service.folder),
                                   on_line=lambda line: self.post(lambda: slot.pending.append(slot.log.write(line))))
                result = f"Updated {changed}" if changed else "Already up to date"
                ok = True
            except (git.GitError, OSError) as exc:
                result, ok = f"git pull failed: {exc}", False

            def done() -> None:
                then, slot.after_pull = slot.after_pull, None
                slot.task_name, slot.pulling = "", False
                self.note(slot, result)
                if then is not None and (self.slots.get(slot.service.id) is not slot
                                         or slot.state is not State.STARTING):
                    return  # stopped or removed while it pulled
                if then is not None and ok:
                    then()
                elif then is not None:
                    self._set(slot, State.FAILED, "crashed", result)
                else:
                    self.emit(self.state_event(slot, "pulled"))

            self.post(done)

        threading.Thread(target=work, name=f"pull-{slot.service.id}", daemon=True).start()

    def clear(self, sid: str) -> None:
        self.slot(sid).log.clear()

    def tail(self, sid: str) -> str:
        slot = self.slot(sid)
        return slot.log.tail() + "".join(slot.pending)

    def check(self, sid: str) -> list[tuple[str, str]]:
        return envcheck.service_report(self.slot(sid).service)

    def save_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.store.save_settings(settings)
        for slot in self.slots.values():  # log rotation applies to the open logs at once
            slot.log.max_bytes = settings.log_max_mb * 1024 * 1024
            slot.log.backups = settings.log_backups

    # -- stats --------------------------------------------------------------------------------------------

    def _stats_loop(self) -> None:
        procs: dict[int, psutil.Process] = {}
        n = 0
        while not self._stop.wait(2.0):
            n += 1
            runners = {s.service.id: s.runner for s in list(self.slots.values()) if s.runner and s.runner.pid}
            pids = {sid: r.pid for sid, r in runners.items()}
            kids = _children()
            conns = _connections() if n % 3 == 1 else None
            out: dict[str, Stats] = {}
            for sid, runner in runners.items():
                out[sid] = st = _tree_stats(runner.pid, procs, ports=n % 3 == 1, kids=kids, conns=conns)
                runner.seen = tuple(st.procs)  # Stop ends these too, even ones that left its process group
            if n % 30 == 0:  # forget processes that ended (cpu_percent needs the same object each time)
                procs = {k: v for k, v in procs.items() if v.is_running()}
            system = _system_stats()
            if system:
                system["pythonator"] = _own_stats(procs, set(pids.values()), out.values(), kids)
            self.post(lambda out=out, system=system: self._on_stats(out, system))  # type: ignore[misc]

    def _on_stats(self, stats: dict[str, Stats], system: dict[str, Any] | None = None) -> None:
        items: dict[str, dict[str, Any]] = {}
        for sid, st in stats.items():
            slot = self.slots.get(sid)
            if slot is None or slot.runner is None:
                continue
            if not st.ports and slot.stats.ports and st.processes:
                st.ports, st.remote = slot.stats.ports, slot.stats.remote  # ports are sampled every third time
            slot.stats = st
            self._check_listening(slot)
            limit = slot.service.memory_mb
            if limit and st.memory > limit * 1024 * 1024 and slot.service.mode is not Mode.CONTAINER:
                self.note(slot, f"Over its {limit} MB memory limit ({st.memory // (1024 * 1024)} MB): stopping it")
                slot.runner.kill(f"{limit} MB memory limit")
            items[sid] = {"cpu": round(st.cpu, 1), "memory": st.memory, "processes": st.processes,
                          "ports": st.ports, "remote": st.remote[:50]}
        self.emit({"ev": "stats", "items": items, "system": system or {}})

    def _check_listening(self, slot: Slot) -> None:
        """Once per start, LISTEN_AFTER seconds in: a service that should serve a port but doesn't listen on it
        yet gets a note saying so, with what its processes are doing, for any runtime. A start that hangs without
        a word (a dev server stuck on something) is then visible, with where it is stuck."""
        port = slot.service.network.ports[0] if slot.service.network.enabled and slot.service.network.ports else 0
        if slot.listen_checked or not port or slot.state is not State.RUNNING or slot.runner is None \
                or time.time() - slot.started_wall < LISTEN_AFTER:
            return
        slot.listen_checked = True
        if port in slot.stats.ports or slot.service.mode is Mode.CONTAINER:  # a container's port is the engine's
            return
        others = f" (it listens on {', '.join(map(str, slot.stats.ports))})" if slot.stats.ports else ""
        self.note(slot, f"Not listening on port {port} after {LISTEN_AFTER} s{others}. If it is still starting, "
                        f"wait; else its output above says why. Its processes now: {_process_tree(slot.runner.pid)}")

    # -- crash safety -------------------------------------------------------------------------------------

    def _save_running(self) -> None:
        """running.json, when the set of running services changed (not on every state change)."""
        running: dict[str, list[float]] = {}
        for sid, slot in self.slots.items():
            if slot.runner and slot.runner.pid:
                if not slot.runner.created:
                    with contextlib.suppress(psutil.Error):
                        slot.runner.created = psutil.Process(slot.runner.pid).create_time()
                if slot.runner.created:
                    running[sid] = [slot.runner.pid, slot.runner.created]
        if running == self._running_saved:
            return
        with contextlib.suppress(OSError):
            write_atomic(self.dirs.state_file, json.dumps(running))
            self._running_saved = running

    def reap_orphans(self) -> int:
        """Kill what a crashed earlier daemon left running (same pid *and* start time, so never a reused pid)."""
        try:
            data = json.loads(self.dirs.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        killed = 0
        for value in data.values() if isinstance(data, dict) else []:
            try:
                pid, created = int(value[0]), float(value[1])
                proc = psutil.Process(pid)
                if abs(proc.create_time() - created) > 1:
                    continue
                for p in [*proc.children(recursive=True), proc]:
                    with contextlib.suppress(psutil.Error):
                        p.kill()
                killed += 1
            except (psutil.Error, ValueError, TypeError, IndexError):
                continue
        with contextlib.suppress(OSError):
            self.dirs.state_file.unlink()
        return killed

    def close(self) -> None:
        """End the stats thread. Services keep running unless ``shutdown`` stopped them."""
        self._stop.set()

    def shutdown(self, *, timeout: float = 15.0) -> None:
        """Stop every service and wait for them (up to ``timeout``)."""
        self._stop.set()
        for slot in self.slots.values():
            slot.restart_at = 0.0
            if slot.runner:
                slot.runner.stop(min(slot.service.stop_timeout, timeout))
            if slot.task:
                slot.task.stop(3)
            if slot.shell:
                slot.shell.kill()
        end = time.monotonic() + timeout + 2
        while time.monotonic() < end and any(s.runner or s.task for s in self.slots.values()):
            self.tick()
            time.sleep(0.05)
        for slot in self.slots.values():
            for runner in (slot.runner, slot.task, slot.shell):
                if runner:
                    runner.kill()
            slot.log.flush()
            slot.log.close()
        with contextlib.suppress(OSError):
            self.dirs.state_file.unlink()


def _system_stats() -> dict[str, Any]:
    """The whole computer: CPU in percent of all cores (since the last call), memory in use."""
    try:
        mem = psutil.virtual_memory()
        return {"cpu": round(psutil.cpu_percent(None), 1), "memory": round(mem.percent, 1),
                "memory_total": mem.total, "cores": limits.cores()}
    except (psutil.Error, OSError):
        return {}


def _own_stats(cache: dict[int, psutil.Process], services: set[int], measured: Iterable[Stats],
               kids: dict[int, list[int]] | None = None) -> dict[str, float]:
    """What Pythonator uses: this background service, its plugins (and anything else it started) and every
    service's process tree, already measured in ``measured``. CPU in percent of one core, memory in bytes."""
    cpu = sum(st.cpu for st in measured)
    memory = sum(st.memory for st in measured)
    try:
        me = cache.get(os.getpid()) or cache.setdefault(os.getpid(), psutil.Process())
        with me.oneshot():
            cpu += me.cpu_percent(None)
            memory += me.memory_info().rss
        children = me.children()
    except psutil.Error:
        children = []
    for child in children:
        if child.pid not in services:
            st = _tree_stats(child.pid, cache, ports=False, kids=kids)
            cpu, memory = cpu + st.cpu, memory + st.memory
    return {"cpu": round(cpu, 1), "memory": memory}


def _children() -> dict[int, list[int]]:
    """Every process's children, from one pass over the process table (each ``children(recursive=True)`` would
    make its own)."""
    kids: dict[int, list[int]] = {}
    for p in psutil.process_iter(["ppid"]):
        if (ppid := p.info.get("ppid")) is not None:
            kids.setdefault(ppid, []).append(p.pid)
    return kids


def _connections() -> dict[int, list[Any]] | None:
    """Every process's TCP/UDP connections from one call, or None where only each process may be asked (macOS
    lists other processes' only to root)."""
    if sys.platform == "darwin":
        return None
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return None
    out: dict[int, list[Any]] = {}
    for c in conns:
        if c.pid:
            out.setdefault(c.pid, []).append(c)
    return out


def _tree_stats(pid: int, cache: dict[int, psutil.Process], *, ports: bool,
                kids: dict[int, list[int]] | None = None, conns: dict[int, list[Any]] | None = None) -> Stats:
    st = Stats()
    if kids is None:
        kids = _children()
    order, seen = [pid], {pid}
    for parent in order:  # grows while it is walked: the whole tree, breadth first
        for child in kids.get(parent, ()):
            if child not in seen:
                seen.add(child)
                order.append(child)
    for p in order:
        try:
            proc = cache.get(p) or cache.setdefault(p, psutil.Process(p))
            with proc.oneshot():
                st.cpu += proc.cpu_percent(None)
                st.memory += proc.memory_info().rss
            st.processes += 1
            st.procs.append(proc)
            if ports:
                for c in conns.get(p, []) if conns is not None else proc.net_connections(kind="inet"):
                    if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port not in st.ports:
                        st.ports.append(c.laddr.port)
                    elif c.raddr and c.status == psutil.CONN_ESTABLISHED:
                        st.remote.append(f"{c.raddr.ip}:{c.raddr.port}")
        except psutil.Error:
            continue
    st.ports.sort()
    return st


def _process_tree(pid: int | None) -> str:
    """``node.exe (pid 12, 3% CPU) → cmd.exe /d /s /c "net use" (pid 15, 0% CPU)``: what a service runs now."""
    if not pid:
        return "none"
    try:
        root = psutil.Process(pid)
        tree = [root, *root.children(recursive=True)]
    except psutil.Error:
        return "none"
    parts = []
    for proc in tree[:10]:
        try:
            with proc.oneshot():
                argv = proc.cmdline()
                if proc.name().lower().startswith("bwrap") or any(
                        "launcher import main" in a or a == "--sandbox-exec" for a in argv):
                    continue  # Pythonator's own sandbox launcher
                line = " ".join(Path(argv[0]).name if i == 0 else a for i, a in enumerate(argv[:5])) or proc.name()
                parts.append(f"{line[:120]} (pid {proc.pid}, {sum(proc.cpu_times()[:2]):.1f} s of CPU)")
        except (psutil.Error, IndexError):
            continue
    return " → ".join(parts) or "none"


def _describe_exit(code: int | None) -> str:
    if code is None:
        return "unknown exit"
    if code < 0:
        import signal

        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    if code >= 0x80000000:
        return f"exit code 0x{code:08X}"
    return f"exit code {code}"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _limits_note(service: Service, runner: Runner) -> str:
    """How this service's limits are kept, on this system and in this mode."""
    parts = []
    container, windows = service.mode is Mode.CONTAINER, sys.platform == "win32"
    if service.memory_mb:
        how = "--memory" if container else "job object" if windows else "stopped if it goes over"
        parts.append(f"memory {service.memory_mb} MB ({how})")
    if service.cpu_percent:
        if container:
            parts.append(f"CPU {limits.describe(service.cpu_percent)} (--cpus)")
        elif not runner.cpu_percent:
            parts.append(f"CPU {limits.describe(service.cpu_percent)}: no limit on this computer's "
                         f"{limits.cores()} cores")
        else:
            how = "job object hard cap" if windows else "paused in short slices when over"
            parts.append(f"CPU {limits.describe(service.cpu_percent)} ({how})")
    return "Limits: " + "; ".join(parts)


def _relative(path: Path, folder: Path) -> str:
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return f"its private {path.name} cache"


def _remove_env(path: Path, allowed: list[Path]) -> None:
    """Delete an environment folder; only ever inside the project or Pythonator's sandbox data."""
    if path.is_symlink() or path.is_junction():  # a link to a folder elsewhere: drop the link, never its target
        if path.is_symlink():
            path.unlink()
        else:
            path.rmdir()
        return
    real = path.resolve()
    if not any(real != root and real.is_relative_to(root) for root in allowed):
        raise OSError(f"{real} is outside the project")

    def retry(func: Callable[..., Any], p: str, _exc: BaseException) -> None:  # read-only files (Go, Windows)
        q = Path(p)
        q.chmod(stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if q.is_dir() else 0))
        func(p)

    # Windows: node_modules goes deeper than 260 characters, which only the \\?\ form of a path reaches.
    target = Path("\\\\?\\" + str(real)) if sys.platform == "win32" and not str(real).startswith("\\\\") else real
    for attempt in range(4):  # a virus scanner or the indexer may hold a file for a moment
        try:
            shutil.rmtree(target, onexc=retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))


def _key(folder: Path) -> str:
    """A folder as one string, whatever its spelling: resolved, and on Windows without case or a ``\\\\?\\`` prefix
    (resolve() keeps that prefix when the folder is being deleted)."""
    try:
        text = str(folder.resolve())
    except (OSError, ValueError):
        return ""
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(text)


def _delete_tree(folder: Path) -> str:
    """Delete a project folder the Project builder made: "" when done, else why not. Links inside are removed as
    links (shutil.rmtree never follows them), so nothing outside the folder is touched."""
    try:
        _remove_env(folder, [folder.parent.resolve()])
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


def _shown(argv: list[str]) -> str:
    from ..core.validate import join_command

    return join_command(argv)[:300]
