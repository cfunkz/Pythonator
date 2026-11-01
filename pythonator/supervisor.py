"""Service supervision: the bridge between the Qt-free core and the GUI.

:class:`Supervisor` owns every service's config, log and running process, applies restart
policies, persists configuration and reports changes through Qt signals. It only depends on
QtCore, so it runs fine without a display (tests use it headless).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .core.ansi import strip_ansi
from .core.fixes import DependencyFix, VersionProbe, apply_to_requirements, fix_from_probe, suggest_fix
from .core.launch import (
    LaunchError,
    LaunchSpec,
    build_create_venv,
    build_env,
    build_install,
    build_launch,
    build_shell,
    resolve_python,
    resolve_workdir,
)
from .core.logs import LineKind, LogStore, LogStream
from .core.models import AppConfig, ConsoleShell, Runtime, ServiceConfig
from .core.paths import DataPaths
from .core.policy import decide_restart
from .core.runner import ExitInfo, ProcessRunner
from .core.safety import is_secret_prompt
from .core.schedule import Schedule, ScheduleError, parse_schedule
from .core.shells import command_hint, resolve_shell
from .core.stats import ProcStats, StatsSampler
from .core.store import ConfigStore

_SpecFactory = Callable[[], LaunchSpec]
CLOCK_MS = 1000
INSTALL_LABEL = "Install dependencies"
TASK_STOP_GRACE = 5.0  # seconds a stopped install or $ command gets before it is killed
PROBE_LABEL = "Looking for compatible versions"
# What spawning can raise besides LaunchError: missing/denied files (OSError), NUL bytes or bad
# values in argv/env (ValueError, TypeError), and subprocess's own errors.
SPAWN_ERRORS = (OSError, ValueError, TypeError, subprocess.SubprocessError)

log = logging.getLogger(__name__)


class Status(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    STOPPING = "stopping"
    WAITING = "waiting"  # restart back-off
    EXITED = "exited"  # finished with exit code 0
    FAILED = "failed"  # crashed or could not start

    @property
    def active(self) -> bool:
        return self in (Status.RUNNING, Status.STOPPING, Status.WAITING)


@dataclass(eq=False)
class Service:
    """Live state of one configured service."""

    config: ServiceConfig
    log: LogStore
    status: Status = Status.STOPPED
    runner: ProcessRunner | None = None
    stream: LogStream | None = None
    task: ProcessRunner | None = None
    task_label: str = ""
    task_shell: ConsoleShell | None = None  # the console shell running the current $ command
    task_output_from: int = 0  # log line count when the current task started
    question: DependencyFix | None = None  # waiting for a y/n answer in the console
    probe: VersionProbe | None = None  # the version lookup running as the current task
    started_at: float = 0.0  # wall clock, for uptime
    restarts: int = 0
    failures: int = 0
    last_exit: ExitInfo | None = None
    restart_on_stop: bool = False
    timer: QTimer | None = field(default=None, repr=False)
    schedule: Schedule | None = None
    next_run: float = 0.0  # wall clock of the next scheduled start; 0 when not scheduled

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def pid(self) -> int:
        return self.runner.pid if self.runner and self.runner.running else 0

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at if self.status in (Status.RUNNING, Status.STOPPING) else 0.0


def log_filename(cfg: ServiceConfig) -> str:
    slug = re.sub(r"[^\w.-]+", "_", cfg.name).strip("._") or "service"
    return f"{slug}-{cfg.id[:6]}.log"


class Supervisor(QObject):
    changed = pyqtSignal(str)  # a service's runtime state changed
    services_changed = pyqtSignal()  # services added, removed, renamed or reordered
    notify = pyqtSignal(str, str)  # (title, message): worth a desktop notification
    _exited = pyqtSignal(str, object, object)  # (service id, runner, ExitInfo), cross-thread

    def __init__(self, paths: DataPaths, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.paths = paths.ensure()
        self._store = ConfigStore(paths.config, paths.legacy_config)
        self.config: AppConfig = self._store.load()
        self._save_failed = False
        self._services: dict[str, Service] = {}
        self.stats = StatsSampler()
        self.stats.start()
        self._exited.connect(self._on_exited)
        for cfg in self.config.services:
            self._services[cfg.id] = svc = Service(cfg, self._make_log(cfg))
            self._plan(svc)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._on_clock)
        self._clock.start(CLOCK_MS)

    # -- queries --------------------------------------------------------------------------

    def services(self) -> list[Service]:
        return [self._services[c.id] for c in self.config.services if c.id in self._services]

    def get(self, service_id: str) -> Service | None:
        return self._services.get(service_id)

    def by_name(self, name: str) -> Service | None:
        return next((s for s in self._services.values() if s.name == name), None)

    def stats_for(self, service: Service) -> ProcStats | None:
        return self.stats.get(service.id) if service.pid else None

    def running_count(self) -> int:
        return sum(1 for s in self._services.values() if s.status in (Status.RUNNING, Status.STOPPING))

    # -- configuration --------------------------------------------------------------------

    def save(self) -> bool:
        """Persist the config. A failure (disk full, read-only folder) is reported, never raised."""
        if self._store.read_only:
            return False
        try:
            self._store.save(self.config)
        except (OSError, ValueError, TypeError) as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            log.error("Could not save %s: %s", self._store.path, reason)
            if not self._save_failed:
                self.notify.emit("Settings not saved", f"Could not write {self._store.path}: {reason}")
            self._save_failed = True
            return False
        self._save_failed = False
        return True

    @property
    def recovered_config(self) -> Path | None:
        """The backup the config was restored from, if the main file was damaged."""
        return self._store.recovered_from

    @property
    def config_warning(self) -> str:
        """Why changes may not be kept, for the status bar ("" when all is well)."""
        if self._store.read_only:
            return f"Settings are read-only: {self._store.path} couldn't be read, so it won't be overwritten."
        if self._save_failed:
            return f"Settings couldn't be saved to {self._store.path}."
        return ""

    def apply_settings(self) -> None:
        st = self.config.settings
        for svc in self._services.values():
            svc.log.configure(
                max_lines=st.console_lines, max_bytes=st.log_max_mb * 1024 * 1024, backups=st.log_backups
            )
        self.save()

    def add(self, cfg: ServiceConfig) -> Service:
        self.config.services.append(cfg)
        svc = Service(cfg, self._make_log(cfg))
        self._services[cfg.id] = svc
        self._plan(svc)
        self.save()
        self.services_changed.emit()
        return svc

    def update(self, cfg: ServiceConfig) -> None:
        svc = self._services[cfg.id]
        if cfg.install_confirmed and resolve_workdir(cfg) != resolve_workdir(svc.config):
            cfg = cfg.copy(install_confirmed=False)  # another folder is another project: ask again
        renamed = svc.config.name != cfg.name
        rescheduled = svc.config.schedule != cfg.schedule
        idx = next(i for i, c in enumerate(self.config.services) if c.id == cfg.id)
        self.config.services[idx] = cfg
        svc.config = cfg
        if renamed:
            svc.log.relocate(self.paths.logs / log_filename(cfg))
        if rescheduled:
            self._plan(svc)
        self.save()
        if renamed:
            self.services_changed.emit()
        self.changed.emit(cfg.id)

    def remove(self, service_id: str, *, delete_logs: bool = False) -> None:
        svc = self._services.pop(service_id, None)
        if svc is None:
            return
        self._cancel_timer(svc)
        if svc.runner:
            svc.runner.kill()
        if svc.task:
            svc.task.kill()
        self.stats.untrack(service_id)
        svc.log.close()
        if delete_logs:
            svc.log.clear(delete_files=True)
        self.config.services = [c for c in self.config.services if c.id != service_id]
        self.save()
        self.services_changed.emit()

    def move(self, service_id: str, delta: int) -> None:
        items = self.config.services
        idx = next((i for i, c in enumerate(items) if c.id == service_id), -1)
        new = idx + delta
        if idx < 0 or not 0 <= new < len(items):
            return
        items[idx], items[new] = items[new], items[idx]
        self.save()
        self.services_changed.emit()

    def unique_name(self, base: str) -> str:
        names = {s.name for s in self._services.values()}
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"

    # -- lifecycle ------------------------------------------------------------------------

    def start(self, service_id: str) -> bool:
        svc = self._services.get(service_id)
        if svc is None or svc.status in (Status.RUNNING, Status.STOPPING):
            return False
        self._cancel_timer(svc)
        svc.restart_on_stop = False
        try:
            spec = build_launch(svc.config)
        except LaunchError as exc:
            return self._start_failed(svc, str(exc))
        except (OSError, ValueError) as exc:  # unreadable folder, odd paths, ...
            return self._start_failed(svc, f"Could not prepare the launch: {_reason(exc)}")
        stream = svc.log.stream()

        def on_exit(info: ExitInfo, sid: str = svc.id) -> None:  # runs on the waiter thread
            self._exited.emit(sid, runner, info)

        runner = ProcessRunner(spec, on_output=stream.write, on_exit=on_exit)
        try:
            pid = runner.start()
        except SPAWN_ERRORS as exc:
            stream.close()
            return self._start_failed(svc, f"Could not start `{spec.display()}`: {_reason(exc)}")
        svc.runner, svc.stream = runner, stream
        svc.status = Status.RUNNING
        svc.log.reset_url()
        svc.started_at = time.time()
        venv = f" · venv {spec.venv}" if spec.venv else ""
        svc.log.system(f"▶ Started {svc.name} · pid {pid}\n  $ {spec.display()}\n  cwd {spec.cwd}{venv}")
        self.stats.track(svc.id, pid)
        self.changed.emit(svc.id)
        return True

    def _start_failed(self, svc: Service, message: str) -> bool:
        svc.log.system(message, LineKind.ERROR)
        svc.status = Status.FAILED
        self.changed.emit(svc.id)
        return False

    def stop(self, service_id: str, *, tasks: bool = True) -> None:
        """Stop the service and (unless ``tasks`` is False) any install or $ command running for it.

        The first press asks politely; pressing again while something is still stopping kills it.
        """
        svc = self._services.get(service_id)
        if svc is None:
            return
        if tasks:
            self._stop_task(svc)
        svc.restart_on_stop = False
        if svc.status is Status.WAITING:
            self._cancel_timer(svc)
            svc.status = Status.STOPPED
            svc.log.system("■ Automatic restart cancelled")
            self.changed.emit(svc.id)
            return
        if svc.runner and svc.runner.running:
            if svc.status is Status.STOPPING:
                svc.log.system("■ Killing process tree")
                svc.runner.kill()
                return
            svc.status = Status.STOPPING
            svc.log.system(f"■ Stopping (grace period {svc.config.stop_timeout:g}s)…")
            svc.runner.stop(svc.config.stop_timeout)
            self.changed.emit(svc.id)

    def restart(self, service_id: str) -> None:
        svc = self._services.get(service_id)
        if svc is None:
            return
        if svc.runner and svc.runner.running:
            self.stop(service_id, tasks=False)  # a restart leaves a running install alone
            svc.restart_on_stop = True
        else:
            self.start(service_id)

    def start_all(self) -> None:
        for svc in self.services():
            if not svc.status.active:
                self.start(svc.id)

    def stop_all(self) -> None:
        for svc in self.services():
            self.stop(svc.id)

    def autostart(self) -> None:
        for svc in self.services():
            if svc.config.autostart:
                self.start(svc.id)

    def send_input(self, service_id: str, text: str) -> bool:
        svc = self._services.get(service_id)
        if svc is None or not svc.runner or not svc.runner.running:
            return False
        # Don't write a password or token typed at the program's prompt into the log file.
        shown = "•" * 8 if text and is_secret_prompt(strip_ansi(svc.log.last_output())) else text
        svc.log.system(shown, LineKind.INPUT)
        return svc.runner.send(text + "\n")

    def _on_exited(self, service_id: str, runner: ProcessRunner, info: ExitInfo) -> None:
        svc = self._services.get(service_id)
        if svc is None:
            return
        if runner is svc.task:
            self._on_task_exited(svc, info)
            return
        if runner is not svc.runner:
            return
        if svc.stream:
            svc.stream.close()
        svc.runner = svc.stream = None
        svc.last_exit = info
        self.stats.untrack(svc.id)
        duration = _fmt_duration(info.runtime)

        if info.requested:
            svc.status = Status.STOPPED
            svc.failures = 0
            svc.log.system(f"■ Stopped after {duration} ({info.describe()})")
            self.changed.emit(svc.id)
            if svc.restart_on_stop:
                svc.restart_on_stop = False
                self.start(svc.id)
            return

        if info.success:
            svc.status = Status.EXITED
            svc.log.system(f"● Exited normally after {duration}")
        else:
            svc.status = Status.FAILED
            svc.log.system(f"✖ Crashed after {duration} ({info.describe()})", LineKind.ERROR)
            self.notify.emit(f"{svc.name} crashed", info.describe())

        decision = decide_restart(svc.config.restart, info, svc.failures)
        svc.failures = decision.failures
        if decision.delay is not None:
            svc.status = Status.WAITING
            svc.log.system(f"↻ Restarting in {decision.delay:g}s (policy: {svc.config.restart.value})")
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda sid=svc.id: self._auto_restart(sid))
            timer.start(int(decision.delay * 1000))
            svc.timer = timer
        self.changed.emit(svc.id)

    def _auto_restart(self, service_id: str) -> None:
        svc = self._services.get(service_id)
        if svc is None or svc.status is not Status.WAITING:
            return
        svc.timer = None
        svc.status = Status.STOPPED
        svc.restarts += 1
        self.start(service_id)

    def _cancel_timer(self, svc: Service) -> None:
        if svc.timer is not None:
            svc.timer.stop()
            svc.timer.deleteLater()
            svc.timer = None

    # -- schedules ------------------------------------------------------------------------

    def _plan(self, svc: Service, after: float | None = None) -> None:
        """(Re)compute ``svc.next_run`` from its schedule; ``after`` defaults to now."""
        svc.schedule, svc.next_run = None, 0.0
        try:
            svc.schedule = parse_schedule(svc.config.schedule)
        except ScheduleError as exc:
            svc.log.system(f"Schedule ignored: {exc}", LineKind.ERROR)
            return
        if svc.schedule is not None:
            moment = datetime.fromtimestamp(time.time() if after is None else after)
            svc.next_run = svc.schedule.next_after(moment).timestamp()

    def _on_clock(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for svc in list(self._services.values()):
            if not svc.next_run or now < svc.next_run:
                continue
            try:
                self._run_scheduled(svc, now)
            except Exception:  # one broken schedule must not stop the others
                log.exception("Scheduled run of %s failed", svc.name)
                svc.next_run = 0.0
                svc.log.system("Schedule stopped after an internal error (see pythonator.log).", LineKind.ERROR)

    def _run_scheduled(self, svc: Service, now: float) -> None:
        due = svc.next_run
        # Continue from the planned time so intervals don't drift; after a sleep or a clock
        # change, skip the missed runs instead of starting the service again and again.
        self._plan(svc, after=due)
        if svc.next_run and svc.next_run <= now:
            self._plan(svc, after=now)
        if svc.status.active:
            svc.log.system("⏱ Scheduled run skipped: the previous run is still going")
        else:
            svc.log.system(f"⏱ Scheduled run ({svc.schedule.describe() if svc.schedule else ''})")
            svc.failures = 0
            self.start(svc.id)
        self.changed.emit(svc.id)

    # -- environment tasks ----------------------------------------------------------------

    def run_shell(self, service_id: str, command: str) -> bool:
        svc = self._services.get(service_id)
        if svc is None:
            return False
        svc.log.system(f"$ {command}", LineKind.INPUT)
        choice = self.config.settings.console_shell

        def spec() -> LaunchSpec:
            shell = resolve_shell(choice)
            svc.task_shell = shell.kind
            return build_shell(svc.config, command, shell=shell)

        svc.task_shell = None
        return self._run_task(svc, command, spec, echo=False)

    def create_venv(self, service_id: str) -> bool:
        svc = self._services.get(service_id)
        if svc is None:
            return False
        return self._run_task(svc, "Create venv", lambda: build_create_venv(svc.config))

    def install_requirements(self, service_id: str) -> bool:
        svc = self._services.get(service_id)
        if svc is None:
            return False
        req_file = self.paths.requirements / f"{svc.id}.txt"
        svc.question = None
        return self._run_task(svc, INSTALL_LABEL, lambda: build_install(svc.config, req_file))

    # -- dependency fixes -------------------------------------------------------------------

    def _task_output(self, svc: Service) -> list[str]:
        produced = svc.log.total_lines - svc.task_output_from
        return [ln.text for ln in svc.log.snapshot()[-produced:]] if produced > 0 else []

    def _offer_fix(self, svc: Service) -> None:
        found = suggest_fix(svc.config.runtime, self._task_output(svc))
        if isinstance(found, DependencyFix):
            self._ask(svc, found)
        elif isinstance(found, VersionProbe):
            self._probe_versions(svc, found)

    def _ask(self, svc: Service, fix: DependencyFix) -> None:
        svc.question = fix
        svc.log.system(f"❓ {fix.question}", LineKind.INPUT)
        self.changed.emit(svc.id)

    def _probe_versions(self, svc: Service, probe: VersionProbe) -> None:
        def spec() -> LaunchSpec:
            workdir = resolve_workdir(svc.config)
            python, venv = resolve_python(svc.config, workdir)
            env = build_env(svc.config, venv, None, workdir)
            return LaunchSpec([str(python), "-m", "pip", *probe.pip_args()], workdir, env, venv)

        svc.log.system(f"{probe.old} failed to build. Checking which versions have ready-made builds…")
        svc.probe = probe
        if not self._run_task(svc, PROBE_LABEL, spec, echo=False):
            svc.probe = None

    def _probe_done(self, svc: Service, info: ExitInfo) -> None:
        probe, svc.probe = svc.probe, None
        if probe is None or info.requested:
            return
        fix = fix_from_probe(probe, self._task_output(svc))
        if fix is None:
            svc.log.system(f"No ready-made build of {probe.package} was found for this Python.", LineKind.ERROR)
        else:
            self._ask(svc, fix)

    def answer(self, service_id: str, text: str) -> bool:
        """Handle console input while a question is open. Returns False if nothing was asked."""
        svc = self._services.get(service_id)
        if svc is None or svc.question is None:
            return False
        reply = text.strip().lower()
        svc.log.system(text.strip() or "(empty)", LineKind.INPUT)
        if reply in ("n", "no"):
            svc.question = None
            svc.log.system("Left as it is.")
        elif reply in ("y", "yes"):
            fix, svc.question = svc.question, None
            self._apply_fix(svc, fix)
        else:
            svc.log.system(f"Please type y or n. {svc.question.question}")
        self.changed.emit(svc.id)
        return True

    def _apply_fix(self, svc: Service, fix: DependencyFix) -> None:
        cfg = svc.config
        if cfg.requirements.strip():
            updated = apply_to_requirements(cfg.requirements, fix, cfg.runtime)
            if updated is not None:
                self.update(cfg.copy(requirements=updated.strip()))
                svc.log.system(f"✓ Package list updated: {fix.old} → {fix.new}")
                self.install_requirements(svc.id)
                return
        if cfg.runtime is Runtime.PYTHON:
            req = resolve_workdir(cfg) / "requirements.txt"
            try:
                text = req.read_text(encoding="utf-8")
                updated = apply_to_requirements(text, fix, cfg.runtime)
                if updated is None:
                    svc.log.system(f"{fix.package} isn't in {req}; change it where it's declared.", LineKind.ERROR)
                    return
                req.write_text(updated, encoding="utf-8")
            except OSError as exc:
                svc.log.system(f"Could not update {req}: {_reason(exc)}", LineKind.ERROR)
                return
            svc.log.system(f"✓ {req.name} updated: {fix.old} → {fix.new}")
            self.install_requirements(svc.id)
            return
        # package.json: let the package manager install the new version and record it itself.
        req_file = self.paths.requirements / f"{svc.id}.txt"
        once = cfg.copy(requirements=fix.new)
        self._run_task(svc, INSTALL_LABEL, lambda: build_install(once, req_file))

    def stop_task(self, service_id: str) -> None:
        svc = self._services.get(service_id)
        if svc and svc.task:
            svc.task.kill()

    def _stop_task(self, svc: Service) -> None:
        task = svc.task
        if task is None or not task.running:
            return
        if task.stop_requested:
            svc.log.system(f"■ Killing “{svc.task_label}”")
            task.kill()
        else:
            svc.log.system(f"■ Stopping “{svc.task_label}” (press Stop again to kill it)")
            task.stop(TASK_STOP_GRACE)

    def _run_task(self, svc: Service, label: str, make: _SpecFactory, *, echo: bool = True) -> bool:
        if svc.task is not None:
            svc.log.system(f"Another task is still running ({svc.task_label}). Press Stop (Shift+F5) to end it.",
                           LineKind.ERROR)
            return False
        try:
            spec = make()
        except LaunchError as exc:
            svc.log.system(str(exc), LineKind.ERROR)
            return False
        except (OSError, ValueError) as exc:
            svc.log.system(f"{label} could not be prepared: {_reason(exc)}", LineKind.ERROR)
            return False
        if echo:
            svc.log.system(f"⚙ {label}: {spec.display()}")
        stream = svc.log.stream()

        def on_exit(info: ExitInfo, sid: str = svc.id) -> None:
            stream.close()
            self._exited.emit(sid, runner, info)

        runner = ProcessRunner(spec, on_output=stream.write, on_exit=on_exit)
        try:
            runner.start()
        except SPAWN_ERRORS as exc:
            stream.close()
            svc.log.system(f"{label} could not start: {_reason(exc)}", LineKind.ERROR)
            return False
        svc.task, svc.task_label = runner, label
        svc.task_output_from = svc.log.total_lines
        self.changed.emit(svc.id)
        return True

    def _on_task_exited(self, svc: Service, info: ExitInfo) -> None:
        label, shell = svc.task_label, svc.task_shell
        svc.task, svc.task_label, svc.task_shell = None, "", None
        if label == PROBE_LABEL:  # an internal lookup: its "failure" is the expected answer
            self._probe_done(svc, info)
            self.changed.emit(svc.id)
            return
        if info.requested:
            svc.log.system(f"⚙ {label}: cancelled")
        elif info.success:
            svc.log.system(f"✓ {label}: done in {_fmt_duration(info.runtime)}")
        else:
            svc.log.system(f"✖ {label}: failed ({info.describe()})", LineKind.ERROR)
            if label == INSTALL_LABEL:
                self._offer_fix(svc)
            elif shell is not None and (hint := command_hint(self._task_output(svc), shell=shell)):
                svc.log.system(f"💡 {hint}")
        self.changed.emit(svc.id)

    # -- shutdown -------------------------------------------------------------------------

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop everything, waiting at most ``timeout`` seconds before killing what is left."""
        deadline = time.monotonic() + timeout
        self._clock.stop()
        runners: list[ProcessRunner] = []
        for svc in self._services.values():
            self._cancel_timer(svc)
            svc.restart_on_stop = False
            if svc.task:
                svc.task.kill()
                runners.append(svc.task)
            if svc.runner and svc.runner.running:
                svc.log.system("■ Pythonator is closing; stopping")
                svc.runner.stop(min(svc.config.stop_timeout, timeout))
                runners.append(svc.runner)
        for r in runners:
            if not r.wait(max(0.0, deadline - time.monotonic())):
                r.kill()
        for r in runners:
            r.wait(1.0)
        for svc in self._services.values():
            if svc.runner is not None or svc.status is Status.WAITING:
                svc.runner = svc.stream = None
                svc.status = Status.STOPPED
            svc.task = None
        self.stats.close()
        for svc in self._services.values():
            svc.log.close()

    def _make_log(self, cfg: ServiceConfig) -> LogStore:
        st = self.config.settings
        return LogStore(
            self.paths.logs / log_filename(cfg),
            max_lines=st.console_lines,
            max_bytes=st.log_max_mb * 1024 * 1024,
            backups=st.log_backups,
        )


def _reason(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return f"{exc.strerror}" + (f": {exc.filename}" if exc.filename else "")
    return str(exc) or type(exc).__name__


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    return f"{minutes}m {sec}s"


def format_next_run(ts: float, now: float | None = None) -> str:
    """``14:30`` today, ``tomorrow 03:00``, or ``Mon 12 May 09:00`` further out."""
    when = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    clock = when.strftime("%H:%M:%S" if when.second else "%H:%M")
    days = (when.date() - today).days
    if days <= 0:
        return clock
    if days == 1:
        return f"tomorrow {clock}"
    return f"{when.strftime('%a %d %b')} {clock}"


def format_uptime(seconds: float) -> str:
    return _fmt_duration(seconds) if seconds >= 60 else f"{int(seconds)}s"
