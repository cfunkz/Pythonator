"""The supervisor: owns every service, starts and stops it, restarts it when it crashes, and records its output.

All state lives on the daemon's main (Qt) thread. Runner threads and the stats thread only put events into one
queue, which :meth:`Supervisor.tick` drains every 50 ms, so nothing here needs a lock.
"""

from __future__ import annotations

import codecs
import contextlib
import json
import logging
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
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
LIVE_LIMIT = 128 * 1024  # characters of output per service per tick sent to windows; the log gets everything


class State(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    WAITING = "waiting"  # a restart or the next scheduled run is due later
    FAILED = "failed"
    TASK = "task"  # installing packages or pulling code


class NeedsConfirmation(Exception):
    """Full access must be confirmed by the user once per service."""


@dataclass
class Stats:
    cpu: float = 0.0
    memory: int = 0
    processes: int = 0
    ports: list[int] = field(default_factory=list)
    remote: list[str] = field(default_factory=list)  # outgoing connections, "host:port"


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
    stats: Stats = field(default_factory=Stats)
    pending: list[str] = field(default_factory=list)
    partial: str = ""
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    stop_then: Callable[[], None] | None = None  # what to do once it stopped (restart, remove)


class Supervisor:
    def __init__(self, dirs: DataDirs, store: Store) -> None:
        self.dirs = dirs
        self.store = store
        self.settings: Settings = store.load_settings()
        self.slots: dict[str, Slot] = {}
        self.listeners: list[Listener] = []
        self.gate: Callable[[Service, Callable[[str], None]], None] | None = None  # policy plugins
        self.queue: queue.SimpleQueue[Event | Callable[[], None]] = queue.SimpleQueue()
        self._pids: dict[str, int] = {}  # read by the stats thread (a copy is swapped in, never mutated)
        self._last_second = 0.0
        self._stop = threading.Event()
        for service in store.load_services():
            self.slots[service.id] = Slot(service, self._new_log(service))
        threading.Thread(target=self._stats_loop, name="stats", daemon=True).start()

    # -- plumbing ---------------------------------------------------------------------------------------

    def _new_log(self, service: Service) -> ServiceLog:
        log_ = ServiceLog(self.dirs.logs / f"{service.id}.log", max_bytes=self.settings.log_max_mb * 1024 * 1024,
                          backups=self.settings.log_backups)
        env = dict(service.env)
        if service.env_file:
            env.update(isolation.base.read_env_file(Path(service.folder) / service.env_file))
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
                    skipped = len(text) - LIVE_LIMIT // 2
                    text = f"\x1b[2m[… {skipped:,} characters skipped in the live view, all in the log]\x1b[0m\n" \
                           + text[-LIVE_LIMIT // 2:]
                self.emit({"ev": "output", "id": slot.service.id, "text": text})
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
        slot = self.slots.get(item.key.split(":")[0])
        if slot is None or not data:
            return
        text = slot.partial + slot.decoder.decode(data)
        cut = text.rfind("\n") + 1
        if cut == 0 and len(text) < 4096:
            slot.partial = text
            return
        if cut == 0:
            cut = len(text)
        slot.partial = text[cut:]
        slot.pending.append(slot.log.write(text[:cut]))

    def _flush_partial(self, slot: Slot) -> None:
        rest = slot.partial + slot.decoder.decode(b"", final=True)
        slot.partial = ""
        if rest:
            slot.pending.append(slot.log.write(rest + "\n"))

    def _on_exit(self, item: Exited) -> None:
        sid, _, kind = item.key.partition(":")
        slot = self.slots.get(sid)
        if slot is None:
            return
        runner = slot.task if kind == "task" else slot.runner
        if runner is not None:  # the last of its output, before the exit is reported
            self._on_output(Output(item.key, runner))
        self._flush_partial(slot)
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
        self.slots[service.id] = Slot(service, self._new_log(service))
        self._services_changed()
        self.emit(self.state_event(self.slots[service.id]))
        self._plan_schedule(self.slots[service.id])

    def update(self, service: Service) -> None:
        slot = self.slot(service.id)
        slot.service = service
        slot.log.close()
        slot.log.mask = self._new_log(service).mask
        self._services_changed()
        if slot.runner:
            self.note(slot, "Saved. Restart it to use the new settings.")
        self._plan_schedule(slot)

    def remove(self, sid: str) -> None:
        slot = self.slot(sid)

        def gone() -> None:
            slot.log.close()
            self.slots.pop(sid, None)
            self._services_changed()
            with contextlib.suppress(OSError):
                isolation.forget(slot.service, self.dirs)

        if slot.runner or slot.task:
            slot.stop_then = gone
            self.stop(sid)
        else:
            gone()

    def start(self, sid: str, *, confirmed: bool = False, auto: bool = False) -> None:
        slot = self.slot(sid)
        if slot.runner or slot.task or slot.state in (State.STARTING, State.STOPPING):
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
        if slot.service.git and slot.service.git.pull_on_start:
            self._pull(slot, then=lambda: self._launch(slot))
            return
        self._launch(slot)

    def _launch(self, slot: Slot) -> None:
        service = slot.service
        for level, text in envcheck.service_report(service):
            if level != "ok":
                self.note(slot, f"{'Problem' if level == 'error' else 'Note'}: {text}")
        try:
            cmd = isolation.prepare(service, self.dirs)
        except (isolation.IsolationError, OSError) as exc:
            self.note(slot, f"Not started: {exc}")
            self._set(slot, State.FAILED, "crashed", str(exc))
            return
        runner = Runner(service.id, cmd, self.queue, memory_mb=service.memory_mb,  # type: ignore[arg-type]
                        cpu_percent=service.cpu_percent)
        try:
            runner.start()
        except OSError as exc:
            self.note(slot, f"Couldn't start: {exc}")
            self._set(slot, State.FAILED, "crashed", str(exc))
            return
        slot.runner = runner
        slot.exit_code = None
        slot.started_wall = time.time()
        slot.next_run = 0.0
        self.note(slot, f"Started {_shown(cmd.shown or cmd.argv)} · pid {runner.pid}")
        self.note(slot, cmd.describe)
        if service.cpu_percent or service.memory_mb:
            self.note(slot, _limits_note(service, runner))
        self._set(slot, State.RUNNING, "started", "")

    def stop(self, sid: str) -> None:
        slot = self.slot(sid)
        slot.restart_at = 0.0
        if slot.task:
            slot.steps.clear()
            slot.task.stop(5)
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

    def write(self, sid: str, text: str) -> None:
        slot = self.slot(sid)
        runner = slot.runner or slot.task
        if runner is None or not runner.write(text if text.endswith("\n") else text + "\n"):
            raise ValueError("It isn't running")
        slot.pending.append(slot.log.write(f"\x1b[36m> {text.rstrip()}\x1b[0m\n"))

    def start_all(self) -> None:
        for sid in list(self.slots):
            with contextlib.suppress(NeedsConfirmation):
                self.start(sid)

    def stop_all(self) -> None:
        for sid in list(self.slots):
            self.stop(sid)

    def autostart(self) -> None:
        for slot in self.slots.values():
            if slot.service.autostart:
                with contextlib.suppress(NeedsConfirmation):
                    self.start(slot.service.id, auto=True)
            else:
                self._plan_schedule(slot)

    # -- tasks: installing packages, pulling code ---------------------------------------------------------

    def install(self, sid: str) -> None:
        slot = self.slot(sid)
        if slot.runner or slot.task:
            raise ValueError("Stop it first")
        service = slot.service
        folder = Path(service.folder)
        if service.mode is Mode.CONTAINER:
            steps = containers.install_steps(service)
        elif service.runtime in runtimes.RUNTIMES and (rt := runtimes.RUNTIMES[service.runtime]).install:
            system = next((p for name in rt.programs if (p := runtimes.which(name))), None)
            if not system:
                raise ValueError(f"{rt.label} isn't installed")
            steps = rt.install(folder, system)
        else:
            steps = []
        if not steps:
            raise ValueError("Nothing to install for this project")
        slot.steps = list(steps)
        slot.task_name = "Installing packages"
        self._set(slot, State.TASK, "installing", "Installing packages")
        self._next_step(slot)

    def _next_step(self, slot: Slot) -> None:
        if not slot.steps:
            slot.task, slot.task_name = None, ""
            self.note(slot, "Packages installed")
            self._set(slot, State.STOPPED, "installed", "")
            self.emit({"ev": "notice", "level": "ok", "id": slot.service.id,
                       "text": f"{slot.service.name}: packages installed"})
            return
        step = slot.steps.pop(0)
        try:
            cmd = isolation.prepare(slot.service, self.dirs, task=step)
        except (isolation.IsolationError, OSError) as exc:
            self._task_failed(slot, str(exc))
            return
        self.note(slot, f"$ {_shown(step)}")
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
        slot.task, slot.steps, slot.task_name = None, [], ""
        self.note(slot, f"Install failed: {why}")
        self._set(slot, State.FAILED, "install_failed", f"Install failed: {why}")
        if slot.stop_then:
            then, slot.stop_then = slot.stop_then, None
            then()

    def pull(self, sid: str) -> None:
        slot = self.slot(sid)
        if slot.task:
            raise ValueError("Busy")
        if not (Path(slot.service.folder) / ".git").exists():
            raise ValueError("This service's folder isn't a git checkout")
        self._pull(slot, then=None)

    def _pull(self, slot: Slot, then: Callable[[], None] | None) -> None:
        self.note(slot, "git pull (fast-forward only)")
        if then is None:
            slot.task_name = "Pulling"
            self.emit(self.state_event(slot, "pulling"))

        def work() -> None:
            try:
                changed = git.pull(Path(slot.service.folder),
                                   on_line=lambda line: self.post(lambda: slot.pending.append(slot.log.write(line))))
                result = f"Updated {changed}" if changed else "Already up to date"
                ok = True
            except git.GitError as exc:
                result, ok = f"git pull failed: {exc}", False

            def done() -> None:
                slot.task_name = ""
                self.note(slot, result)
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

    # -- stats --------------------------------------------------------------------------------------------

    def _stats_loop(self) -> None:
        procs: dict[int, psutil.Process] = {}
        n = 0
        while not self._stop.wait(2.0):
            n += 1
            pids = {s.service.id: s.runner.pid for s in list(self.slots.values()) if s.runner and s.runner.pid}
            out: dict[str, Stats] = {}
            for sid, pid in pids.items():
                out[sid] = _tree_stats(pid, procs, ports=n % 3 == 1)
            if n % 30 == 0:  # forget processes that ended (cpu_percent needs the same object each time)
                procs = {k: v for k, v in procs.items() if v.is_running()}
            system = _system_stats()
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
            limit = slot.service.memory_mb
            if limit and st.memory > limit * 1024 * 1024 and slot.service.mode is not Mode.CONTAINER:
                self.note(slot, f"Over its {limit} MB memory limit ({st.memory // (1024 * 1024)} MB): stopping it")
                slot.runner.kill(f"{limit} MB memory limit")
            items[sid] = {"cpu": round(st.cpu, 1), "memory": st.memory, "processes": st.processes,
                          "ports": st.ports, "remote": st.remote[:50]}
        self.emit({"ev": "stats", "items": items, "system": system or {}})

    # -- crash safety -------------------------------------------------------------------------------------

    def _save_running(self) -> None:
        running: dict[str, list[float]] = {}
        for sid, slot in self.slots.items():
            if slot.runner and slot.runner.pid:
                with contextlib.suppress(psutil.Error):
                    running[sid] = [slot.runner.pid, psutil.Process(slot.runner.pid).create_time()]
        with contextlib.suppress(OSError):
            write_atomic(self.dirs.state_file, json.dumps(running))

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
        end = time.monotonic() + timeout + 2
        while time.monotonic() < end and any(s.runner or s.task for s in self.slots.values()):
            self.tick()
            time.sleep(0.05)
        for slot in self.slots.values():
            for runner in (slot.runner, slot.task):
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


def _tree_stats(pid: int, cache: dict[int, psutil.Process], *, ports: bool) -> Stats:
    st = Stats()
    try:
        root = cache.get(pid) or cache.setdefault(pid, psutil.Process(pid))
        tree = [root, *root.children(recursive=True)]
    except psutil.Error:
        return st
    for p in tree:
        proc = cache.setdefault(p.pid, p)
        try:
            with proc.oneshot():
                st.cpu += proc.cpu_percent(None)
                st.memory += proc.memory_info().rss
            st.processes += 1
            if ports:
                for c in proc.net_connections(kind="inet"):
                    if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port not in st.ports:
                        st.ports.append(c.laddr.port)
                    elif c.raddr and c.status == psutil.CONN_ESTABLISHED:
                        st.remote.append(f"{c.raddr.ip}:{c.raddr.port}")
        except psutil.Error:
            continue
    st.ports.sort()
    return st


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


def _shown(argv: list[str]) -> str:
    from ..core.validate import join_command

    return join_command(argv)[:300]
