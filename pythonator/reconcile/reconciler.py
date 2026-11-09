"""The reconciler: makes what runs match the specs, once a second, and says why when it can't.
Every action is idempotent and written to the ownership ledger before it happens. A spec whose risk hash you
haven't approved never starts (it shows as Blocked: waiting for your OK). A spec that can't be made safe on this
machine never starts either (Blocked, with the refusal code and the fix)."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from pythonator.ledger.ledger import Entry, Ledger
from pythonator.logs.store import LogStore
from pythonator.model.service import Desired, Mode, Reach, ServiceSpec
from pythonator.model.state import Drift, DriftKind, Observed, Phase
from pythonator.net.gate import Gate, Rule
from pythonator.policy.compile import GATE_KEY_SLOT, Jail, compile_service
from pythonator.policy.host import HostFacts
from pythonator.policy.paths import as_you_clash
from pythonator.policy.plan import EnforcementPlan, Refused
from pythonator.policy.risk import changes, exposure, risk_hash, safe_part, widenings
from pythonator.reconcile import proc, secrets
from pythonator.reconcile.trycopy import TryCopies
from pythonator.safe.hostenv import host_env, resolve
from pythonator.safe.mask import Masker, secret_values
from pythonator.safe.process import NO_WINDOW, write_log
from pythonator.store.files import ensure_private_dir, read_json, write_json
from pythonator.store.specs import SpecStore
from pythonator.work.git import Snapshots
from pythonator.work.installs import Installs

STOP_GRACE_S = 5.0
COOL_DOWN_S = 600.0  # after giving up, one more try this much later
GATE_EVENT = "\x1epythonator-gate "
Emit = Callable[[str, dict[str, Any]], None]

@dataclass
class Run:
    spec: ServiceSpec
    jail: Jail
    proc: asyncio.subprocess.Process
    entry: Entry
    started: float
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    cache: dict[int, psutil.Process] = field(default_factory=dict)
    stopping: bool = False
    stop_reason: str = ""
    gate_key: str = ""  # WSL: signs the gate's event lines on stderr
    last_probe: float = 0.0
    probing: bool = False
    fails: int = 0

class Reconciler:
    def __init__(self, root: Path, host: HostFacts, emit: Emit | None = None) -> None:
        self.root = root
        self.host = host
        self.emit: Emit = emit or (lambda topic, data: None)
        self.store = SpecStore(root)
        ensure_private_dir(root / "logs")
        self.ledger = Ledger(root / "ledger.jsonl")
        self.specs: dict[str, ServiceSpec] = {}
        self.observed: dict[str, Observed] = {}
        self.runs: dict[str, Run] = {}
        self.logs: dict[str, LogStore] = {}
        self.plans: dict[str, Any] = {}
        self.refusals: dict[str, Refused] = {}
        # Blocked after a run that could never work (a layer refused, the engine refused, no such program): held
        # until you start it again or change it, never restarted by the loop.
        self.held: set[str] = set()
        self.fired: dict[str, float] = read_json(root / "schedule.json", {})  # last scheduled run per service
        self.gate = Gate(self.emit)
        self.healed: deque[dict[str, Any]] = deque(maxlen=100)
        self.ignored: set[tuple[str, str]] = set()
        self.tries = TryCopies(Path(host.data_dir or root), host, self.gate)
        self.installs = Installs(root)
        self.snaps = Snapshots(root)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # setup ------------------------------------------------------------------------------------------------------
    async def open(self) -> None:
        self.tries.cleanup()
        try:  # a broken ledger must never keep the background service from starting
            await self.recover()
        except Exception as ex:  # noqa: BLE001
            write_log(self.root / "daemon.log", f"recovery stopped early: {ex!r}")
        self.reload()
        self._task = asyncio.create_task(self._loop(), name="reconcile")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await asyncio.gather(*(self._stop_run(n, "Pythonator is shutting down") for n in list(self.runs)))
        for lg in self.logs.values():
            lg.close()

    def reload(self) -> None:
        self.specs = self.store.load_all()
        for name in list(self.observed):
            if name not in self.specs and name not in self.runs:
                del self.observed[name]
        for name, spec in self.specs.items():
            self.observed.setdefault(name, Observed(name=name))
            self.log(name).set_masker(self._masker(spec))
            self._compile(spec)
        self.poke()

    def poke(self) -> None:
        self._wake.set()

    def log(self, name: str) -> LogStore:
        lg = self.logs.get(name)
        if lg is None:
            lg = self.logs[name] = LogStore(self.root / "logs", name)
        return lg

    def _masker(self, spec: ServiceSpec, *, everything: bool = False) -> Masker:
        """Rebuilt at every start from what the run is really given: keychain secrets plus variables with secret
        names. `everything` adds every non-trivial value, for text that leaves through a plugin."""
        vals = [v for k in spec.secrets if (v := secrets.get(spec.name, k))]
        return Masker(vals + secret_values(spec.env, everything=everything))

    def _compile(self, spec: ServiceSpec, host: HostFacts | None = None) -> Jail | None:
        try:
            jail = compile_service(spec, host or self.host)
        except Refused as r:
            self.refusals[spec.name] = r
            self.plans.pop(spec.name, None)
            return None
        self.refusals.pop(spec.name, None)
        self.plans[spec.name] = jail.plan
        return jail

    # crash recovery ---------------------------------------------------------------------------------------------
    async def recover(self) -> None:
        """Finish or undo whatever the last run of Pythonator left half done. Only ledger rows are touched. Rows
        belong to services and to plugins (`plugin:<id>`); a row that can't be finished is logged and kept, and
        never stops the background service from starting."""
        for e in self.ledger.live():
            try:
                await self._recover_one(e)
            except Exception as ex:  # noqa: BLE001 - one bad row must not keep every service down
                write_log(self.root / "daemon.log", f"recovery: {e.kind} {e.service} {e.ref}: {ex!r}")
        for svc in {e.service for e in self.ledger.live("acl")}:
            if not svc.startswith("plugin:") and self._spec(svc) is None:  # removed while Pythonator wasn't running
                try:
                    await self._revoke_grants(svc)
                except Exception as ex:  # noqa: BLE001
                    write_log(self.root / "daemon.log", f"recovery: folder grants of {svc}: {ex!r}")

    def _spec(self, name: str) -> ServiceSpec | None:
        """The saved spec of a service, or None for a plugin's row or a name that isn't a service."""
        try:
            return self.store.get(name)
        except ValueError:
            return None

    async def _recover_one(self, e: Any) -> None:
        if e.kind == "process":
            pid_s, _, created = e.ref.partition(":")
            if pid_s.isdigit() and created and proc.same_process(int(pid_s), float(created)):
                proc.signal_tree(int(pid_s), proc.TERM)
                await asyncio.sleep(0.5)
                proc.signal_tree(int(pid_s), proc.KILL)
                self.heal(e.service, "ledger · leftover process ended", f"Process {pid_s}, left by an interrupted run.")
            spec = self._spec(e.service)
            if spec is not None and spec.mode is Mode.WSL and (jail := self._compile(spec)) and jail.stop_argv:
                await _quiet(*jail.stop_argv)  # ending wsl.exe doesn't end the run inside Linux
            self.ledger.undone(e)
        elif e.kind == "container":
            engine = resolve(e.extra.get("engine", "podman"))
            if engine:
                p = await asyncio.create_subprocess_exec(
                    engine, "rm", "-f", e.ref, env=host_env(), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, **NO_WINDOW,
                )
                await p.wait()
                self.heal(e.service, "ledger · leftover removed", f"Container {e.ref}, left by an interrupted start.")
            self.ledger.undone(e)

    # Windows folder grants ----------------------------------------------------------------------------------------
    def _record_grants(self, name: str, jail: Jail) -> None:
        """Each folder an AppContainer gets is written to the ledger before the launcher grants it."""
        for path, write in jail.grants:
            ref = f"{jail.box}|{path}"
            if not self.ledger.owns("acl", ref):
                self.ledger.done(self.ledger.intend("acl", name, ref, write=write))

    async def revoke_all(self) -> None:
        """Uninstall: take back every folder permission any service or plugin was given."""
        for name in sorted({e.service for e in self.ledger.live("acl")}):
            await self._revoke_grants(name)

    async def _revoke_grants(self, name: str, keep: frozenset[str] = frozenset()) -> None:
        """Take back this service's folder permissions, except the paths in `keep` (still in its current plan): a
        folder you removed from a service loses its permission at the next start, not at uninstall."""
        entries = [e for e in self.ledger.live("acl", name) if e.ref.partition("|")[2] not in keep]
        if not entries or not self.host.winjail:
            return
        box = entries[0].ref.partition("|")[0]
        paths = [e.ref.partition("|")[2] for e in entries]
        import json

        if (
            await _quiet(self.host.python, "-I", "-S", self.host.winjail, "--revoke", json.dumps({"name": box, "paths": paths}))
            == 0
        ):
            for e in entries:
                self.ledger.undone(e)
        else:
            self.emit("error", {"message": f"Couldn't take back every folder permission of {name}; System check lists them."})

    # the loop ---------------------------------------------------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must survive one bad service
                self.emit("error", {"message": f"reconcile: {e!r}"})
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 1.0)

    async def tick(self) -> None:
        now = time.time()
        for name, spec in self.specs.items():
            ob = self.observed.setdefault(name, Observed(name=name))
            run = self.runs.get(name)
            if run:
                self._sample(name, run, ob)
                if spec.desired is Desired.STOPPED and not run.stopping:
                    asyncio.create_task(self._stop_run(name, "stopped"))
                elif run.spec.health.kind != "off" and not run.stopping and not run.probing:
                    h = run.spec.health
                    if now - run.started >= h.grace_s and now - run.last_probe >= h.every_s:
                        run.probing = True
                        asyncio.create_task(self._probe(name, run, ob))
                continue
            if spec.desired is Desired.STOPPED:
                if ob.phase not in (Phase.STOPPED, Phase.FAILED):
                    self._set(ob, Phase.STOPPED, "")
                continue
            if spec.schedule is not None:
                due = self.next_run(spec)
                if now < due:
                    if ob.phase not in (Phase.STOPPED, Phase.FAILED) or not ob.message:
                        self._set(ob, Phase.STOPPED, f"next run {_when(due)}")
                    continue
                self.fired[name] = now
                write_json(self.root / "schedule.json", self.fired)
                await self._start(spec, ob)
                continue
            if ob.phase is Phase.FAILED:
                if ob.next_retry_at and now >= ob.next_retry_at and ob.restarts > spec.restart.give_up_after:
                    ob.restarts, ob.next_retry_at = 0, None
                    self.heal(name, "cool-down → tried again", f"After {spec.restart.give_up_after} crashes and "
                              f"{COOL_DOWN_S // 60:.0f} minutes.")
                    await self._start(spec, ob)
                continue
            if ob.phase is Phase.BACKOFF and ob.next_retry_at and now < ob.next_retry_at:
                continue
            if name in self.held:
                continue
            await self._start(spec, ob)
        for name in [n for n in self.runs if n not in self.specs]:
            asyncio.create_task(self._stop_run(name, "service removed"))

    def _set(self, ob: Observed, phase: Phase, message: str) -> None:
        if ob.phase != phase or ob.message != message:
            ob.phase, ob.message = phase, message
            self.emit("state", ob.model_dump())

    # start ------------------------------------------------------------------------------------------------------
    async def _start(self, spec: ServiceSpec, ob: Observed) -> None:
        name = spec.name
        if not self.store.is_approved(spec):
            ok = self.store.approved_spec(name)
            if ok is None or self.store.approvals().get(name) != risk_hash(ok):
                self._set(ob, Phase.BLOCKED, "Waiting for your OK: " + "; ".join(widenings(None, spec)[:3]))
                return
            # A saved change waits for your OK on the plan sheet; until then the version you approved keeps running.
            spec = ok.evolve(desired=spec.desired.value)
        others = [(s.name, s.mode is Mode.FULL, [s.folder, *(g.path for g in s.folders if g.write)]) for s in self.specs.values()]
        mine = [spec.folder] if spec.mode is Mode.FULL else [spec.folder, *(g.path for g in spec.folders if g.write)]
        clash = next((c for f in mine if (c := as_you_clash(name, spec.mode is Mode.FULL, f, others))), "")
        if clash:
            self._set(ob, Phase.BLOCKED, f"RUN-FOLDER-001: {clash} shares this folder, and one of the two runs as you "
                      "(Full access) while the other is isolated: the isolated one could change what runs as you. "
                      "Fix: give them separate folders, or the same mode.")
            return
        host = self.host
        if spec.runtime == "python" and spec.runtime_version and spec.mode is not Mode.CONTAINER:
            exe = await self.installs.find(spec.runtime_version)
            if not exe:
                self._set(ob, Phase.BLOCKED, f"RUN-RT-004: Python {spec.runtime_version} isn't installed. Fix: "
                          f"Engines → Runtimes → install Python {spec.runtime_version}.")
                return
            import dataclasses

            host = dataclasses.replace(host, python=exe)
        jail = self._compile(spec, host)
        if jail is None:
            r = self.refusals[name]
            msg = f"{r.code}: {r.message}" + (f" Fix: {r.fix}" if r.fix else "")
            if ob.message != msg:
                self.log(name).append("s", f"refused · {msg}")
            self._set(ob, Phase.BLOCKED, msg)
            return
        env = dict(jail.env)
        missing = []
        for k in spec.secrets:
            v = secrets.get(name, k)
            if v is None:
                missing.append(k)
            else:
                env[k] = v
        if missing:
            self._set(ob, Phase.BLOCKED, f"Secret {missing[0]} isn't set. Set it in the service's Settings → Variables")
            return
        if "WSLENV" in env and spec.secrets:
            env["WSLENV"] = ":".join([env["WSLENV"], *(f"{k}/u" for k in spec.secrets)]).strip(":")
        if not jail.container_name and (busy := _busy_port(spec)):
            self._set(ob, Phase.BLOCKED, f"NET-PORT-001: port {busy} is in use by another program, or reserved by the "
                      f"system. Fix: stop that program, or pick a free port such as {_free_near(busy)}.")
            return
        for d in jail.make_dirs:
            ensure_private_dir(Path(d))
        if jail.grants:
            await self._revoke_grants(name, keep=frozenset(p for p, _w in jail.grants))  # what a saved change dropped
        self._record_grants(name, jail)
        if jail.gate:
            g = jail.gate
            try:
                await self.gate.open(name, Rule(g.hosts, g.allow_lan), unix=g.unix, tcp=g.tcp, ports=g.ports)
            except OSError as e:
                self._set(ob, Phase.BLOCKED, f"NET-GATE-003: the egress gate couldn't open ({e.strerror}). Is the port in use?")
                return
        argv = list(jail.argv)
        gate_key = ""
        if any(GATE_KEY_SLOT in a for a in argv):
            import secrets as _s

            gate_key = _s.token_hex(16)
            argv = [a.replace(GATE_KEY_SLOT, gate_key) for a in argv]
            self.gate.external(name, Rule(spec.network.traffic.hosts, spec.network.traffic.allow_lan))
        kind, ref, extra = "process", "pending", {}
        if jail.container_name:
            engine = resolve(argv[0])
            if not engine:
                self._set(ob, Phase.BLOCKED, f"{argv[0]} isn't installed in a trusted folder.")
                return
            argv[0] = engine
            env = {**host_env(), **env}
            kind, ref, extra = "container", jail.container_name, {"engine": jail.argv[0]}
        elif sys.platform == "win32":
            env = {**host_env(), **env}  # Windows programs need SYSTEMROOT, TEMP and friends to start at all
        self._set(ob, Phase.STARTING, "")
        lg = self.log(name)
        lg.set_masker(self._masker(spec))
        lg.append("s", f"start · generation {spec.generation} · {jail.plan.first_line()}")
        if kind == "container" and spec.container and not self.ledger.owns("image", spec.container.image):
            from pythonator.work.engines import image_present

            if not await image_present(extra["engine"], argv[0], spec.container.image):
                # the engine pulls it on run: Pythonator's from now on, so Engines → Prune can remove it later
                self.ledger.done(self.ledger.intend("image", name, spec.container.image, engine=extra["engine"]))
        if jail.network and not await self._own_network(name, argv[0], jail.network, extra["engine"]):
            self._set(ob, Phase.BLOCKED, f"RUN-CTR-003: {extra['engine']} couldn't create the network {jail.network}.")
            return
        entry = self.ledger.intend(kind, name, ref, **extra)
        t0 = time.perf_counter()
        try:
            p = await asyncio.create_subprocess_exec(
                *argv, cwd=jail.host_cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=1 << 20, **_detach(),
            )
        except OSError as e:
            self.ledger.undone(entry)
            await self.gate.close(name)
            lg.append("s", f"couldn't start: {e}")
            self._after_exit(spec, ob, None, 0.0)
            return
        if kind == "process":
            self.ledger.done(entry, f"{p.pid}:{proc.start_time(p.pid)}")
        else:
            self.ledger.done(entry)
        run = Run(spec, jail, p, entry, time.time(), gate_key=gate_key)
        run.readers = [
            asyncio.create_task(self._pump(name, p.stdout, "o", t0)),
            asyncio.create_task(self._pump(name, p.stderr, "e", None)),
        ]
        self.runs[name] = run
        ob.pid, ob.started_at, ob.exit_code, ob.generation = p.pid, run.started, None, spec.generation
        self._set(ob, Phase.RUNNING, "")
        asyncio.create_task(self._wait(name, run))

    async def _pump(self, name: str, stream: asyncio.StreamReader | None, kind: str, t0: float | None) -> None:
        if stream is None:
            return
        lg = self.log(name)
        dec = codecs.getincrementaldecoder("utf-8")("replace")  # a character split by an over-long line stays whole
        while True:
            try:
                raw = await stream.readuntil(b"\n")
            except asyncio.IncompleteReadError as e:  # the end, maybe with a last line without a newline
                raw = e.partial
            except asyncio.LimitOverrunError as e:  # a line longer than the limit: keep it, in pieces
                raw = await stream.read(e.consumed + 1)
            if not raw:
                return
            if t0 is not None:
                self.emit("bench", {"name": name, "first_line_ms": round((time.perf_counter() - t0) * 1000, 1)})
                t0 = None
            text = dec.decode(raw)
            if "\ufffd" in text and sys.platform == "win32" and b"\xef\xbf\xbd" not in raw:
                text = raw.decode("oem", errors="replace")  # a console tool writing the OEM code page (cp437, cp850…)
            text = text.rstrip("\r\n")
            if kind == "e" and text.startswith(GATE_EVENT) and self._gate_event(name, text):
                continue
            n = lg.append(kind, text)
            self.emit("log", {"name": name, "total": n + 1})

    async def _probe(self, name: str, run: Run, ob: Observed) -> None:
        """One health check. `fails` in a row restarts the service, and the restart is listed under Healed today."""
        h = run.spec.health
        try:
            ok, why = await probe(h.kind, h.port, h.path, h.timeout_s)
        finally:
            run.probing, run.last_probe = False, time.time()
        if self.runs.get(name) is not run or run.stopping:
            return
        run.fails = 0 if ok else run.fails + 1
        state = "ok" if ok else f"failing {run.fails}/{h.fails}"
        if ob.health != state:
            ob.health = state
            self.emit("state", ob.model_dump())
        if not ok:
            self.log(name).append("s", f"health · {h.kind} :{h.port}{h.path if h.kind == 'http' else ''} failed: {why}")
        if run.fails >= h.fails:
            self.heal(name, "health check → restarted", f"{h.fails} failed checks in a row ({why})")
            await self.restart(name)

    def _gate_event(self, name: str, text: str) -> bool:
        """A WSL gate's event line: taken only with a valid signature from this run's key (a service can't fake one)."""
        import hashlib
        import hmac
        import json

        run = self.runs.get(name)
        sig, _, body = text[len(GATE_EVENT):].partition(" ")
        if run is None or not run.gate_key:
            return False
        good = hmac.new(bytes.fromhex(run.gate_key), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return False
        try:
            ev = json.loads(body)
            self.gate.record(name, str(ev["host"]), int(ev["port"]), str(ev["verdict"]), str(ev.get("why", "")))
        except (ValueError, KeyError, TypeError):
            return False
        return True

    async def update_rule(self, name: str, rule: Rule) -> None:
        """A new allow list for a running service, live: the gate here, or the one inside WSL."""
        import json

        self.gate.update(name, rule)
        run = self.runs.get(name)
        if run and run.jail.rule_argv:
            await _quiet(*run.jail.rule_argv, json.dumps({"hosts": list(rule.hosts), "allow_lan": rule.allow_lan}))

    async def _wait(self, name: str, run: Run) -> None:
        code = await run.proc.wait()
        # The run ends when its main program exits, not when every holder of its output pipe has closed: a child
        # that kept stdout open gets 2 s to finish writing, then its group is ended.
        _done, pending = await asyncio.wait(run.readers, timeout=2.0)
        if pending:
            proc.signal_tree(run.proc.pid, proc.KILL)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.gate.close(name)
        self.ledger.undone(run.entry)
        self.runs.pop(name, None)
        ob = self.observed.setdefault(name, Observed(name=name))
        ob.pid, ob.exit_code, ob.cpu_percent, ob.memory_bytes, ob.tasks = None, code, 0.0, 0, 0
        why = run.stop_reason or f"exited with code {code}"
        self.log(name).append("s", f"stop · {why}")
        spec = self.specs.get(name, run.spec)
        if run.stopping:
            self._set(ob, Phase.STOPPED, run.stop_reason)
            return
        if code == 126 and spec.mode is not Mode.CONTAINER:
            self._hold(ob, "A protection layer couldn't be applied, so the service was not run: " + self._last_error(name))
            return
        if spec.mode in (Mode.CONTAINER, Mode.WSL) and time.time() - run.started < 30 and (
            why := self._engine_refused(name, code, spec)
        ):
            self._hold(ob, why)  # the container never ran: restarting would only repeat the error
            return
        if code == 127 and time.time() - run.started < 5:  # the program itself wasn't found: a restart can't help
            self._hold(ob, "The program wasn't found (exit 127); check the command (see log).")
            return
        self._after_exit(spec, ob, code, time.time() - run.started)

    def _hold(self, ob: Observed, why: str) -> None:
        self.held.add(ob.name)
        self._set(ob, Phase.BLOCKED, why)

    def _last_error(self, name: str) -> str:
        """The jail's own reason (its last line before exit 126), or a pointer to the log."""
        for line in reversed(self.log(name).tail(8)):
            text = line.text.strip()
            if text.startswith(("pythonator ", "pythonator:")) or "couldn't" in text:
                return text.split(": ", 1)[-1][:300] if text.startswith("pythonator wsl: ") else text[:300]
        return "see log."

    def _engine_refused(self, name: str, code: int | None, spec: ServiceSpec) -> str:
        """Why the engine couldn't run the container at all (a bad image, a command missing from the image), or ''."""
        tail = " ".join(x.text for x in self.log(name).tail(8)).lower()
        if spec.mode is Mode.WSL:
            if "wsl_e_distro_not_found" in tail or "no distribution with the supplied name" in tail:
                import dataclasses

                (Path(self.host.data_dir or self.root) / "wsl" / "ready").unlink(missing_ok=True)
                self.host = dataclasses.replace(self.host, wsl_ready=False)
                return "RUN-WSL-004: Pythonator's WSL distribution isn't there any more. Fix: Engines → WSL → Set up."
            if "error code: wsl/" in tail:
                return "RUN-WSL-005: WSL couldn't start the service (see log). Fix: run wsl --update, then try again."
            return ""
        image = spec.container.image if spec.container else ""
        if code == 125 and ("pull" in tail or "copy from source" in tail or "manifest" in tail or "not found" in tail):
            return (f"RUN-CTR-004: the image {image} couldn't be downloaded (see log). Fix: check the image name and "
                    "tag in Settings → Isolation, for example python:3.13-slim.")
        if code == 125:
            return "RUN-CTR-005: the engine refused to run the container (exit 125, see log)."
        if code in (126, 127) or "failed to exec pid1" in tail or "executable file not found" in tail:
            return ("RUN-CTR-006: the command can't be run inside the image (see log). Fix: check the command and the "
                    "runtime in Settings → General.")
        return ""

    def _after_exit(self, spec: ServiceSpec, ob: Observed, code: int | None, lasted: float) -> None:
        r = spec.restart
        if spec.schedule is not None and spec.desired is Desired.RUNNING:  # a scheduled run: no restarts, wait for the next
            done = "finished" if code == 0 else f"exited with code {code}"
            self._set(ob, Phase.STOPPED if code == 0 else Phase.FAILED, f"{done} · next run {_when(self.next_run(spec))}")
            return
        if spec.desired is Desired.STOPPED or r.policy == "never" or (r.policy == "on-failure" and code == 0):
            if spec.name in self.specs and spec.desired is Desired.RUNNING:
                self.specs[spec.name] = self.store.set_desired(spec, Desired.STOPPED)  # finished: don't start it again
            self._set(ob, Phase.STOPPED, "finished" if code == 0 else f"exited with code {code}")
            return
        if lasted >= r.stable_after_s:
            ob.restarts = 0
        ob.restarts += 1
        if ob.restarts > r.give_up_after:
            # Giving up isn't forever: one more try after a cool-down (a network outage shouldn't leave it down).
            ob.next_retry_at = time.time() + COOL_DOWN_S
            self._set(ob, Phase.FAILED, f"Crashed {r.give_up_after} times in a row; trying again at "
                      f"{_when(ob.next_retry_at)}. Check the log, or Start now.")
            return
        delay = min(r.backoff_s * (2 ** (ob.restarts - 1)), r.max_backoff_s)
        ob.next_retry_at = time.time() + delay
        self.heal(spec.name, "crash → restarted", f"Exit {code} at {_when(time.time())}, back-off {delay:.0f} s.")
        self._set(ob, Phase.BACKOFF, f"restarting in {delay:.0f} s (attempt {ob.restarts} of {r.give_up_after})")

    def next_run(self, spec: ServiceSpec) -> float:
        """When a scheduled service runs next. Missed runs (the computer was off) run once now if catch_up is on."""
        from cronsim import CronSim  # small, imported only when a schedule exists

        assert spec.schedule is not None  # noqa: S101 - callers check
        last = self.fired.get(spec.name)
        if last is None:
            last = self.fired[spec.name] = time.time()
            write_json(self.root / "schedule.json", self.fired)
        due = next(CronSim(spec.schedule.cron, datetime.fromtimestamp(last))).timestamp()
        if due < time.time() - 60 and not spec.schedule.catch_up:
            due = next(CronSim(spec.schedule.cron, datetime.now())).timestamp()
        return due

    # stats and watchdog -----------------------------------------------------------------------------------------
    def _sample(self, name: str, run: Run, ob: Observed) -> None:
        cpu, mem, n = proc.stats(run.proc.pid, run.cache)
        ob.cpu_percent, ob.memory_bytes, ob.tasks = round(cpu, 1), mem, n
        self.emit("stats", {"name": name, "cpu": ob.cpu_percent, "mem": mem, "tasks": n})
        cap = run.jail.watchdog_memory_mb
        if cap and mem > cap * 1024 * 1024 and not run.stopping and proc.private_bytes(run.proc.pid) > cap * 1024 * 1024:
            asyncio.create_task(self._stop_run(name, f"memory over {cap} MB (watchdog)", crashed=True))

    # stop -------------------------------------------------------------------------------------------------------
    async def _stop_run(self, name: str, reason: str, crashed: bool = False) -> None:
        run = self.runs.get(name)
        if run is None or run.stopping:
            return
        run.stopping = not crashed
        run.stop_reason = reason
        ob = self.observed.setdefault(name, Observed(name=name))
        if not crashed:
            self._set(ob, Phase.STOPPING, reason)
        if run.jail.container_name:
            engine = resolve(run.jail.argv[0])
            if engine:
                p = await asyncio.create_subprocess_exec(
                    engine, "stop", "-t", str(int(STOP_GRACE_S)), run.jail.container_name, env=host_env(),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **NO_WINDOW,
                )
                await p.wait()
        if run.jail.stop_argv:
            await _quiet(*run.jail.stop_argv)
        if sys.platform == "win32":
            # Ctrl+C on the program's own hidden console (Windows has no SIGTERM); the job ends the rest.
            kids = proc.tree(run.proc.pid)[1:2]
            if kids and self.host.winjail and self.host.winjail in run.jail.argv:
                await _quiet(self.host.python, "-I", "-S", self.host.winjail, "--ctrl-c", str(kids[0].pid))
        else:
            proc.signal_tree(run.proc.pid, proc.TERM)
        try:
            await asyncio.wait_for(run.proc.wait(), STOP_GRACE_S)
        except TimeoutError:
            proc.signal_tree(run.proc.pid, proc.KILL)
        if run.jail.marker:
            proc.kill_marked(run.jail.marker)

    # commands from the window or the CLI -------------------------------------------------------------------------
    def apply(self, spec: ServiceSpec) -> dict[str, Any]:
        """Save a spec. Returns the plan, what widens and the hash you would approve."""
        saved = self.store.save(spec)
        self.reload()
        h = risk_hash(saved)
        self.held.discard(saved.name)  # changed: worth trying again
        return {
            "spec": saved.model_dump(mode="json"), "hash": h, "approved": self.store.is_approved(saved),
            "widens": widenings(self.store.approved_spec(spec.name) if self.store.approvals().get(spec.name) else None, saved),
            **self.plan_info(saved.name),
        }

    def preview(self, spec: ServiceSpec) -> dict[str, Any]:
        """The plan a spec would get, without saving anything: what New service and Settings show before you save."""
        old = self.specs.get(spec.name)
        try:
            plan, refused = compile_service(spec, self.host).plan, None
        except Refused as r:
            plan, refused = None, r
        base = self.store.approved_spec(spec.name) if old and self.store.approvals().get(spec.name) else None
        return {
            "spec": spec.model_dump(mode="json"), "hash": risk_hash(spec), "widens": widenings(base, spec),
            "changes": changes(self.store.approved_spec(spec.name) if old else None, spec),
            "generation": [old.generation if old else 0, (old.generation + 1) if old else 1], "exists": old is not None,
            **_plan_dict(plan, refused),
        }

    def plan(self, name: str) -> dict[str, Any]:
        """The plan sheet: the saved spec (or what "Apply safe steps only" left waiting) against the approved one."""
        cur = self.specs[name]
        target = self.store.proposal(name) or cur
        base = self.store.approved_spec(name) if self.store.approvals().get(name) else None
        res = self.preview(target)
        res.update(
            name=name, approved=self.store.is_approved(target), proposal=target is not cur,
            base=base.model_dump(mode="json") if base else None,
            changes=changes(base, target) if not self.store.is_approved(target) else [],
            exposure=[exposure(base) if base else 0.0, exposure(target)],
            generation=[base.generation if base else 0, cur.generation + (1 if target is not cur else 0)],
        )
        return res

    def approve_all(self, name: str, h: str) -> dict[str, Any]:
        """Approve and apply all: the plan's spec (a waiting proposal is saved first), bound to the hash you saw."""
        prop = self.store.proposal(name)
        if prop is not None:
            if risk_hash(prop) != h:
                return {"approved": False}
            self.store.drop_proposal(name)
            self.apply(prop.evolve(desired=self.specs[name].desired.value))
        return {"approved": self.approve(name, h)}

    def apply_safe(self, name: str) -> dict[str, Any]:
        """Apply safe steps only: everything that doesn't widen access applies now; the rest waits as a proposal."""
        cur = self.specs[name]
        base = self.store.approved_spec(name)
        if base is None:
            raise Refused("PLAN-001", f"{name} has never been approved, so every line of its plan needs your OK.",
                          "Review the plan and approve it.")
        wanted = self.store.proposal(name) or cur
        merged = safe_part(base, wanted).evolve(desired=cur.desired.value)
        left = widenings(merged, wanted)
        res = self.apply(merged)
        self.approve(name, res["hash"])
        if left:
            self.store.propose(wanted)
        else:
            self.store.drop_proposal(name)
        return {"applied": len(changes(base, merged)), "waiting": left}

    def plan_info(self, name: str) -> dict[str, Any]:
        return _plan_dict(self.plans.get(name), self.refusals.get(name))

    def approve(self, name: str, h: str) -> bool:
        ok = self.store.approve(name, h)
        if ok:
            spec = self.store.get(name)
            if spec is not None:
                self.snapshot_later(name, spec.folder, f"approved generation {spec.generation}")
            ob = self.observed.get(name)
            self.held.discard(name)
            if ob and ob.phase is Phase.BLOCKED:
                self._set(ob, Phase.STOPPED, "")
            self.poke()
        return ok

    def set_desired(self, name: str, desired: Desired) -> ServiceSpec:
        spec = self.specs[name]
        if spec.desired is not desired:
            spec = self.store.set_desired(spec, desired)
            self.specs[name] = spec
        ob = self.observed.setdefault(name, Observed(name=name))
        self.held.discard(name)  # Start or Stop by you: the next start is a fresh try
        if desired is Desired.RUNNING and ob.phase in (Phase.FAILED, Phase.BACKOFF, Phase.BLOCKED):
            ob.restarts, ob.next_retry_at = 0, None
            self._set(ob, Phase.STOPPED, "")
        self.poke()
        return spec

    def snapshot_later(self, name: str, folder: str, message: str) -> None:
        """A private git snapshot of the folder, in the background (approvals and Keep changes take one)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        t = loop.create_task(self.snaps.snapshot(name, folder, message))
        t.add_done_callback(lambda f: f.exception())

    async def send_stdin(self, name: str, text: str) -> bool:
        """One line to the running program's standard input (the console's "send a line" box)."""
        run = self.runs.get(name)
        if run is None or run.proc.stdin is None or run.proc.stdin.is_closing():
            return False
        if run.proc.stdin.transport.get_write_buffer_size() > 64 * 1024:
            return False  # the program isn't reading its input; never queue more, never wait for it
        run.proc.stdin.write(text.replace("\x00", "")[:65536].encode() + b"\n")
        with contextlib.suppress(OSError, ConnectionError, TimeoutError):
            await asyncio.wait_for(run.proc.stdin.drain(), 2.0)
        self.log(name).append("s", f"stdin · {len(text)} characters sent")
        return True

    async def restart(self, name: str) -> None:
        if name in self.runs:
            await self._stop_run(name, "restart")
            while name in self.runs:
                await asyncio.sleep(0.05)
        self.set_desired(name, Desired.RUNNING)

    async def _own_network(self, name: str, exe: str, net: str, engine: str) -> bool:
        """The service's own Docker network, created once (ledger first) and removed with the service."""
        if await _quiet(exe, "network", "inspect", net, timeout=30) == 0:
            return True
        e = self.ledger.intend("network", name, net, engine=engine)
        ok = await _quiet(exe, "network", "create", "--label", "pythonator.owner=1", "--label",
                          f"pythonator.service={name}", net, timeout=60) == 0
        (self.ledger.done if ok else self.ledger.undone)(e)
        return ok

    async def remove(self, name: str) -> None:
        if name in self.runs:
            await self._stop_run(name, "removed")
            while name in self.runs:
                await asyncio.sleep(0.05)
        await self._revoke_grants(name)
        for e in self.ledger.live("network", name):
            exe = resolve(e.extra.get("engine", "docker"))
            if exe and await _quiet(exe, "network", "rm", e.ref, timeout=60) == 0:
                self.ledger.undone(e)
        self.store.remove(name)
        self.specs.pop(name, None)
        self.observed.pop(name, None)
        self.plans.pop(name, None)
        self.refusals.pop(name, None)
        self.store.drop_proposal(name)
        lg = self.logs.pop(name, None)  # a new service with this name starts with an empty log
        if lg is not None:
            lg.close()
        import shutil

        shutil.rmtree(self.root / "logs" / name, ignore_errors=True)
        if self.fired.pop(name, None) is not None:
            write_json(self.root / "schedule.json", self.fired)
        self.emit("removed", {"name": name})

    async def rename(self, old: str, new: str) -> None:
        """Same service, new name: its log and approval move with it. Its grants are taken back and given again on
        the next start, under the new name (Windows ACLs, the WSL user id and container names all follow the name)."""
        spec = self.specs[old]
        if new in self.specs or old in self.runs:
            raise ValueError(f"{new} already exists." if new in self.specs else f"Stop {old} first.")
        moved = ServiceSpec.model_validate({**spec.model_dump(mode="json"), "name": new, "desired": "stopped"}, strict=False)
        approved = self.store.is_approved(spec)
        if (lg := self.logs.pop(old, None)) is not None:
            lg.close()
        if (self.root / "logs" / old).is_dir():
            (self.root / "logs" / old).rename(self.root / "logs" / new)
        await self.remove(old)
        saved = self.store.save(moved)
        if approved:
            self.store.approve(new, risk_hash(saved))
        self.reload()
        self.poke()

    def heal(self, name: str, title: str, detail: str) -> None:
        """Something the reconciler put right by itself: shown under Healed today."""
        self.healed.append({"name": name, "title": title, "detail": detail, "ts": time.time()})
        self.emit("healed", self.healed[-1])

    async def fix(self, name: str, kind: str, action: str) -> None:
        """A Drift inbox button: heal (make it match the spec), adopt (make the spec match reality) or ignore."""
        if action == "ignore":
            self.ignored.add((name, kind))
        elif action in ("heal", "start") and name in self.specs:
            ob = self.observed.get(name)
            if ob:
                ob.restarts, ob.next_retry_at = 0, None
                self._set(ob, Phase.STOPPED, "")
            self.set_desired(name, Desired.RUNNING)
        elif action == "restart" and name in self.specs:
            await self.restart(name)
        elif action == "adopt" and name in self.specs:
            self.set_desired(name, Desired.STOPPED)
        self.poke()

    def drift(self) -> list[Drift]:
        self.ignored = {(n, k) for n, k in self.ignored if n in self.specs}
        out = [d for d in self._drift() if (d.name, d.kind.value) not in self.ignored]
        return out

    def _drift(self) -> list[Drift]:
        out: list[Drift] = []
        for name, spec in self.specs.items():
            ob = self.observed.get(name)
            if ob is None:
                continue
            if ob.phase is Phase.BLOCKED:
                out.append(Drift(name=name, kind=DriftKind.BLOCKED, detail=ob.message, fixes=("open",)))
            elif ob.phase is Phase.FAILED:
                out.append(Drift(name=name, kind=DriftKind.UNHEALTHY, detail=ob.message, fixes=("heal", "adopt", "ignore")))
            elif name in self.runs and self.runs[name].spec.generation != spec.generation and self.store.is_approved(spec):
                out.append(
                    Drift(
                        name=name, kind=DriftKind.CHANGED,
                        detail=f"running generation {self.runs[name].spec.generation}, saved {spec.generation}",
                        fixes=("restart", "ignore"),
                    )
                )
        for name, broken in self.store.broken.items():
            out.append(Drift(name=name, kind=DriftKind.BLOCKED, detail=f"can't read the file: {broken}", fixes=()))
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "services": [
                {
                    "spec": s.model_dump(mode="json"), "observed": self.observed.get(n, Observed(name=n)).model_dump(),
                    "approved": self.store.is_approved(s), "hash": risk_hash(s), "log_total": self.log(n).total,
                    "changes": [] if self.store.is_approved(s) else changes(self.store.approved_spec(n), s),
                    "next_run": self.next_run(s) if s.schedule else None,
                    "waiting": len(widenings(s, prop)) if (prop := self.store.proposal(n)) else 0,
                    "exposure": exposure(s), **self.plan_info(n),
                }
                for n, s in sorted(self.specs.items())
            ],
            "drift": [d.model_dump() for d in self.drift()],
            "healed": [h for h in self.healed if h["ts"] > time.time() - 86400],
        }

def _plan_dict(plan: EnforcementPlan | None, r: Refused | None) -> dict[str, Any]:
    return {
        "plan": None
        if plan is None
        else {
            "os": plan.os, "mode": plan.mode, "first_line": plan.first_line(), "weakest": str(plan.weakest()),
            "layers": [{"key": x.key, "title": x.title, "status": str(x.status), "by": x.by, "detail": x.detail} for x in plan.layers],
        },
        "refused": None if r is None else {"code": r.code, "message": r.message, "fix": r.fix},
    }

async def _quiet(*argv: str, timeout: float = 120) -> int:
    """Run a helper with Pythonator's own environment, no window, no output. Its exit code, or -1."""
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, env=host_env(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, **_detach(),
        )
        return await asyncio.wait_for(p.wait(), timeout)
    except (OSError, TimeoutError):
        return -1

def _bindable(port: int, lan: bool) -> bool:
    import socket

    with socket.socket() as s:
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TIME_WAIT from our own last run is fine
        try:
            s.bind(("0.0.0.0" if lan else "127.0.0.1", port))  # noqa: S104 - a test bind, closed at once
            return True
        except OSError:
            return False

def _busy_port(spec: ServiceSpec) -> int:
    """The first of the service's ports another program holds (or Windows reserved for Hyper-V and WSL), or 0."""
    for p in spec.network.ports:
        if not _bindable(p.port, p.reach is Reach.LAN):
            return p.port
    return 0

def _free_near(port: int) -> int:
    return next((q for q in range(port + 1, min(port + 50, 65535)) if _bindable(q, False)), 0)

def _when(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d:%H:%M}" if d.date() == datetime.now().date() else f"{d:%a %d %b %H:%M}"

def _detach() -> dict[str, Any]:
    """Own session (POSIX) or process group with no console window (Windows), so Stop reaches the whole tree."""
    if sys.platform == "win32":
        import subprocess

        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}

async def probe(kind: str, port: int, path: str, timeout: float) -> tuple[bool, str]:
    """TCP: the port accepts a connection. HTTP: GET path answers with a status below 500."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
    except (OSError, TimeoutError) as e:
        return False, "connection refused" if isinstance(e, ConnectionRefusedError) else "no answer"
    try:
        if kind == "tcp":
            return True, ""
        w.write(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nUser-Agent: pythonator-health\r\n\r\n".encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout)
        parts = line.decode("latin-1").split(" ")
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return (0 < code < 500), f"HTTP {code}" if code else "not HTTP"
    except (OSError, TimeoutError, ValueError):
        return False, "no answer"
    finally:
        w.close()
