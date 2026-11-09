"""Problems and quick fixes for the editor, from ruff, run jailed.
The daemon never runs a linter on the folder: it sends the open file's text to ruff on stdin, in a jail with ruff's
own binary read-only, no home, no project folder and no network (compile_tool). Settings come from the project's
[tool.ruff] table or ruff.toml, read here through the safe reader and passed as plain values, never as a path ruff
would open. A quick fix comes back as a proposal: the whole new text, which you look at and apply (or not)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tomllib
from typing import Any

from pythonator.policy.compile import compile_tool
from pythonator.policy.host import HostFacts
from pythonator.policy.plan import Refused
from pythonator.reconcile import proc
from pythonator.safe import safefs
from pythonator.safe.hostenv import host_env
from pythonator.safe.process import NO_WINDOW

TIMEOUT_S = 20.0
KEYS = ("line-length", "target-version")
LINT_KEYS = ("select", "ignore", "extend-select")

def ruff_bin() -> str:
    try:
        import ruff

        return str(ruff.find_ruff_bin())
    except (ImportError, FileNotFoundError):
        import shutil

        return shutil.which("ruff") or ""

def project_config(folder: str) -> list[str]:
    """--config overrides from the project's own ruff settings (values only)."""
    table: dict[str, Any] = {}
    for name, sub in (("ruff.toml", None), (".ruff.toml", None), ("pyproject.toml", ("tool", "ruff"))):
        with contextlib.suppress(OSError, safefs.UnsafePath, tomllib.TOMLDecodeError):
            data = tomllib.loads(safefs.read_text(folder, name, limit=256 * 1024))
            for k in sub or ():
                data = data.get(k, {}) if isinstance(data, dict) else {}
            if data:
                table = data
                break
    out: list[str] = []
    for k in KEYS:
        if k in table and isinstance(table[k], (int, str)):
            out += ["--config", f"{k} = {json.dumps(table[k])}"]
    lint = table.get("lint", {}) if isinstance(table.get("lint"), dict) else {}
    for k in LINT_KEYS:
        v = lint.get(k, table.get(k))
        if isinstance(v, list) and all(isinstance(x, str) and x.isalnum() for x in v):
            out += ["--config", f"lint.{k} = {json.dumps(v)}"]
    return out

async def _run(argv: tuple[str, ...], env: dict[str, str], cwd: str | None, text: str) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **NO_WINDOW,
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(text.encode("utf-8")), TIMEOUT_S)
    except TimeoutError:
        proc.signal_tree(p.pid, proc.KILL)
        raise Refused("LINT-002", "ruff took longer than 20 seconds.") from None
    return p.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

ERRORS = "E9,F63,F7,F82"  # what can't run: syntax errors, undefined names, misplaced statements

async def ruff(host: HostFacts, folder: str, path: str, text: str, *, fix: bool, only: str = "",
               errors_only: bool = False) -> tuple[int, str, str]:
    exe = ruff_bin()
    if not exe:
        raise Refused("LINT-001", "ruff isn't installed next to Pythonator.", "Reinstall Pythonator (ruff ships with it).")
    args = ("check", "--isolated", "--no-cache", "--stdin-filename", path.rsplit("/", 1)[-1],
            *(("--select", ERRORS) if errors_only else project_config(folder)),
            *(("--select", only) if only.isalnum() and only else ()),
            *(("--fix", "--unsafe-fixes") if fix else ("--output-format", "json")), "-")
    jail = compile_tool("ruff", exe, args, host)
    from pathlib import Path

    from pythonator.store.files import ensure_private_dir

    for d in jail.make_dirs:
        ensure_private_dir(Path(d))
    import sys

    env = {**host_env(), **jail.env} if sys.platform == "win32" else dict(jail.env)
    return await _run(jail.argv, env, jail.host_cwd, text)

async def check(host: HostFacts, folder: str, path: str, text: str, errors_only: bool = False) -> list[dict[str, Any]]:
    """ruff's problems. `errors_only`: only what stops the file from running (the editor's default)."""
    code, out, err = await ruff(host, folder, path, text, fix=False, errors_only=errors_only)
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        raise Refused("LINT-003", f"ruff didn't answer ({code}): {err.strip()[:300]}") from None
    probs = []
    for r in rows:
        loc, end = r.get("location") or {}, r.get("end_location") or {}
        fixable = bool(r.get("fix"))
        probs.append({
            "code": r.get("code") or "", "message": r.get("message", ""), "line": loc.get("row", 1),
            "col": loc.get("column", 1), "end_line": end.get("row", loc.get("row", 1)),
            "end_col": end.get("column", loc.get("column", 1)),
            "severity": "error" if (r.get("code") or "").startswith(("E9", "F63", "F7", "F82")) or not r.get("code") else "warning",
            "fixable": fixable, "fix": (r.get("fix") or {}).get("message") or "",
        })
    return probs

async def fix(host: HostFacts, folder: str, path: str, text: str, only: str = "") -> str:
    """The text with ruff's fixes applied (all of them, or one rule's). Nothing is written: it is a proposal."""
    code, out, err = await ruff(host, folder, path, text, fix=True, only=only)
    if code not in (0, 1) or (not out and text):
        raise Refused("LINT-003", f"ruff didn't answer ({code}): {err.strip()[:300]}")
    return out
