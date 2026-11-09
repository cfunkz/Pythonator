"""Private snapshots of a service folder, with git, kept outside the folder.
Every approved change and every "Keep changes" from Try in a copy takes a snapshot. The git directory lives in
Pythonator's data folder (`<data>/git/<service>.git`), so the service can't read or rewrite its own history, and the
folder never gets a .git it didn't have. Restore writes files back through safefs, which refuses links."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pythonator.safe import safefs
from pythonator.safe.hostenv import host_env, resolve
from pythonator.safe.process import NO_WINDOW

MAX_FILE = 20 * 1024 * 1024
EXCLUDE = (".git/", ".venv/", "venv/", "node_modules/", "__pycache__/", "target/", ".mypy_cache/", ".pytest_cache/",
           ".ruff_cache/", "*.pyc", "*.log", ".env")

class Snapshots:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "git"

    def git_dir(self, name: str) -> Path:
        return self.root / f"{name}.git"

    def _env(self, name: str, folder: str) -> dict[str, str]:
        return host_env({"GIT_DIR": str(self.git_dir(name)), "GIT_WORK_TREE": folder, "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})

    def _flags(self) -> list[str]:
        """No hooks (an empty private folder), no fsmonitor program, no background gc or maintenance, no signing:
        git only ever reads the folder and writes the private history."""
        hooks = self.root / ".no-hooks"
        hooks.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = {"core.symlinks": "false", "core.hooksPath": str(hooks), "core.fsmonitor": "false",
                 "core.untrackedCache": "false", "gc.auto": "0", "maintenance.auto": "false",
                 "commit.gpgsign": "false", "user.name": "Pythonator", "user.email": "pythonator@localhost"}
        return [a for k, v in flags.items() for a in ("-c", f"{k}={v}")]

    async def _git(self, name: str, folder: str, *args: str, timeout: float = 60) -> tuple[int, str]:
        git = resolve("git")
        if not git:
            return 127, "git isn't installed"
        p = await asyncio.create_subprocess_exec(
            git, *self._flags(), *args,
            env=self._env(name, folder), cwd=folder, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **NO_WINDOW)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except TimeoutError:
            p.kill()
            return 124, "git took too long"
        return p.returncode or 0, out.decode("utf-8", "replace")

    async def _init(self, name: str, folder: str) -> None:
        gd = self.git_dir(name)
        if not gd.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            await self._git(name, folder, "init", "-q")  # GIT_DIR is outside the folder; the folder gets no .git
            (gd / "info").mkdir(exist_ok=True)
            (gd / "info" / "exclude").write_text("\n".join(EXCLUDE) + "\n", encoding="utf-8")  # ours: the data folder

    async def snapshot(self, name: str, folder: str, message: str) -> str:
        """Commit the folder as it is now. Returns the short id, or "" when nothing changed (or git is missing)."""
        await self._init(name, folder)
        code, _ = await self._git(name, folder, "add", "-A", "--", ".")
        if code != 0:
            return ""
        code, out = await self._git(name, folder, "commit", "-q", "--no-verify", "-m", message[:200])
        if code != 0:
            return ""
        code, out = await self._git(name, folder, "rev-parse", "--short=10", "HEAD")
        return out.strip() if code == 0 else ""

    async def log(self, name: str, folder: str, n: int = 20) -> list[dict[str, str]]:
        if not self.git_dir(name).exists():
            return []
        code, out = await self._git(name, folder, "log", f"-{n}", "--format=%h%x09%ct%x09%s", "--shortstat")
        if code != 0:
            return []
        rows: list[dict[str, str]] = []
        for line in out.splitlines():
            if "\t" in line:
                h, ts, msg = line.split("\t", 2)
                rows.append({"id": h, "ts": ts, "message": msg, "stat": ""})
            elif line.strip() and rows:
                rows[-1]["stat"] = line.strip()
        return rows

    async def changed(self, name: str, folder: str) -> list[str]:
        """Files changed since the last snapshot (the Git section's "since the last snapshot")."""
        if not self.git_dir(name).exists():
            return []
        code, out = await self._git(name, folder, "status", "--porcelain", "-uall")
        return [x[3:] for x in out.splitlines() if len(x) > 3] if code == 0 else []

    async def restore(self, name: str, folder: str, rev: str) -> int:
        """Put every tracked file back as it was in `rev`, through safefs (a link in the folder is refused).
        Files added after `rev` are left alone. Returns how many files were written."""
        if not rev.isalnum() or len(rev) > 40:
            raise ValueError("bad snapshot id")
        await self.snapshot(name, folder, f"before restoring {rev}")
        code, out = await self._git(name, folder, "ls-tree", "-r", "-z", "--name-only", rev)
        if code != 0:
            raise ValueError(out.strip()[:200])
        n = 0
        for rel in [x for x in out.split("\0") if x]:
            code, data = await self._git_bytes(name, folder, "show", f"{rev}:{rel}")
            if code == 0 and len(data) <= MAX_FILE:
                safefs.write_bytes(folder, rel, data, make_parents=True)
                n += 1
        await self.snapshot(name, folder, f"restored {rev}")
        return n

    async def _git_bytes(self, name: str, folder: str, *args: str) -> tuple[int, bytes]:
        git = resolve("git")
        if not git:
            return 127, b""
        p = await asyncio.create_subprocess_exec(git, *self._flags(), *args, env=self._env(name, folder), cwd=folder, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **NO_WINDOW)
        out, _ = await p.communicate()
        return p.returncode or 0, out
