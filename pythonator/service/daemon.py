"""pythonatord: the background service. It owns the reconciler; windows and the CLI are clients.
Closing the window never stops your services. Requests are JSON-RPC style:
    -> {"id": 1, "method": "start", "params": {"name": "api"}}
    <- {"id": 1, "result": {...}}  or  {"id": 1, "error": {"code": "...", "message": "...", "fix": "..."}}
Events go to clients that subscribed: {"event": "state"|"log"|"stats"|"bench"|"removed"|"error", "data": {...}}"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import struct
import sys
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pythonator import __version__
from pythonator.model.service import Desired, Mode, ServiceSpec
from pythonator.net.gate import Rule
from pythonator.plugins.host import PluginHost
from pythonator.plugins.model import BadPlugin
from pythonator.policy.host import HostFacts, describe, probe
from pythonator.policy.plan import Refused
from pythonator.policy.risk import exposure, projection, risk_hash, widenings
from pythonator.reconcile.reconciler import Reconciler
from pythonator.safe.mask import Masker
from pythonator.service import check, ipc
from pythonator.store.events import SecurityLog
from pythonator.store.files import data_dir, write_atomic

STATS_EVERY_S = 1.0

class Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.topics: set[str] = set()
        self.logs: set[str] = set()  # service names whose new-line counts this client wants

    def send(self, obj: dict[str, Any]) -> None:
        if self.writer.is_closing():
            return
        if self.writer.transport.get_write_buffer_size() > 8 * 1024 * 1024:
            return  # a client that stopped reading drops events instead of growing our memory
        self.writer.write(ipc.dumps(obj))

class Daemon:
    def __init__(self, root: Path, host: HostFacts | None = None) -> None:
        self.root = root
        self.host = host or probe(str(root))
        self.key = ipc.load_key(root, create=True)
        self.clients: set[Client] = set()
        self.tasks: set[asyncio.Task[None]] = set()  # requests being answered
        self.jobs: dict[str, dict[str, Any]] = {}  # long jobs running now: key -> title, last line, start
        self.security = SecurityLog(root / "security.jsonl")
        self.rec = Reconciler(root, self.host, self._emit)
        self.bench = check.Bench(root)
        self.plugins = PluginHost(root, self.host, self)
        self.started = time.time()
        self._stop = asyncio.Event()
        self._last_stats: dict[str, float] = {}
        self.engines: dict[str, str] = {}
        self._engines_probed = 0.0

    # events -----------------------------------------------------------------------------------------------------
    def _emit(self, topic: str, data: dict[str, Any]) -> None:
        if topic == "stats":  # at most once a second per service, whatever the reconciler does
            now = time.monotonic()
            if now - self._last_stats.get(data["name"], 0) < STATS_EVERY_S:
                return
            self._last_stats[data["name"]] = now
        if topic == "bench" and data.get("name") == "__bench__":
            return
        if topic == "state":
            self.plugins.on_event(topic, data)
        if topic == "traffic" and data.get("verdict") == "blocked":
            fixable = data.get("why") == "not in the allowed hosts"
            self.security.add("blocked", data["name"], f"Blocked {data['host']}:{data['port']}",
                              "not on allow-list" if fixable else f"always blocked · {data.get('why', '')}",
                              {"kind": "allow", "service": data["name"], "host": data["host"]} if fixable else None)
        elif topic == "healed":
            who = "ledger" if str(data.get("title", "")).startswith("ledger") else data.get("name", "")
            self.security.add("healed", who, str(data.get("detail") or data.get("title")),
                              str(data.get("title", "")) + (f" · {data['name']}" if who == "ledger" else ""))
        msg = {"event": topic, "data": data}
        for c in list(self.clients):
            if topic == "log":
                if data["name"] in c.logs or "log" in c.topics:
                    c.send(msg)
            elif topic in c.topics or "*" in c.topics:
                c.send(msg)

    # connection -------------------------------------------------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None and sys.platform == "linux" and sock.family == socket.AF_UNIX:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            if uid != os.getuid():
                writer.close()
                return
        if sock is not None and sock.family == getattr(socket, "AF_UNIX", -1):
            with contextlib.suppress(OSError):  # macOS defaults to 8 KB: the window fell behind under output floods
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        n_s = ipc.nonce()
        writer.write(ipc.dumps({"hello": n_s, "protocol": ipc.PROTOCOL, "version": __version__}))
        try:
            auth = ipc.loads(await asyncio.wait_for(reader.readline(), 5))
        except (TimeoutError, ValueError):
            writer.close()
            return
        n_c = auth.get("nonce")
        if not isinstance(n_c, str) or not ipc.check(self.key, b"client", n_s, n_c, auth.get("proof")):
            writer.write(ipc.dumps({"error": {"code": "IPC-AUTH-001", "message": "not authorised"}}))
            writer.close()
            return
        writer.write(ipc.dumps({"proof": ipc.proof(self.key, b"server", n_c, n_s)}))
        client = Client(writer)
        self.clients.add(client)
        try:
            while not reader.at_eof():
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break
                if not raw:
                    break
                try:
                    req = ipc.loads(raw)
                except ValueError:
                    break
                # Each request is its own task, so a long one (WSL setup, an image pull, an install) never holds up
                # the next: the window keeps opening files and starting services. Replies carry the request's id.
                task = asyncio.create_task(self._answer(client, req))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        finally:
            self.clients.discard(client)
            writer.close()

    async def _answer(self, client: Client, req: dict[str, Any]) -> None:
        rid = req.get("id")
        try:
            result = await self.call(client, str(req.get("method", "")), req.get("params") or {})
            client.send({"id": rid, "result": result})
        except ipc.RpcError as e:
            client.send({"id": rid, "error": {"code": e.code, "message": e.message, "fix": e.fix}})
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first.get("loc", ()))
            client.send({"id": rid, "error": {"code": "SPEC-001", "message": f"{where}: {first.get('msg')}", "fix": ""}})
        except Exception as e:  # noqa: BLE001
            client.send({"id": rid, "error": {"code": "INTERNAL", "message": repr(e), "fix": ""}})
        with contextlib.suppress(ConnectionError, RuntimeError):
            if not client.writer.is_closing():
                await client.writer.drain()

    @contextlib.asynccontextmanager
    async def _job(self, key: str, title: str) -> AsyncIterator[Callable[[str], None]]:
        """A long job (WSL setup, an image pull, a Python install): one at a time per key, shown in the window's
        status bar with its latest line. `say` may be called from a worker thread."""
        if key in self.jobs:
            raise ipc.RpcError("JOB-BUSY", f"{title} is already running.", "Wait for it to finish.")
        loop = asyncio.get_running_loop()
        self.jobs[key] = {"title": title, "line": "", "started": time.time()}
        self._emit("job", {"key": key, **self.jobs[key]})

        def line(text: str) -> None:
            if key in self.jobs:
                self.jobs[key]["line"] = text[:200]
                self._emit("job", {"key": key, **self.jobs[key]})

        try:
            yield lambda text: loop.call_soon_threadsafe(line, text)
        finally:
            self.jobs.pop(key, None)
            self._emit("job", {"key": key, "done": True})

    # methods ----------------------------------------------------------------------------------------------------
    def _need(self, name: Any) -> str:
        if not isinstance(name, str) or name not in self.rec.specs:
            raise ipc.RpcError("SVC-404", f"No service called {name!r}.")
        return name

    async def call(self, client: Client, method: str, p: dict[str, Any]) -> Any:
        r = self.rec
        if method == "ping":
            return {"version": __version__, "uptime": time.time() - self.started}
        if method == "subscribe":
            client.topics = {t for t in p.get("topics", []) if isinstance(t, str)}
            return True
        if method == "watch_logs":
            client.logs = {n for n in p.get("names", []) if isinstance(n, str)}
            return True
        if method == "snapshot":
            return {
                **r.snapshot(),
                "host": self._host(),
                "jobs": [{"key": k, **v} for k, v in self.jobs.items()],
                "daemon": {
                    "version": __version__, "uptime": time.time() - self.started,
                    "memory_mb": round(check.daemon_mb(), 1),
                },
            }
        if method == "apply":
            spec = ServiceSpec.model_validate(p.get("spec"), strict=False)
            old = r.specs.get(spec.name)
            await self._guard(spec, old)
            return r.apply(spec)
        if method.startswith("plugins."):
            return await self._plugins(method[8:], p)
        if method == "approve":
            name = self._need(p.get("name"))
            before = r.store.approved_spec(name)
            ok = r.approve(name, str(p.get("hash", "")))
            if not ok:
                raise ipc.RpcError("SPEC-HASH", "The service changed after you looked at it. Review it again.")
            self._approved_event(name, before)
            return True
        if method == "start":
            r.set_desired(self._need(p.get("name")), Desired.RUNNING)
            return True
        if method == "stop":
            r.set_desired(self._need(p.get("name")), Desired.STOPPED)
            return True
        if method == "restart":
            await r.restart(self._need(p.get("name")))
            return True
        if method == "login":  # App settings: start the background service at login (`on` None only asks)
            from pythonator.service import osservice

            return await asyncio.to_thread(osservice.at_login, p["on"] if isinstance(p.get("on"), bool) else None)
        if method == "rename":
            try:
                await r.rename(self._need(p.get("name")), str(p.get("new", "")).strip().lower())
            except ValidationError:
                raise
            except ValueError as e:
                raise ipc.RpcError("SVC-409", str(e).splitlines()[-1][:200]) from e
            return True
        if method == "remove":
            await r.remove(self._need(p.get("name")))
            return True
        if method == "plan":
            name = self._need(p.get("name"))
            res = r.plan(name)
            res["guard"] = await self.plugins.policy_review({"name": name, **projection(ServiceSpec.model_validate(
                res["spec"], strict=False)), "widens": res["widens"]})
            return res
        if method == "approve_all":
            name = self._need(p.get("name"))
            prop = r.store.proposal(name)
            if prop is not None:
                await self._guard(prop, r.specs[name])
            before = r.store.approved_spec(name)
            res = r.approve_all(name, str(p.get("hash", "")))
            if not res["approved"]:
                raise ipc.RpcError("SPEC-HASH", "The service changed after you looked at it. Review it again.")
            self._approved_event(name, before)
            return res
        if method == "apply_safe":
            try:
                return r.apply_safe(self._need(p.get("name")))
            except Refused as e:
                raise ipc.RpcError(e.code, e.message, e.fix) from None
        if method == "try.run":
            spec = (ServiceSpec.model_validate(p["spec"], strict=False) if isinstance(p.get("spec"), dict)
                    else r.specs[self._need(p.get("name"))])
            try:
                return await r.tries.run(spec, timeout=min(600.0, float(p.get("timeout", 60))))
            except Refused as e:
                raise ipc.RpcError(e.code, e.message, e.fix) from None
        if method in ("try.keep", "try.discard"):
            cid = str(p.get("id", ""))
            if cid not in r.tries.copies:
                raise ipc.RpcError("TRY-404", "That copy is gone (it is removed when the background service restarts).")
            if method == "try.discard":
                return r.tries.discard(cid)
            c = r.tries.copies[cid]
            if c.name in r.specs:
                await r.snaps.snapshot(c.name, c.source, "before Keep changes (Try in a copy)")
            res = r.tries.keep(cid)
            if c.name in r.specs:
                res["snapshot"] = await r.snaps.snapshot(c.name, c.source, "Keep changes (Try in a copy)")
            return res
        if method.startswith("git."):
            name = self._need(p.get("name"))
            folder = r.specs[name].folder
            if method == "git.log":
                return {"log": await r.snaps.log(name, folder), "changed": await r.snaps.changed(name, folder)}
            if method == "git.snapshot":
                return await r.snaps.snapshot(name, folder, str(p.get("message") or "snapshot")[:200])
            if method == "git.restore":
                try:
                    return await r.snaps.restore(name, folder, str(p.get("id", "")))
                except ValueError as e:
                    raise ipc.RpcError("GIT-001", str(e)) from None
        if method.startswith("files.") or method.startswith("lint."):
            return await self._files(method, p)
        if method == "templates.list":
            return await self.plugins.templates()
        if method == "templates.create":
            try:
                return await self._create_project(p)
            except Refused as e:
                raise ipc.RpcError(e.code, e.message, e.fix) from None
        if method == "assist.list":
            return self.plugins.assistants()
        if method == "assist.ask":
            try:
                return await self._assist(p)
            except Refused as e:
                raise ipc.RpcError(e.code, e.message, e.fix) from None
        if method.startswith("security."):
            return await self._security(method, p)
        if method.startswith("engines."):
            return await self._engines(method, p)
        if method == "runtime.list":
            from pythonator.policy.runtimes import RUNTIMES

            found = dict(self.host.runtimes)
            found.setdefault("python", self.host.python)
            if self.host.node:
                found.setdefault("node", self.host.node)
            return {"runtimes": [{"key": k, "label": rt.label, "path": found.get(k, ""), "install": rt.install,
                                  "image": rt.image, "example": rt.example} for k, rt in RUNTIMES.items() if k != "binary"],
                    "python": await r.installs.listing()}
        if method == "runtime.install":
            v = str(p.get("version", ""))
            async with self._job(f"python:{v}", f"Installing Python {v}"):
                ok, msg = await r.installs.install(v)
            if not ok:
                raise ipc.RpcError("RUN-RT-005", msg)
            return msg
        if method == "drift.fix":
            action = str(p.get("action", ""))
            if action not in ("heal", "start", "restart", "adopt", "ignore"):
                raise ipc.RpcError("RPC-400", f"Unknown fix {action!r}.")
            await r.fix(self._need(p.get("name")), str(p.get("kind", "")), action)
            return True
        if method == "plan_os":
            from pythonator.policy.compile import compile_service
            from pythonator.policy.reference import for_os

            spec = ServiceSpec.model_validate(p.get("spec"), strict=False)
            target = str(p.get("os", ""))
            if target not in ("linux", "darwin", "windows", "podman"):
                raise ipc.RpcError("RPC-400", "os must be linux, darwin, windows or podman")
            if target == self.host.os and not (target == "linux" and spec.mode.value == "container"):
                return r.preview(spec)
            try:
                tspec, ref = for_os(spec, target, self.host.home)
                plan, refused = compile_service(tspec, ref).plan, None
            except Refused as e:
                plan, refused = None, e
            except ValueError as e:
                raise ipc.RpcError("RPC-400", str(e)) from None
            from pythonator.reconcile.reconciler import _plan_dict

            return {"reference": True, **_plan_dict(plan, refused)}
        if method == "preview":
            spec = ServiceSpec.model_validate(p.get("spec"), strict=False)
            res = r.preview(spec)
            res["guard"] = await self.plugins.policy_review({"name": spec.name, **projection(spec), "widens": res["widens"]})
            res["exposure"] = [0.0, exposure(spec)]
            return res
        if method == "fit":
            from pythonator.policy import fit
            from pythonator.policy.paths import check_folder

            folder = str(p.get("folder", ""))
            verdict = check_folder(folder, self.host.home)
            if not verdict.ok:
                raise ipc.RpcError(verdict.code, verdict.reason, "Pick the project's own folder.")
            return (await asyncio.to_thread(fit.scan, folder)).as_dict()
        if method == "stdin":
            text = p.get("text")
            if not isinstance(text, str):
                raise ipc.RpcError("RPC-400", "text must be a string")
            if not await r.send_stdin(self._need(p.get("name")), text):
                raise ipc.RpcError("SVC-STDIN", "It isn't running, so there is nothing to send the line to.")
            return True
        if method == "traffic":
            return r.gate.recent(self._need(p.get("name")))
        if method == "allow_host":
            return await self._allow_host(self._need(p.get("name")), str(p.get("host", "")))
        if method == "logs":
            lg = r.log(self._need(p.get("name")))
            start, count = int(p.get("start", -1)), int(p.get("count", 200))
            lines = lg.tail(count) if start < 0 else lg.read(start, count)
            return {"first": lg.first, "total": lg.total, "lines": [[x.n, x.ts, x.stream, x.text] for x in lines]}
        if method == "search_logs":
            lg = r.log(self._need(p.get("name")))
            return [[x.n, x.ts, x.stream, x.text] for x in lg.search(str(p.get("text", ""))[:200])]
        if method == "check":
            mode = str(p.get("mode", "sandbox"))
            if mode not in ("sandbox", "container", "wsl", "full"):
                raise ipc.RpcError("RPC-400", "mode must be sandbox, container, wsl or full")
            t0 = time.perf_counter()
            res = {
                "host": self._host(), "probes": check.probes(self.host), "speed": self.bench.summary(),
                "hints": self.bench.hints(self.host) + await self._hints(), "matrix": check.matrix(self.host, mode),
            }
            res["probe_ms"] = round((time.perf_counter() - t0) * 1000)
            return res
        if method == "measure":
            ms = await check.measure_sandbox(self.host)
            values = {"daemon_mb": check.daemon_mb()}
            if ms is not None:
                values["sandbox_ms"] = ms
            cms = await self._measure_container()
            if cms is not None:
                values["container_ms"] = cms
            self.bench.add(**values)
            return self.bench.summary()
        if method == "engines.pull":
            return await self._pull(self._need(p.get("name")))
        if method in ("wsl.setup", "wsl.remove"):
            return await self._wsl(method == "wsl.setup")
        if method == "report_ui":  # the window reports its own first paint and worst stall
            vals = {k: float(v) for k, v in p.items() if k in ("first_paint_ms", "ui_stall_ms") and isinstance(v, (int, float))}
            if vals:
                self.bench.add(**vals)
            return True
        if method == "shutdown":
            if p.get("uninstall"):  # the uninstaller: no folder keeps a permission for a sandbox that's going away
                await self.rec.revoke_all()
            self._stop.set()
            return True
        raise ipc.RpcError("RPC-404", f"Unknown method {method!r}.")

    async def _guard(self, spec: ServiceSpec, old: ServiceSpec | None) -> None:
        """Every reviewer plugin (Guard) on a change; a refusal goes on the security timeline and back to you."""
        try:
            await self.plugins.policy_check({"name": spec.name, **projection(spec), "widens": widenings(old, spec),
                                             "exposure": exposure(spec)})
        except Refused as e:
            reviewer, _, why = e.message.partition(": ")
            why = (why or e.message).removeprefix(f"{spec.name}: ").replace(self.host.home + "/", "~/")
            self.security.add("refused", spec.name, f"{reviewer or 'Guard'} refused: {why}",
                              e.fix or e.code, {"kind": "rule", "code": e.code})
            raise ipc.RpcError(e.code, e.message, e.fix) from None

    def _approved_event(self, name: str, before: ServiceSpec | None) -> None:
        spec = self.rec.store.approved_spec(name)
        if spec is None:
            return
        wid = widenings(before, spec)
        if wid:
            self.security.add("permission", name, "You approved: " + wid[0] + (f" (+{len(wid) - 1} more)" if len(wid) > 1 else ""),
                              f"exposure {exposure(spec)}", {"kind": "open", "service": name})

    async def _allow_host(self, name: str, host: str) -> dict[str, Any]:
        """Allow on a blocked attempt: add that one host to the list. Your click is the approval for exactly that
        line; anything else that would widen access still waits. The running gate takes it at once, no restart."""
        r = self.rec
        old = r.specs[name]
        if old.network.traffic.mode != "hosts":
            raise ipc.RpcError("NET-ALLOW-001", "Allow works with Traffic: allowed hosts.", "Change Traffic in the service's Network settings.")
        net = old.network.model_dump()
        net["traffic"]["hosts"] = [*old.network.traffic.hosts, host.lower().rstrip(".")]
        new = old.evolve(network=net)
        if new.network.traffic.hosts == old.network.traffic.hosts:
            return {"approved": True, "hash": risk_hash(new)}
        approved_before = r.store.is_approved(old)
        extra = [w for w in widenings(old, new) if host.lower() not in w]
        await self._guard(new, old)
        res = r.apply(new)
        if approved_before and not extra:
            r.approve(name, res["hash"])
            res["approved"] = True
        await r.update_rule(name, Rule(new.network.traffic.hosts, new.network.traffic.allow_lan))
        return res

    # plugin host API (what plugins may ask for, after their grants are checked) ----------------------------------
    def emit(self, topic: str, data: dict[str, Any]) -> None:
        self._emit(topic, data)

    def services_list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": n, "mode": s.mode.value,
                "phase": self.rec.observed[n].phase.value if n in self.rec.observed else "stopped",
                "ports": [[x.port, x.reach.value] for x in s.network.ports],
            }
            for n, s in sorted(self.rec.specs.items())
        ]

    def logs(self, name: str, start: int, count: int) -> dict[str, Any]:
        if name not in self.rec.specs:
            return {"lines": []}
        lg = self.rec.log(name)
        lines = lg.tail(count) if start < 0 else lg.read(start, count)
        return {"total": lg.total, "lines": [[x.n, x.ts, x.stream, x.text] for x in lines]}

    def mask_all(self, text: str) -> str:
        """Every service's secrets, masked: for anything that leaves through a plugin."""
        vals: list[str] = []
        for spec in self.rec.specs.values():
            vals += self.rec._masker(spec, everything=True)._values
        return Masker(vals).mask(text)

    async def _files(self, method: str, p: dict[str, Any]) -> Any:
        from pythonator.work import files, lint

        name = self._need(p.get("name"))
        folder = self.rec.specs[name].folder
        try:
            if method == "files.list":
                git = await self.rec.snaps.changed(name, folder)
                return {"folder": folder, "tree": files.tree(folder), "changed": git}
            if method == "files.names":
                return await asyncio.to_thread(files.names, folder)
            if method == "files.read":
                return files.read(folder, str(p.get("path", "")))
            if method == "files.write":
                text = p.get("text")
                if not isinstance(text, str):
                    raise ipc.RpcError("RPC-400", "text must be a string")
                return files.write(folder, str(p.get("path", "")), text, str(p.get("hash", "")))
            if method == "files.edit":
                res = files.apply_edit(folder, str(p.get("path", "")), str(p.get("old", "")), str(p.get("new", "")))
                self.rec.snapshot_later(name, folder, f"assistant edit to {res['path']}")
                return res
            text = str(p.get("text", ""))
            path = str(p.get("path", "x.py"))
            if not path.endswith((".py", ".pyi")):
                return [] if method == "lint.check" else text
            if method == "lint.check":
                return await lint.check(self.host, folder, path, text, errors_only=p.get("level") == "errors")
            if method == "lint.fix":
                return {"text": await lint.fix(self.host, folder, path, text, str(p.get("code", "")))}
        except files.FileRefused as e:
            raise ipc.RpcError(e.code, e.message, e.fix) from None
        except Refused as e:
            raise ipc.RpcError(e.code, e.message, e.fix) from None
        raise ipc.RpcError("RPC-404", f"Unknown method {method!r}.")

    async def _create_project(self, p: dict[str, Any]) -> dict[str, Any]:
        """A starter project: the plugin returns file contents; we write them into a new, empty folder you picked."""
        import re

        from pythonator.policy.paths import check_folder
        from pythonator.safe import safefs

        folder = str(p.get("folder", ""))
        name = str(p.get("name", "")) or folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name):
            raise Refused("TPL-001", "Name: lowercase letters, digits and '-'.")
        target = Path(folder)
        if target.exists() and (not target.is_dir() or target.is_symlink() or any(target.iterdir())):
            raise Refused("TPL-002", f"{folder} isn't empty.", "Pick a new folder; templates never write over files.")
        verdict = check_folder(folder, self.host.home, data_dir=self.host.data_dir, program_dirs=self.host.program)
        if not verdict.ok:
            raise Refused(verdict.code, verdict.reason, "Pick a folder inside your home.")
        res = await self.plugins.render_template(str(p.get("plugin", "")), str(p.get("template", "")), name)
        files = res["files"]
        total = sum(len(str(v).encode()) for v in files.values())
        if len(files) > 200 or total > 2 * 1024 * 1024:
            raise Refused("TPL-003", "The template is too big (200 files, 2 MB at most).")
        for rel in files:
            if not isinstance(rel, str) or rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/") \
                    or ":" in rel:
                raise Refused("TPL-004", f"The template has a bad path: {rel!r}.")
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
        for rel, text in sorted(files.items()):
            safefs.write_bytes(str(target), rel, str(text).encode("utf-8"), make_parents=True)
        return {"folder": str(target), "name": name, "files": sorted(files), "runtime": res.get("runtime", "python"),
                "command": res.get("command") or [], "port": res.get("port") or 0}

    async def _assist(self, p: dict[str, Any]) -> dict[str, Any]:
        """Build exactly what the assistant may see, ask it, and keep only edits that can apply as exact text."""
        from pythonator.work import files

        name = self._need(p.get("name"))
        spec = self.rec.specs[name]
        helpers = self.plugins.assistants()
        pid = str(p.get("plugin") or (helpers[0]["id"] if helpers else ""))
        if not pid:
            raise Refused("PLG-ASSIST-004", "No assistant plugin is on.", "Turn on the AI plugin in Plugins.")
        plug = self.plugins._get(pid)
        path = str(p.get("path", ""))
        question = str(p.get("question", ""))[:2000].strip()
        if not question:
            raise Refused("PLG-ASSIST-005", "Ask a question first.")
        masker = self.rec._masker(spec, everything=True)
        payload: dict[str, Any] = {"service": name, "runtime": spec.runtime, "command": list(spec.command),
                                   "question": masker.mask(question)}
        sent: list[str] = []
        if path and not files.is_env(path):
            text = p.get("text") if isinstance(p.get("text"), str) else files.read(spec.folder, path)["text"]
            payload["file"] = {"path": path, "text": masker.mask(str(text))[:200_000]}
            payload["problems"] = [x for x in (p.get("problems") or []) if isinstance(x, dict)][:50]
            sent.append(f"{path} (read-only)")
        if self.plugins.log_grant(plug, name):
            lines = self.rec.log(name).tail(40)
            payload["logs"] = [masker.mask(x.text) for x in lines]
            sent.insert(0, f"{len(lines)} masked log lines")
        res = await self.plugins.assist(pid, payload)
        edits = []
        sent_text = payload.get("file", {}).get("text", "")
        for e in (res.get("edits") or [])[:5]:
            if not isinstance(e, dict) or e.get("path") != path:
                continue
            old, new = str(e.get("old", "")), str(e.get("new", ""))
            if old and sent_text.count(old) == 1 and "••••" not in old + new:
                edits.append({"path": path, "old": old, "new": new, "line": sent_text[: sent_text.index(old)].count("\n") + 1})
        hosts = ", ".join(plug.grants.get("net.connect") or []) or "nowhere (no network granted)"
        return {
            "plugin": plug.manifest.name, "text": masker.mask(str(res.get("text", "")))[:8000], "edits": edits,
            "sent": f"Sent: {', '.join(sent) or 'your question only'}. To: {hosts} only.",
        }

    async def _plugins(self, verb: str, p: dict[str, Any]) -> Any:
        h = self.plugins
        try:
            if verb == "list":
                return h.snapshot()
            if verb == "install":
                return h.install(str(p.get("path", "")))
            if verb == "inspect":
                return h.inspect(str(p.get("path", "")))
            pid = str(p.get("id", ""))
            if verb == "grant":
                h.grant(pid, str(p.get("permission", "")), p.get("scope", True))
            elif verb == "revoke":
                h.revoke(pid, str(p.get("permission", "")))
            elif verb == "enable":
                await h.set_enabled(pid, bool(p.get("on", True)))
            elif verb == "remove":
                h.remove(pid)
            elif verb == "settings":
                await h.set_settings(pid, p.get("settings"))
            elif verb == "reviewed":
                h.reviewed(pid)
            elif verb == "secret":
                h.set_secret(pid, str(p.get("key", "")), str(p.get("value", "")))
            elif verb == "safe_mode":
                await h.set_safe_mode(bool(p.get("on", True)))
            elif verb == "views":
                return h.views.get(str(p.get("service", "")), {})
            else:
                raise ipc.RpcError("RPC-404", f"Unknown method plugins.{verb}.")
        except Refused as e:
            raise ipc.RpcError(e.code, e.message, e.fix) from None
        except BadPlugin as e:
            raise ipc.RpcError("PLG-BAD", str(e)) from None
        return h.snapshot()

    async def _daily_measure(self) -> None:
        """Measure once a day on this machine, so System check always has fresh, real numbers."""
        last = max((r.get("ts", 0) for r in self.bench.rows if "sandbox_ms" in r), default=0)
        if time.time() - last > 86400:
            ms = await check.measure_sandbox(self.host)
            self.bench.add(daemon_mb=check.daemon_mb(), **({"sandbox_ms": ms} if ms is not None else {}))

    def _host(self) -> dict[str, Any]:
        if self.rec.host is not self.host:  # the reconciler learned something (WSL's distribution went missing)
            self.host = self.plugins.host = self.rec.host
        h = self.host
        found = [n for n, path in (("podman", h.podman), ("docker", h.docker)) if path]
        if any(not self.engines.get(n) for n in found) and time.monotonic() - self._engines_probed > 30:
            asyncio.ensure_future(self._probe_engines())  # a slow first `--version` at logon: ask again, never block
        return {
            "os": h.os,
            "describe": describe(),
            "home": h.home,
            "bwrap": bool(h.bwrap),
            "pasta": bool(h.pasta),
            "seccomp": h.seccomp,
            "systemd_user": h.systemd_user,
            "landlock_abi": h.landlock_abi,
            "appcontainer": h.appcontainer,
            "seatbelt": bool(h.sandbox_exec),
            "wsl": bool(h.wsl),
            "wsl_ready": h.wsl_ready,
            "engines": self.engines,
            "engines_found": found,  # installed, even before the version answered: one truth for Engines and New service
            "stale": [k for k, v in self.engines.items() if v and not check.version_ok(k, v)],
        }

    def _container_specs(self) -> list[tuple[ServiceSpec, str, str]]:
        """(spec, engine, engine path) for every Container mode service whose engine is here."""
        from pythonator.policy.compile import pick_engine

        out = []
        for spec in self.rec.specs.values():
            if spec.mode is not Mode.CONTAINER or not spec.container:
                continue
            try:
                eng = pick_engine(spec, self.host)
            except Refused:
                continue
            out.append((spec, eng, getattr(self.host, eng)))
        return out

    async def _hints(self) -> list[dict[str, str]]:
        """Hints from your own services: images not downloaded yet, Full access that Sandbox would cover."""
        from pythonator.work.engines import image_present

        hints: list[dict[str, str]] = []
        for spec, eng, exe in self._container_specs()[:8]:
            img = spec.container.image if spec.container else ""
            if img and not await image_present(eng, exe, img):
                hints.append({"level": "info", "ico": "box", "title": f"{spec.name}'s image isn't downloaded yet",
                              "detail": f"({img.split('@')[0]}), so its next start downloads it first.",
                              "action": f"pull:{spec.name}", "label": "Download now"})
        for name, spec in self.rec.specs.items():
            if spec.mode.value == "full":
                hints.append({"level": "warn", "ico": "unlock", "title": f"{name} runs with Full access",
                              "detail": f"Exposure {exposure(spec)} of 10. Sandbox would keep it to its own folder "
                                        "and the hosts you list; the plan sheet shows exactly what changes.",
                              "action": f"open:{name}", "label": "Review in Sandbox"})
        return hints

    async def _measure_container(self) -> float | None:
        """A real container start (`run --rm --network none <image> true`) with an image you already have."""
        from pythonator.work.engines import _run, image_present

        for spec, eng, exe in self._container_specs():
            img = spec.container.image if spec.container else ""
            if img and await image_present(eng, exe, img):
                t0 = time.perf_counter()
                code, _ = await _run(exe, "run", "--rm", "--network", "none", "--entrypoint", "true", img, timeout=60)
                if code == 0:
                    return round((time.perf_counter() - t0) * 1000, 1)
        return None

    async def _pull(self, name: str) -> dict[str, Any]:
        from pythonator.work.engines import _run3, image_present

        row = next((x for x in self._container_specs() if x[0].name == name), None)
        if row is None:
            raise ipc.RpcError("ENG-002", f"{name} isn't a Container mode service with an engine here.")
        spec, eng, exe = row
        img = spec.container.image if spec.container else ""
        if await image_present(eng, exe, img):
            return {"image": img, "pulled": False}
        led = self.rec.ledger
        entry = None if led.owns("image", img) else led.intend("image", name, img, engine=eng)  # before it happens
        async with self._job(f"pull:{img}", f"Pulling {img}"):
            code, _out, err = await _run3(exe, "pull", img, timeout=900)
        if code != 0:
            if entry is not None:
                led.undone(entry)
            why = err.strip().splitlines()[-1][:200] if err.strip() else str(code)
            raise ipc.RpcError("ENG-003", f"{eng} pull {img} failed: {why}")
        if entry is not None:
            led.done(entry)
        return {"image": img, "pulled": True}

    async def _wsl(self, setup: bool) -> str:
        """Pythonator's own Debian for WSL mode: installed and locked down (no drives, no interop), or removed."""
        import dataclasses

        from pythonator.jail import wslsetup
        from pythonator.safe.process import write_log

        if self.host.os != "windows" or not self.host.wsl:
            raise ipc.RpcError("RUN-WSL-001", "WSL mode needs WSL on Windows 10 or 11.", "wsl --install --no-distribution")
        async with self._job("wsl", "WSL setup" if setup else "Removing WSL") as say:
            def tell(line: str) -> None:
                write_log(self.root / "daemon.log", f"wsl: {line}")
                say(line)

            if setup:
                ok, msg = await asyncio.to_thread(wslsetup.setup, self.host.wsl, self.root, tell)
            else:
                ok, msg = await asyncio.to_thread(wslsetup.remove, self.host.wsl, self.root)
        ready = ok and setup
        self.host = dataclasses.replace(self.host, wsl_ready=ready)
        self.rec.host = self.plugins.host = self.host
        self.security.add("permission", "WSL", msg if ok else f"WSL {'setup' if setup else 'removal'} failed", msg)
        if not ok:
            raise ipc.RpcError("RUN-WSL-002", msg)
        return msg

    async def _security(self, method: str, p: dict[str, Any]) -> Any:
        r = self.rec
        guard = self.plugins.plugins.get("guard")
        if method == "security.list":
            days = int(p.get("days", 1)) if str(p.get("days", 1)).isdigit() else 1
            days = days if days in (1, 7, 30) else 1
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            since = midnight if days == 1 else time.time() - days * 86400
            rows = self.security.since(since)
            counts = {k: sum(1 for x in rows if x["kind"] == k) for k in ("blocked", "refused", "permission", "healed")}
            return {
                "since": since,
                "days": days,
                "rows": rows[-500:],
                "counts": counts,
                "exposure": sorted(({"name": n, "score": exposure(sp), "mode": sp.mode.value} for n, sp in r.specs.items()),
                                   key=lambda x: x["name"]),
                "guard": {
                    "installed": guard is not None, "enabled": bool(guard and guard.enabled),
                    "state": guard.state if guard else "", "granted": bool(guard and "policy.check" in guard.grants),
                    "settings": guard.settings if guard else {}, "home": self.host.home,
                },
            }
        if method == "security.allow":
            rid = int(p.get("id", 0))
            row = next((x for x in self.security.rows if x["id"] == rid), None)
            act = (row or {}).get("action") or {}
            if not row or act.get("kind") != "allow" or act.get("done"):
                raise ipc.RpcError("SEC-001", "That block can't be allowed from here (or it already was).")
            if act["service"] not in r.specs:
                raise ipc.RpcError("SEC-002", f"{act['service']} doesn't exist any more.")
            res = await self._allow_host(act["service"], act["host"])
            self.security.resolve(rid, "allowed")
            return res
        if method == "security.rule":
            if guard is None:
                raise ipc.RpcError("SEC-003", "Guard isn't installed.", "Plugins → Guard → Install.")
            key = str(p.get("key", ""))
            if key not in ("full_access_rule", "port_rule", "exposure_rule", "block_lan"):
                raise ipc.RpcError("RPC-400", f"Unknown rule {key!r}.")
            try:
                await self.plugins.set_settings("guard", {**guard.settings, key: bool(p.get("on"))})
            except Refused as e:
                raise ipc.RpcError(e.code, e.message, e.fix) from None
            self.security.add("permission", "Guard", f"Rule {'on' if p.get('on') else 'off'}: {key.replace('_', ' ')}",
                              "changed in Security → Guard rules")
            return True
        raise ipc.RpcError("RPC-404", f"Unknown method {method}.")

    async def _engines(self, method: str, p: dict[str, Any]) -> Any:
        from pythonator.work import engines as eng

        r = self.rec
        paths = {"podman": self.host.podman, "docker": self.host.docker}
        if method == "engines.list":
            if p.get("recheck"):
                self.engines.clear()
                await self._probe_engines()
            wsl = {"os": self.host.os, "exe": bool(self.host.wsl), "ready": self.host.wsl_ready,
                   "distro": "Pythonator (Debian)", "interop": False, "drives": False,
                   "command": "wsl --list --verbose" if self.host.os == "windows" else "# shown on Windows 10 and 11"}
            pinned = {n: s.container.image for n, s in r.specs.items() if s.container}
            res = await eng.snapshot(self.engines, paths, r.ledger, wsl, pinned)
            for card in res["engines"]:
                if card.get("installed") and card.get("version"):
                    self.engines[card["engine"]] = str(card["version"])
            res["stale"] = [k for k, v in self.engines.items() if v and not check.version_ok(k, v)]
            return res
        name = str(p.get("engine", ""))
        if name not in paths or not paths[name]:
            raise ipc.RpcError("ENG-001", f"{name or 'That engine'} isn't installed.")
        from pythonator.policy.compile import pick_engine

        def on_engine(spec: ServiceSpec) -> bool:
            if spec.mode is not Mode.CONTAINER:
                return False
            try:
                return pick_engine(spec, self.host) == name
            except Refused:
                return False

        if method == "engines.restart":
            names = [n for n, s in r.specs.items() if on_engine(s) and n in r.runs]
            for n in names:
                await r.restart(n)
            return {"restarted": names, "commands": [f"restarted {n}" for n in names]
                    or [f"# no Pythonator service is running on {name}"]}
        if method == "engines.prune":
            images = {s.container.image for s in r.specs.values() if s.container}
            return await eng.prune(name, paths[name], r.ledger, set(r.runs), images)
        raise ipc.RpcError("RPC-404", f"Unknown method {method}.")

    async def _probe_engines(self) -> None:
        """Engine versions, at start and again while one hasn't answered (each `--version` is a short read-only call),
        and whether Pythonator's WSL distribution still exists (its ready marker can outlive an unregister)."""
        self._engines_probed = time.monotonic()
        if self.host.wsl_ready and self.host.wsl:
            import dataclasses

            from pythonator.jail import wslsetup

            if not await asyncio.to_thread(wslsetup.installed, self.host.wsl):
                (self.root / "wsl" / "ready").unlink(missing_ok=True)
                self.host = dataclasses.replace(self.host, wsl_ready=False)
                self.rec.host = self.plugins.host = self.host
            else:
                await asyncio.to_thread(wslsetup.refresh, self.host.wsl, self.root)
        for name, path in (("podman", self.host.podman), ("docker", self.host.docker)):
            if path:
                self.engines[name] = await check.engine_version(path)

    # lifecycle --------------------------------------------------------------------------------------------------
    async def serve(self) -> None:
        tcp = ipc.use_tcp()
        if tcp:
            marker = ipc.port_path(self.root)
            if marker.exists():
                with contextlib.suppress(FileNotFoundError):
                    if await _alive_tcp(ipc.read_port(self.root)):
                        raise SystemExit("pythonatord is already running")
                marker.unlink(missing_ok=True)
            server = await asyncio.start_server(self.handle, host="127.0.0.1", port=0, limit=ipc.MAX_LINE)
            write_atomic(marker, str(server.sockets[0].getsockname()[1]).encode())
        else:
            marker = ipc.socket_path(self.root)
            if marker.exists():
                if await _alive(marker):
                    raise SystemExit("pythonatord is already running")
                marker.unlink()
            # The folder is owner-only (0700), which is what keeps others out; no umask change, which is process-wide
            # and raced with other threads creating files.
            server = await asyncio.start_unix_server(self.handle, path=str(marker), limit=ipc.MAX_LINE)
            os.chmod(marker, 0o600)
        await self.rec.open()
        await self._probe_engines()
        self.plugins.load()
        await self.plugins.start_all()
        asyncio.get_running_loop().call_later(5, lambda: asyncio.ensure_future(self._daily_measure()))
        loop = asyncio.get_running_loop()
        from pythonator.safe.process import write_log

        loop.set_exception_handler(lambda _l, ctx: write_log(self.root / "daemon.log", f"task: {ctx.get('message')} "
                                                             f"{ctx.get('exception')!r}"))
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows; RuntimeError: not the main thread
                loop.add_signal_handler(sig, self._stop.set)
        async with server:
            await self._stop.wait()
        await self.plugins.close()
        await self.rec.close()
        marker.unlink(missing_ok=True)

async def _alive(path: Path) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1)
        w.close()
        return True
    except (TimeoutError, OSError):
        return False

async def _alive_tcp(port: int) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
        w.close()
        return True
    except (TimeoutError, OSError):
        return False

def main() -> None:
    from pythonator.safe import process

    os.umask(0o077)
    root = data_dir()
    process.harden(root)
    process.raise_open_files()
    process.install_hook(root / "daemon.log")
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a closed terminal never stops your services
    if process.running_as_root():
        process.write_log(root / "daemon.log", "warning: running as root; Sandbox services would run as root too")
    asyncio.run(Daemon(root).serve())

if __name__ == "__main__":
    main()
