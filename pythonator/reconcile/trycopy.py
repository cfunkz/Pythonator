"""Try in a copy: run a service's command once in a throwaway copy of its folder and report what it did.
The copy leaves out .git, virtual environments and caches, and stops at 200 MB. The run is jailed in Sandbox mode
with traffic off: every address the program tries goes to the egress gate with an empty allow list, so each attempt
is refused and listed. Afterwards you see each file it created, changed or deleted, and either keep those changes
(written back through safefs, which refuses links) or throw the copy away.
Copies live next to Pythonator's data folder (`<data>-try/<id>`), never inside it, because a service may not be given
Pythonator's own data folder."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets as _secrets
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pythonator.model.service import Desired, Mode, Network, Restart, ServiceSpec, Traffic
from pythonator.net.gate import Gate, Rule
from pythonator.policy.compile import compile_service
from pythonator.policy.host import HostFacts
from pythonator.policy.plan import Refused
from pythonator.reconcile import proc
from pythonator.safe import safefs
from pythonator.safe.hostenv import host_env
from pythonator.safe.process import NO_WINDOW
from pythonator.store.files import ensure_private_dir

MAX_BYTES = 200 * 1024 * 1024
MAX_FILES = 20_000
SKIP = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env.d", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".cache", "target", ".gradle", ".next", ".turbo", "bin", "obj",
})
DEFAULT_TIMEOUT_S = 60.0

class TooBig(Exception):
    pass

@dataclass
class Copy:
    id: str
    name: str
    source: str
    path: Path
    before: dict[str, str]
    started: float = field(default_factory=time.time)
    report: dict[str, Any] | None = None

def _walk(root: str, rel: str = "", depth: int = 0) -> list[tuple[str, int]]:
    """Every regular file under root (relative paths, sizes), links and special files left out."""
    out: list[tuple[str, int]] = []
    if depth > 32:
        return out
    for e in safefs.list_dir(root, rel):
        r = f"{rel}/{e.name}" if rel else e.name
        if e.kind == "dir" and e.name not in SKIP:
            out += _walk(root, r, depth + 1)
        elif e.kind == "file":
            out.append((r, e.size))
        if len(out) > MAX_FILES:
            raise TooBig(f"more than {MAX_FILES} files")
    return out

def _digest(root: Path) -> dict[str, str]:
    out = {}
    for rel, _size in _walk(str(root)):
        with contextlib.suppress(OSError, safefs.UnsafePath):
            out[rel] = hashlib.sha256(safefs.read_bytes(str(root), rel, MAX_BYTES)).hexdigest()
    return out

def _lines(a: bytes, b: bytes) -> int | None:
    """How many lines differ, for text files; None for binary."""
    if b"\x00" in a[:8192] or b"\x00" in b[:8192]:
        return None
    import difflib

    x, y = a.decode("utf-8", "replace").splitlines(), b.decode("utf-8", "replace").splitlines()
    return sum(1 for d in difflib.ndiff(x, y) if d[:1] in "+-" and d[:2] != "? ") if len(x) + len(y) < 20000 else abs(len(x) - len(y))

class TryCopies:
    def __init__(self, data_dir: Path, host: HostFacts, gate: Gate) -> None:
        self.base = Path(str(data_dir).rstrip("/\\") + "-try")
        self.host = host
        self.gate = gate
        self.copies: dict[str, Copy] = {}

    def cleanup(self) -> None:
        """Copies are throwaway: anything left from an earlier run of the background service is removed at start."""
        if self.base.is_dir():
            for p in self.base.iterdir():  # ours: the data folder
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)

    def make(self, spec: ServiceSpec) -> Copy:
        files = _walk(spec.folder)
        total = sum(s for _r, s in files)
        if total > MAX_BYTES:
            raise TooBig(f"the folder is {total // 1048576} MB without .git, venvs and caches; the cap is 200 MB")
        ensure_private_dir(self.base)
        cid = _secrets.token_hex(4)
        dst = self.base / cid / spec.folder.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        dst.mkdir(parents=True)
        for rel, _size in files:
            try:
                data = safefs.read_bytes(spec.folder, rel, MAX_BYTES)
            except (OSError, safefs.UnsafePath):
                continue
            p = dst.joinpath(*rel.split("/"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        c = Copy(cid, spec.name, spec.folder, dst, _digest(dst))
        self.copies[cid] = c
        return c

    def spec_for(self, spec: ServiceSpec, c: Copy) -> ServiceSpec:
        """The same command in Sandbox mode with traffic off (every attempt refused and listed), no ports, no extra
        folders, no secrets, no schedule, no restarts."""
        name = f"try-{c.id}"
        return spec.model_copy(update={
            "name": name, "folder": str(c.path), "mode": Mode.SANDBOX, "desired": Desired.RUNNING,
            "network": Network(traffic=Traffic(mode="hosts", hosts=())), "restart": Restart(policy="never"),
            "schedule": None, "secrets": (), "folders": (), "container": None,
        })

    async def run(self, spec: ServiceSpec, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        try:
            c = self.make(spec)
        except TooBig as e:
            raise Refused("TRY-001", f"Can't copy {spec.name}: {e}.", "Move big data out of the folder, or add it to .gitignore.") from None
        tspec = self.spec_for(spec, c)
        jail = compile_service(tspec, self.host)  # Refused propagates: the window shows the code and fix
        for d in jail.make_dirs:
            ensure_private_dir(Path(d))
        if jail.gate:
            g = jail.gate
            await self.gate.open(tspec.name, Rule((), False), unix=g.unix, tcp=g.tcp)
        env = {**host_env(), **jail.env} if sys.platform == "win32" else dict(jail.env)
        t0 = time.monotonic()
        peak, out = 0, bytearray()
        code: int | None = None
        timed_out = False
        try:
            p = await asyncio.create_subprocess_exec(
                *jail.argv, cwd=jail.host_cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **NO_WINDOW,
            )

            async def read() -> None:
                assert p.stdout is not None  # noqa: S101 - PIPE was asked for
                while chunk := await p.stdout.read(65536):
                    out.extend(chunk)
                    del out[:-65536]

            reader = asyncio.create_task(read())
            cache: dict[int, Any] = {}
            with contextlib.suppress(Exception):
                peak = proc.stats(p.pid, cache)[1]
            while True:
                try:
                    code = await asyncio.wait_for(p.wait(), 0.25)
                    break
                except TimeoutError:
                    with contextlib.suppress(Exception):
                        peak = max(peak, proc.stats(p.pid, cache)[1])
                    if time.monotonic() - t0 > timeout:
                        timed_out = True
                        proc.signal_tree(p.pid, proc.KILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader, 5)
            if jail.marker:
                proc.kill_marked(jail.marker)
        finally:
            reached = self.gate.recent(tspec.name)
            await self.gate.close(tspec.name)
        after = _digest(c.path)
        files: list[dict[str, Any]] = []
        for rel in sorted(set(c.before) | set(after)):
            if rel not in c.before:
                st = os.lstat(c.path / rel)  # the copy is the service's: never follow its links
                size = st.st_size if stat.S_ISREG(st.st_mode) else 0
                files.append({"path": rel, "change": "added", "detail": f"+{_size(size)}"})
            elif rel not in after:
                files.append({"path": rel, "change": "deleted", "detail": "deleted"})
            elif c.before[rel] != after[rel]:
                old = safefs.read_bytes(spec.folder, rel, MAX_BYTES) if _exists(spec.folder, rel) else b""
                try:
                    n = _lines(old, safefs.read_bytes(str(c.path), rel, MAX_BYTES))
                except (OSError, safefs.UnsafePath):
                    n = None
                files.append({"path": rel, "change": "changed", "detail": f"{n} lines" if n is not None else "binary"})
        hosts: dict[tuple[str, int], dict[str, Any]] = {}
        for ev in reached:
            k = (ev["host"], ev["port"])
            hosts.setdefault(k, {"host": ev["host"], "port": ev["port"], "verdict": ev["verdict"], "why": ev.get("why", ""),
                                 "count": 0})["count"] += 1
        c.report = {
            "id": c.id, "name": spec.name, "exit": code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - t0, 1), "peak_mb": round(peak / 1048576), "files": files,
            "reached": list(hosts.values()), "output": out.decode("utf-8", "replace")[-8000:],
            "plan": jail.plan.first_line(),
        }
        return c.report

    def keep(self, cid: str) -> dict[str, Any]:
        """Write the copy's changes back into the real folder, then drop the copy. Links in the real folder are refused."""
        c = self.copies.get(cid)
        if c is None or c.report is None:
            raise KeyError(cid)
        done, failed = 0, []
        for f in c.report["files"]:
            rel = f["path"]
            try:
                if f["change"] == "deleted":
                    safefs.unlink(c.source, rel)
                else:
                    safefs.write_bytes(c.source, rel, (c.path / rel).read_bytes(), make_parents=True)
                done += 1
            except (OSError, safefs.UnsafePath) as e:
                failed.append(f"{rel}: {e}")
        self.discard(cid)
        return {"applied": done, "failed": failed}

    def discard(self, cid: str) -> bool:
        c = self.copies.pop(cid, None)
        if c is None:
            return False
        shutil.rmtree(c.path.parent, ignore_errors=True)
        return True

def _exists(root: str, rel: str) -> bool:
    try:
        safefs.read_bytes(root, rel, 1 << 30)
        return True
    except (OSError, safefs.UnsafePath):
        return False

def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n // 1024} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"
