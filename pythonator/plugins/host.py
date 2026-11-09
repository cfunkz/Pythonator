"""The plugin host, inside pythonatord.
Each plugin is its own process in its own jail (compile_plugin). It can compute, and it can ask the host for
things over stdin/stdout. Every request names a method; each method needs one permission; the host checks the
grant (and its scope) on every call and counts calls and refusals. Grants are stored against the plugin's content
hash: when the code changes, the grants are gone.
Policy hooks fail closed: if a plugin you trusted with policy.check is enabled but crashed or too slow, changes are
refused until you fix or disable it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from pythonator.logs.store import LogStore
from pythonator.plugins.model import CATALOGUE, BadPlugin, Manifest, content_hash, load_manifest
from pythonator.policy.compile import compile_plugin
from pythonator.policy.host import HostFacts
from pythonator.policy.plan import Refused
from pythonator.reconcile import proc
from pythonator.safe import safefs
from pythonator.safe.hostenv import host_env
from pythonator.store.files import ensure_private_dir, read_json, write_json

RES = Path(__file__).resolve().parent.parent / "resources"
SDK = RES / "sdk"
BUNDLED = RES / "plugins"
MAX_ERR_LINES = 50  # stderr lines a second kept in the plugin log; the rest are counted, not stored
MAX_ERR_BYTES = 2 << 20  # stderr bytes a second before the plugin is stopped
MAX_RATE = 200  # messages per second
MAX_BACKLOG = 2 * 1024 * 1024  # bytes queued to a plugin that isn't reading
HOOK_TIMEOUT_S = 2.0
METHOD_PERMISSION = {
    "services.list": "services.read", "services.logs": "logs.read", "ui.notify": "ui.notify", "ui.view": "ui.views",
    "net.request": "net.connect", "services.probe": "net.service", "ui.status": "ui.statusbar",
}
ASSIST_TIMEOUT_S = 180.0

@dataclass
class Calls:
    calls: int = 0
    refused: int = 0
    last: float = 0.0

@dataclass
class Plugin:
    manifest: Manifest
    folder: Path
    hash: str
    official: bool
    grants: dict[str, Any] = field(default_factory=dict)  # permission -> True or [service names]
    enabled: bool = False
    settings: dict[str, Any] = field(default_factory=dict)
    code_changed: bool = False
    state: str = "stopped"  # stopped, starting, running, crashed, refused
    start_ms: float = 0.0  # spawn to hello, last start
    message: str = ""
    proc: asyncio.subprocess.Process | None = None
    hooks: set[str] = field(default_factory=set)
    events: set[str] = field(default_factory=set)
    calls: dict[str, Calls] = field(default_factory=dict)
    crashes: int = 0
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    window: list[float] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)  # net.connect requests: when, host, bytes, status
    new_perms: tuple[str, ...] = ()  # asked for by this code and not by the version you last trusted
    status: str = ""  # its status bar item (ui.statusbar)

class PluginHost:
    def __init__(self, root: Path, host: HostFacts, api: Any) -> None:
        self.root = root
        self.dir = ensure_private_dir(root / "plugins")
        self.state_path = root / "plugins.json"
        self.host = host
        self.api = api  # the daemon: services_list(), logs(name, start, count), emit(topic, data)
        self.plugins: dict[str, Plugin] = {}
        self.safe_mode = os.environ.get("PYTHONATOR_SAFE_MODE") == "1"
        self.views: dict[str, dict[str, list[dict[str, str]]]] = {}  # service -> plugin -> rows
        self._hook_seq = 0
        self.official = self._official_hashes()

    # state on disk ----------------------------------------------------------------------------------------------
    def _official_hashes(self) -> dict[str, str]:
        """A bundled plugin is official only while its files hash to the value in official.json, which the release
        pins (a CI test keeps the two in step). An edited copy in the install folder is just another plugin."""
        out: dict[str, str] = {}
        pinned = read_json(BUNDLED / "official.json", {})
        pinned = pinned if isinstance(pinned, dict) else {}
        if BUNDLED.is_dir():
            for d in sorted(BUNDLED.iterdir()):
                if (d / "plugin.toml").is_file():
                    with contextlib.suppress(BadPlugin, OSError, safefs.UnsafePath):
                        pid, h = load_manifest(d).id, content_hash(d)
                        if pinned.get(pid) == h:
                            out[pid] = h
        return out

    def load(self) -> None:
        saved = read_json(self.state_path, {})
        saved = saved if isinstance(saved, dict) else {}
        self.plugins.clear()
        for d in sorted(self.dir.iterdir()):
            if not d.is_dir() or d.is_symlink():
                continue
            try:
                m = load_manifest(d)
                h = content_hash(d)
            except (BadPlugin, OSError, safefs.UnsafePath):
                continue
            st = saved.get(m.id, {})
            p = Plugin(m, d, h, official=self.official.get(m.id) == h, settings=st.get("settings", {}))
            if st.get("hash") == h:
                p.grants, p.enabled = dict(st.get("grants", {})), bool(st.get("enabled"))
                p.code_changed = bool(st.get("code_changed"))
                p.new_perms = tuple(st.get("new_perms", ()))
            elif st:
                p.code_changed = True  # new code: grants reset, off until you review it
                p.new_perms = tuple(x for x in m.permissions if x not in st.get("perms", m.permissions))
                self._reset_event(p)
            self.plugins[m.id] = p
        self._save()

    def _save(self) -> None:
        write_json(
            self.state_path,
            {
                pid: {"hash": p.hash, "grants": p.grants, "enabled": p.enabled, "settings": p.settings,
                      "perms": list(p.manifest.permissions), "code_changed": p.code_changed,
                      "new_perms": list(p.new_perms)}
                for pid, p in self.plugins.items()
            },
        )

    # install ----------------------------------------------------------------------------------------------------
    def available(self) -> list[dict[str, Any]]:
        out = []
        if BUNDLED.is_dir():
            for d in sorted(BUNDLED.iterdir()):
                with contextlib.suppress(BadPlugin, OSError, safefs.UnsafePath):
                    m = load_manifest(d)
                    if m.id not in self.plugins:
                        out.append(
                            {
                                "id": m.id, "name": m.name, "version": m.version, "description": m.description,
                                "official": True, "path": str(d),
                            }
                        )
        return out

    def inspect(self, src: str) -> dict[str, Any]:
        """What installing a folder would mean, before anything is copied: who, what, how big, what it asks for,
        and the jail those permissions compile into."""
        source = Path(src)
        m = load_manifest(source)
        h = content_hash(source)
        n_files, lines = 0, 0

        def walk(rel: str) -> None:
            nonlocal n_files, lines
            for e in safefs.list_dir(str(source), rel):
                path = f"{rel}/{e.name}" if rel else e.name
                if e.kind == "dir" and e.name != "__pycache__":
                    walk(path)
                elif e.kind == "file":
                    n_files += 1
                    if e.name.endswith(".py"):
                        lines += safefs.read_bytes(str(source), path).count(b"\n")

        walk("")
        old = self.plugins.get(m.id)
        try:
            plan = compile_plugin(m.id, str(source), str(SDK), m.entry, self.host).plan
            jail = [{"title": x.title, "status": str(x.status), "by": x.by, "detail": x.detail} for x in plan.layers]
        except Refused as r:
            jail = [{"title": "Refused", "status": "not-enforced", "by": r.code, "detail": r.message}]
        return {
            "id": m.id, "name": m.name, "version": m.version, "publisher": m.publisher, "description": m.description,
            "hash": h, "official": self.official.get(m.id) == h, "files": n_files, "lines": lines, "path": str(source),
            "hosts": list(m.hosts), "secrets": list(m.secrets), "activation": list(m.activation), "jail": jail,
            "replaces": old.manifest.version if old else "",
            "permissions": [self._perm_row(k, None, old is not None and k not in old.manifest.permissions)
                            for k in m.permissions],
        }

    @staticmethod
    def _perm_row(k: str, granted: Any, new: bool) -> dict[str, Any]:
        c = CATALOGUE[k]
        return {"key": k, "title": c.title, "detail": c.detail, "scoped": c.scoped, "available": c.available,
                "why_not": c.why_not, "scope_kind": c.scope_kind, "granted": granted, "new": new}

    def install(self, src: str) -> dict[str, Any]:
        """Copy a plugin folder in (through the safe reader: no links, no special files). It starts disabled with
        no permissions; you grant them one by one."""
        source = Path(src)
        m = load_manifest(source)
        h = content_hash(source)
        dest = self.dir / m.id
        tmp = self.dir / f".{m.id}.installing"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(mode=0o700)

        def copy(rel: str) -> None:
            for e in safefs.list_dir(str(source), rel):
                path = f"{rel}/{e.name}" if rel else e.name
                if e.kind == "dir" and e.name != "__pycache__":
                    (tmp / path).mkdir(mode=0o700)
                    copy(path)
                elif e.kind == "file":
                    (tmp / path).write_bytes(safefs.read_bytes(str(source), path))

        copy("")
        if content_hash(tmp) != h:
            shutil.rmtree(tmp, ignore_errors=True)
            raise BadPlugin("the folder changed while it was being copied")
        old = self.plugins.get(m.id)
        if old:
            self._stop_now(old)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        settings = _default_settings(dest, self.host.home)
        p = Plugin(m, dest, h, official=self.official.get(m.id) == h, settings=old.settings if old else settings)
        if old and old.hash != h:
            p.code_changed = True
            p.new_perms = tuple(x for x in m.permissions if x not in old.manifest.permissions)
            self._reset_event(p)
        elif not old:
            self._sec("permission", m.name, f"Installed {m.name} {m.version}", f"content hash {h[:4]}…{h[-4:]}",
                      {"kind": "review", "plugin": m.id})
        self.plugins[m.id] = p
        self._save()
        return self.describe(p)

    def remove(self, pid: str) -> None:
        p = self._get(pid)
        self._stop_now(p)
        if hasattr(self.api, "rec"):
            asyncio.get_event_loop().create_task(self.api.rec._revoke_grants(f"plugin:{pid}"))
        shutil.rmtree(p.folder, ignore_errors=True)
        self.plugins.pop(pid, None)
        for rows in self.views.values():
            rows.pop(pid, None)
        self._save()

    # grants -----------------------------------------------------------------------------------------------------
    def grant(self, pid: str, perm: str, scope: Any = True) -> None:
        p = self._get(pid)
        cat = CATALOGUE.get(perm)
        if cat is None or perm not in p.manifest.permissions:
            raise Refused("PLG-GRANT-001", f"{p.manifest.name} didn't ask for {perm}.")
        if not cat.available:
            raise Refused("PLG-GRANT-002", f"{cat.title} isn't available yet: {cat.why_not}.")
        if cat.scope_kind == "hosts":
            hosts = list(p.manifest.hosts) if scope is True else scope
            if not isinstance(hosts, list) or not hosts or not all(isinstance(x, str) for x in hosts):
                raise Refused("PLG-GRANT-003", "Pick at least one host.")
            extra = [h for h in hosts if h not in p.manifest.hosts]
            if extra:
                raise Refused("PLG-GRANT-004", f"{p.manifest.name} didn't declare {extra[0]}; it can only be granted "
                              f"{', '.join(p.manifest.hosts)}.")
            scope = sorted(set(hosts))
        elif cat.scoped and scope is not True:
            if not isinstance(scope, list) or not all(isinstance(x, str) for x in scope):
                raise Refused("PLG-GRANT-003", "Scope must be a list of service names.")
        p.grants[perm] = scope if cat.scoped else True
        p.new_perms = tuple(x for x in p.new_perms if x != perm)
        p.code_changed = bool(p.new_perms)
        self._save()
        self._sec("permission", p.manifest.name, f"Granted {cat.title}",
                  perm + ("" if scope is True or not cat.scoped else " · " + ", ".join(scope)),
                  {"kind": "review", "plugin": pid})

    def _sec(self, kind: str, who: str, title: str, detail: str = "", action: dict[str, Any] | None = None) -> None:
        log = getattr(self.api, "security", None)
        if log is not None:
            log.add(kind, who, title, detail, action)

    def _reset_event(self, p: Plugin) -> None:
        self._sec("permission", p.manifest.name, f"Grants reset: code changed ({p.manifest.version})",
                  f"content hash {p.hash[:4]}…{p.hash[-4:]}", {"kind": "review", "plugin": p.manifest.id})

    def _refused_event(self, p: Plugin, perm: str, why: str) -> None:
        why = "not granted" + why.split("granted", 1)[1] if "isn't granted" in why or why.startswith("not granted") else why
        self._sec("refused", p.manifest.name, f"Refused {perm}: {why}", "the plugin asked; Pythonator said no",
                  {"kind": "review", "plugin": p.manifest.id})

    def reviewed(self, pid: str) -> None:
        """Keep it off: you looked at the new code's permissions and granted none (or no more)."""
        p = self._get(pid)
        p.code_changed, p.new_perms = False, ()
        self._save()

    def set_secret(self, pid: str, key: str, value: str) -> None:
        """A plugin's key (an API key, say) goes to the OS keychain. The plugin never reads it: net.request fills
        {secret:NAME} in a header on the way out. An empty value deletes it."""
        p = self._get(pid)
        if key not in p.manifest.secrets:
            raise Refused("PLG-SECRET-001", f"{p.manifest.name} didn't declare a secret called {key}.")
        from pythonator.reconcile import secrets

        try:
            if value:
                secrets.put(f"plugin:{p.manifest.id}", key, value)
            else:
                secrets.delete(f"plugin:{p.manifest.id}", key)
        except Exception as e:  # noqa: BLE001 - keyring raises its own types per backend
            raise Refused("PLG-SECRET-002", f"The OS keychain refused it: {e}",
                          "On Linux, install and unlock a Secret Service keychain (GNOME Keyring or KWallet).") from None

    def _secret(self, p: Plugin, key: str) -> str | None:
        from pythonator.reconcile import secrets

        return secrets.get(f"plugin:{p.manifest.id}", key)

    def revoke(self, pid: str, perm: str) -> None:
        p = self._get(pid)
        if p.grants.pop(perm, None) is not None:  # always safe: the plugin keeps working with less
            cat = CATALOGUE.get(perm)
            self._sec("permission", p.manifest.name, f"Took back {cat.title if cat else perm}", perm,
                      {"kind": "review", "plugin": pid})
        self._save()

    async def set_enabled(self, pid: str, on: bool) -> None:
        p = self._get(pid)
        p.enabled = on
        p.code_changed = bool(p.new_perms)
        self._save()
        if on and not self.safe_mode:
            await self.start(p)
        elif not on:
            self._stop_now(p)

    async def set_settings(self, pid: str, settings: Any) -> None:
        """Settings live beside the code, not in it: changing them keeps the grants and restarts the plugin."""
        p = self._get(pid)
        if not isinstance(settings, dict) or len(json.dumps(settings)) > 65536:
            raise Refused("PLG-SET-001", "Settings must be a JSON object under 64 KB.")
        p.settings = settings
        self._save()
        if p.state in ("running", "starting"):
            self._stop_now(p)
            await self.start(p)

    async def set_safe_mode(self, on: bool) -> None:
        self.safe_mode = on
        if on:
            for p in self.plugins.values():
                self._stop_now(p)
        else:
            await self.start_all()

    # processes --------------------------------------------------------------------------------------------------
    async def start_all(self) -> None:
        if self.safe_mode:
            return
        for p in self.plugins.values():
            if p.enabled and p.state not in ("running", "starting"):
                await self.start(p)

    async def start(self, p: Plugin) -> None:
        try:
            jail = compile_plugin(p.manifest.id, str(p.folder), str(SDK), p.manifest.entry, self.host)
        except Refused as r:
            p.state, p.message = "refused", f"{r.code}: {r.message}"
            self._changed(p)
            return
        p.state, p.message, p.hooks, p.events = "starting", "", set(), set()
        for d in jail.make_dirs:
            ensure_private_dir(Path(d))
        if jail.grants:
            self.api.rec._record_grants(f"plugin:{p.manifest.id}", jail)
        log = self._log(p)
        log.append("s", f"start · {p.manifest.name} {p.manifest.version} · hash {p.hash[:12]} · {jail.plan.first_line()}")
        t0 = time.perf_counter()
        p.proc = await asyncio.create_subprocess_exec(
            *jail.argv, env={**host_env(), **jail.env} if sys.platform == "win32" else jail.env, cwd=jail.host_cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=1 << 20, **_detach(),
        )
        asyncio.create_task(self._stderr(p, p.proc))
        asyncio.create_task(self._reader(p, p.proc))
        asyncio.create_task(self._watch(p, p.proc))
        try:
            hello = await self._request(p, "hello", {"settings": p.settings, "grants": sorted(p.grants)})
        except (TimeoutError, ConnectionError, RuntimeError) as e:
            p.message = f"didn't answer hello: {e}"
            self._stop_now(p)
            return
        p.hooks = set(hello.get("hooks", [])) if isinstance(hello, dict) else set()
        p.events = set(hello.get("events", [])) if isinstance(hello, dict) else set()
        p.state = "running"
        p.start_ms = round((time.perf_counter() - t0) * 1000, 1)
        bench = getattr(self.api, "bench", None)
        if bench is not None:
            bench.add(plugin_ms=p.start_ms)
        self._changed(p)

    async def _stderr(self, p: Plugin, pr: asyncio.subprocess.Process) -> None:
        assert pr.stderr is not None  # noqa: S101 - created with PIPE above
        log = self._log(p)
        second, lines, size, dropped = 0, 0, 0, 0
        while True:
            try:
                raw = await pr.stderr.readline()
            except ValueError:  # one line over 1 MB
                log.append("s", "stopped: a stderr line over 1 MB")
                proc.signal_tree(pr.pid, proc.KILL)
                return
            if not raw:
                return
            now = int(time.monotonic())
            if now != second:
                if dropped:
                    log.append("s", f"{dropped} stderr lines dropped (over {MAX_ERR_LINES} a second)")
                second, lines, size, dropped = now, 0, 0, 0
            lines, size = lines + 1, size + len(raw)
            if size > MAX_ERR_BYTES:
                log.append("s", f"stopped: more than {MAX_ERR_BYTES // (1 << 20)} MB of stderr in a second")
                proc.signal_tree(pr.pid, proc.KILL)
                return
            if lines > MAX_ERR_LINES:
                dropped += 1
                continue
            log.append("e", raw.decode("utf-8", "replace").rstrip("\n")[:4000])

    async def _watch(self, p: Plugin, pr: asyncio.subprocess.Process) -> None:
        cache: dict[int, psutil.Process] = {}
        while pr.returncode is None:
            _cpu, mem, _n = proc.stats(pr.pid, cache)
            if mem > 256 * 1024 * 1024:
                self._log(p).append("s", "stopped: memory over 256 MB (watchdog)")
                proc.signal_tree(pr.pid, proc.KILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(pr.wait(), 1.0)
        code = pr.returncode
        if p.proc is pr:
            p.proc = None
            for f in p.pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("plugin exited"))
            p.pending.clear()
            if p.state != "stopped":
                p.crashes += 1
                p.state, p.message = "crashed", f"exited with code {code}"
                self._log(p).append("s", f"stop · exited with code {code}")
                self._changed(p)
                if p.enabled and p.crashes <= 3 and not self.safe_mode:
                    await asyncio.sleep(2**p.crashes)
                    if p.enabled and p.proc is None:
                        await self.start(p)

    def _stop_now(self, p: Plugin) -> None:
        p.state = "stopped"
        if p.proc and p.proc.returncode is None:
            proc.signal_tree(p.proc.pid, proc.KILL)
        p.proc = None
        self._changed(p)

    async def close(self) -> None:
        for p in self.plugins.values():
            self._stop_now(p)

    # protocol ---------------------------------------------------------------------------------------------------
    def _write(self, p: Plugin, obj: dict[str, Any]) -> None:
        """Never blocks: asyncio buffers the write. A plugin that stops reading and lets 2 MB pile up is stopped and
        turned off, so it can't grow Pythonator's memory or hold up events for the others."""
        if p.proc and p.proc.stdin and not p.proc.stdin.is_closing():
            if p.proc.stdin.transport.get_write_buffer_size() > MAX_BACKLOG:
                self._log(p).append("s", "stopped: it stopped reading Pythonator's messages (2 MB waiting)")
                p.enabled = False
                self._save()
                self._stop_now(p)
                return
            p.proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    async def _request(self, p: Plugin, method: str, params: dict[str, Any], timeout: float = 5.0) -> Any:
        self._hook_seq += 1
        rid = f"h{self._hook_seq}"
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        p.pending[rid] = fut
        self._write(p, {"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            p.pending.pop(rid, None)

    async def _reader(self, p: Plugin, pr: asyncio.subprocess.Process) -> None:
        assert pr.stdout is not None  # noqa: S101 - created with PIPE above
        while True:
            try:
                raw = await pr.stdout.readline()
            except ValueError:
                self._log(p).append("s", "stopped: a message over 1 MB")
                proc.signal_tree(pr.pid, proc.KILL)
                return
            if not raw:
                return
            now = time.monotonic()
            p.window = [t for t in p.window if now - t < 1.0] + [now]
            if len(p.window) > MAX_RATE:
                self._log(p).append("s", f"stopped: more than {MAX_RATE} messages a second")
                proc.signal_tree(pr.pid, proc.KILL)
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            rid = msg.get("id")
            if isinstance(rid, str) and rid in p.pending and "method" not in msg:
                fut = p.pending[rid]
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str((msg.get("error") or {}).get("message", "error"))))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            if "method" in msg:
                params = msg.get("params") or {}
                if msg["method"] == "net.request" and isinstance(params, dict):
                    asyncio.create_task(self._serve_net(p, rid, params))
                elif msg["method"] == "services.probe" and isinstance(params, dict):
                    asyncio.create_task(self._serve_probe(p, rid, params))
                else:
                    self._write(p, {"id": rid, **self._serve(p, str(msg["method"]), params)})

    async def _serve_net(self, p: Plugin, rid: Any, params: dict[str, Any]) -> None:
        from pythonator.plugins import netproxy

        c = p.calls.setdefault("net.connect", Calls())
        c.calls += 1
        c.last = time.time()
        granted = p.grants.get("net.connect")
        url = str(params.get("url", ""))[:4096]
        row: dict[str, Any] = {"ts": time.time(), "url": url[:200], "status": 0, "sent": 0, "ms": 0}
        try:
            if not isinstance(granted, list):
                raise netproxy.NetRefused("PLG-PERM", "net.connect isn't granted")
            headers = params.get("headers") or {}
            res = await asyncio.to_thread(
                netproxy.request, url, method=str(params.get("method", "POST")).upper(),
                headers=headers if isinstance(headers, dict) else {}, body=str(params.get("body") or ""),
                timeout=float(params.get("timeout", 60)), granted=tuple(granted), declared=p.manifest.secrets,
                lookup=lambda k: self._secret(p, k), mask=self.api.mask_all,
            )
            row.update(status=res["status"], sent=res["sent"], ms=res["ms"], host=res["host"])
            out: dict[str, Any] = {"result": res}
        except netproxy.NetRefused as e:
            c.refused += 1
            self._refused_event(p, "net.connect", str(e)[:120])
            row.update(error=f"{e.code}: {e}")
            out = {"error": {"code": e.code, "message": str(e)}}
        p.sent = [*p.sent[-49:], row]
        self._log(p).append("s", f"net · {row.get('host') or url[:80]} · {row.get('status') or row.get('error')} · "
                                 f"{row['sent']} bytes out")
        self._write(p, {"id": rid, **out})

    async def _serve_probe(self, p: Plugin, rid: Any, params: dict[str, Any]) -> None:
        """net.service: TCP or HTTP GET to one of a service's own open ports, made by Pythonator."""
        from pythonator.reconcile.reconciler import probe

        c = p.calls.setdefault("net.service", Calls())
        c.calls += 1
        c.last = time.time()
        grant = p.grants.get("net.service")
        name = params.get("service") if isinstance(params.get("service"), str) else ""
        spec = self.api.rec.specs.get(name) if hasattr(self.api, "rec") else None
        try:
            port = int(params.get("port") or (spec.network.ports[0].port if spec and spec.network.ports else 0))
        except (TypeError, ValueError):
            port = 0
        if grant is None or (isinstance(grant, list) and name not in grant):
            c.refused += 1
            self._refused_event(p, "net.service", f"not granted for {name}")
            out: dict[str, Any] = {"error": {"code": "PLG-PERM", "message": f"net.service isn't granted for {name}"}}
        elif spec is None or port not in [x.port for x in spec.network.ports]:
            c.refused += 1
            out = {"error": {"code": "PLG-ARG", "message": f"{port} isn't one of {name}'s open ports"}}
        else:
            kind = "http" if params.get("kind") == "http" else "tcp"
            path = str(params.get("path") or "/")[:200]
            if not path.startswith("/") or any(ch in path for ch in "\r\n "):
                path = "/"
            t0 = time.monotonic()
            ok, detail = await probe(kind, port, path, 5.0)
            out = {"result": {"ok": ok, "detail": detail, "ms": round((time.monotonic() - t0) * 1000, 1)}}
        self._write(p, {"id": rid, **out})

    async def assist(self, pid: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask a plugin trusted with editor.assist. The caller (the daemon) decides what goes in the payload: the
        open file and problems, plus masked logs only when the plugin may read that service's logs."""
        p = self._get(pid)
        if p.grants.get("editor.assist") is not True:
            raise Refused("PLG-ASSIST-001", f"{p.manifest.name} may not answer in the editor.",
                          "Grant it Answer in the editor on the Plugins page.")
        if p.state != "running" or "editor.assist" not in p.hooks:
            raise Refused("PLG-ASSIST-002", f"{p.manifest.name} isn't running ({p.state}).", "Turn it on in Plugins.")
        c = p.calls.setdefault("editor.assist", Calls())
        c.calls += 1
        c.last = time.time()
        try:
            res = await self._request(p, "editor.assist", payload, timeout=ASSIST_TIMEOUT_S)
        except (TimeoutError, ConnectionError, RuntimeError) as e:
            raise Refused("PLG-ASSIST-003", f"{p.manifest.name} didn't answer: {e or 'timeout'}.") from None
        return res if isinstance(res, dict) else {"text": str(res)[:4000]}

    async def templates(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.plugins.values():
            if p.state != "running" or p.grants.get("projects.create") is not True or "projects.templates" not in p.hooks:
                continue
            try:
                res = await self._request(p, "projects.templates", {}, timeout=5.0)
            except (TimeoutError, ConnectionError, RuntimeError):
                continue
            for t in res if isinstance(res, list) else []:
                if isinstance(t, dict) and isinstance(t.get("id"), str):
                    out.append({**{k: t.get(k) for k in ("id", "title", "detail", "runtime", "command", "port")},
                                "plugin": p.manifest.id, "by": p.manifest.name})
        return out

    async def render_template(self, pid: str, template: str, name: str) -> dict[str, Any]:
        p = self._get(pid)
        if p.grants.get("projects.create") is not True or p.state != "running":
            raise Refused("PLG-TPL-001", f"{p.manifest.name} may not offer starter projects, or isn't running.")
        c = p.calls.setdefault("projects.create", Calls())
        c.calls += 1
        c.last = time.time()
        try:
            res = await self._request(p, "projects.render", {"template": template, "name": name}, timeout=10.0)
        except (TimeoutError, ConnectionError, RuntimeError) as e:
            raise Refused("PLG-TPL-002", f"{p.manifest.name} didn't answer: {e or 'timeout'}.") from None
        if not isinstance(res, dict) or not isinstance(res.get("files"), dict):
            raise Refused("PLG-TPL-003", f"{p.manifest.name} sent no files.")
        return res

    def log_grant(self, p: Plugin, service: str) -> bool:
        g = p.grants.get("logs.read")
        return g is True or (isinstance(g, list) and service in g)

    def assistants(self) -> list[dict[str, Any]]:
        return [
            {"id": p.manifest.id, "name": p.manifest.name, "official": p.official, "state": p.state,
             "hosts": p.grants.get("net.connect") or []}
            for p in self.plugins.values()
            if p.enabled and p.grants.get("editor.assist") is True
        ]

    def _serve(self, p: Plugin, method: str, params: dict[str, Any]) -> dict[str, Any]:
        perm = METHOD_PERMISSION.get(method)
        if perm is None:
            return {"error": {"code": "PLG-METHOD", "message": f"unknown method {method}"}}
        c = p.calls.setdefault(perm, Calls())
        c.calls += 1
        c.last = time.time()
        grant = p.grants.get(perm)
        target = params.get("service") if isinstance(params.get("service"), str) else None
        if grant is None or (isinstance(grant, list) and target not in grant):
            c.refused += 1
            self._refused_event(p, perm, "not granted" + (f" for {target}" if target else ""))
            return {"error": {"code": "PLG-PERM", "message": f"{perm} isn't granted" + (f" for {target}" if target else "")}}
        if method == "services.list":
            return {"result": self.api.services_list()}
        if method == "services.logs":
            if target is None:
                return {"error": {"code": "PLG-ARG", "message": "service is required"}}
            return {"result": self.api.logs(target, int(params.get("start", -1)), min(int(params.get("count", 100)), 500))}
        if method == "ui.notify":
            self.api.emit(
                "notify",
                {
                    "plugin": p.manifest.name, "text": str(params.get("text", ""))[:500],
                    "level": str(params.get("level", "info"))[:10],
                },
            )
            return {"result": True}
        if method == "ui.status":
            p.status = str(params.get("text", ""))[:60]
            self.api.emit("plugin", {"id": p.manifest.id, "state": p.state, "message": p.message, "status": p.status})
            return {"result": True}
        if method == "ui.view":
            svc = str(params.get("service", ""))
            rows = [
                {"label": str(r.get("label", ""))[:40], "value": str(r.get("value", ""))[:120]}
                for r in (params.get("rows") or [])[:12]
                if isinstance(r, dict)
            ]
            self.views.setdefault(svc, {})[p.manifest.name] = rows
            self.api.emit("view", {"service": svc})
            return {"result": True}
        return {"error": {"code": "PLG-METHOD", "message": method}}

    # events and hooks -------------------------------------------------------------------------------------------
    def on_event(self, topic: str, data: dict[str, Any]) -> None:
        if topic != "state":
            return
        for p in self.plugins.values():
            if p.state == "running" and "service.state" in p.events and "services.read" in p.grants:
                self._write(p, {"method": "event", "params": {"topic": "service.state", "data": data}})

    async def policy_check(self, change: dict[str, Any]) -> None:
        """Ask every enabled plugin trusted with policy.check. Any refusal, crash or timeout refuses the change."""
        if self.safe_mode:
            return
        for p in self.plugins.values():
            if not p.enabled or "policy.check" not in p.grants:
                continue
            name = p.manifest.name
            if p.state != "running" or "policy.check" not in p.hooks:
                raise Refused(
                    "GUARD-001", f"{name} reviews every change but isn't running ({p.state}).",
                    f"Fix {name}, or turn off its Review changes permission.",
                )
            c = p.calls.setdefault("policy.check", Calls())
            c.calls += 1
            c.last = time.time()
            try:
                res = await self._request(p, "policy.check", change, timeout=HOOK_TIMEOUT_S)
            except (TimeoutError, ConnectionError, RuntimeError) as e:
                raise Refused(
                    "GUARD-002",
                    f"{name} didn't answer in time ({e or 'timeout'}).",
                    f"Fix {name}, or turn off its Review changes permission.",
                ) from None
            if not isinstance(res, dict) or not res.get("allow"):
                reason = str((res or {}).get("reason", "no reason given"))[:300] if isinstance(res, dict) else "bad answer"
                raise Refused(
                    "GUARD-003", f"{name}: {reason}", str((res or {}).get("fix", ""))[:200] if isinstance(res, dict) else ""
                )

    async def policy_review(self, change: dict[str, Any]) -> list[dict[str, Any]]:
        """The same review as policy_check, for the plan sheet: every reviewer's verdict, never raises."""
        out: list[dict[str, Any]] = []
        if self.safe_mode:
            return out
        for p in self.plugins.values():
            if not p.enabled or "policy.check" not in p.grants:
                continue
            row: dict[str, Any] = {"plugin": p.manifest.name, "id": p.manifest.id, "allow": False, "reason": "", "summary": ""}
            if p.state != "running" or "policy.check" not in p.hooks:
                row["reason"] = f"isn't running ({p.state}), so every change is refused"
            else:
                try:
                    res = await self._request(p, "policy.check", change, timeout=HOOK_TIMEOUT_S)
                except (TimeoutError, ConnectionError, RuntimeError) as e:
                    res = {"allow": False, "reason": f"didn't answer ({e or 'timeout'})"}
                if isinstance(res, dict):
                    row.update(allow=bool(res.get("allow")), reason=str(res.get("reason", ""))[:300],
                               fix=str(res.get("fix", ""))[:200], summary=str(res.get("summary", ""))[:200])
            out.append(row)
        return out

    # views for the window and CLI -------------------------------------------------------------------------------
    def _get(self, pid: str) -> Plugin:
        p = self.plugins.get(pid)
        if p is None:
            raise Refused("PLG-404", f"No plugin called {pid!r}.")
        return p

    def _log(self, p: Plugin) -> LogStore:
        if not hasattr(self, "_logs"):
            self._logs: dict[str, LogStore] = {}
        lg = self._logs.get(p.manifest.id)
        if lg is None:
            lg = self._logs[p.manifest.id] = LogStore(ensure_private_dir(self.root / "logs" / "plugins"), p.manifest.id)
        return lg

    def _changed(self, p: Plugin) -> None:
        self.api.emit("plugin", {"id": p.manifest.id, "state": p.state, "message": p.message})

    def describe(self, p: Plugin) -> dict[str, Any]:
        m = p.manifest
        try:
            plan = compile_plugin(m.id, str(p.folder), str(SDK), m.entry, self.host).plan
            jail = [{"title": x.title, "status": str(x.status), "by": x.by, "detail": x.detail} for x in plan.layers]
        except Refused as r:
            jail = [{"title": "Refused", "status": "not-enforced", "by": r.code, "detail": r.message}]
        return {
            "id": m.id, "name": m.name, "version": m.version, "description": m.description, "publisher": m.publisher,
            "official": p.official, "hash": p.hash, "enabled": p.enabled, "state": p.state, "message": p.message,
            "code_changed": p.code_changed, "settings": p.settings, "jail": jail,
            "permissions": [self._perm_row(k, p.grants.get(k), k in p.new_perms) for k in m.permissions],
            "calls": {k: {"calls": c.calls, "refused": c.refused, "last": c.last} for k, c in p.calls.items()},
            "hosts": list(m.hosts), "secrets": [{"key": k, "set": bool(self._secret(p, k))} for k in m.secrets],
            "sent": p.sent[-20:], "new_perms": list(p.new_perms), "status": p.status, "activation": list(m.activation),
            "update": bool(m.id in self.official and self.official[m.id] != p.hash), "memory_mb": self._memory(p),
            "rate": len([x for x in p.window if time.monotonic() - x < 1.0]), "crashes": p.crashes,
            "log": [[x.n, x.ts, x.stream, x.text] for x in self._log(p).tail(60)],
        }

    def _memory(self, p: Plugin) -> float:
        if p.proc is None or p.proc.returncode is not None:
            return 0.0
        try:
            return round(proc.stats(p.proc.pid, {})[1] / 1048576, 1)
        except Exception:  # noqa: BLE001 - the process may be gone between the check and the read
            return 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "safe_mode": self.safe_mode, "installed": [self.describe(p) for p in self.plugins.values()],
            "available": self.available(),
        }

def _default_settings(folder: Path, home: str) -> dict[str, Any]:
    try:
        import tomllib

        data = tomllib.loads(safefs.read_text(str(folder), "plugin.toml", limit=64 * 1024))
    except Exception:  # noqa: BLE001 - settings are optional
        return {}
    raw = json.dumps(data.get("settings", {}))
    return json.loads(raw.replace("{home}", home.replace("\\", "/")))

def _detach() -> dict[str, Any]:
    if sys.platform == "win32":
        import subprocess

        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}
