"""Clone and pull service code with your own ``git``, hardened.

Your git keeps your credentials, SSH agent and credential manager working. Every call is pinned down: only https
and ssh transports, no hooks, no submodules, no fsmonitor, no prompts, ``--`` before every URL or path, and pulls are
fast-forward only, so a pull never merges or rewrites anything.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..paths import quiet_start
from . import validate
from .runtimes import which

HARDENING = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
             "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false", "-c", "credential.interactive=never"]
MIN_VERSION = (2, 40)


class GitError(Exception):
    pass


def git_path() -> str:
    found = which("git")
    if not found:
        raise GitError("git isn't installed. Windows: winget install Git.Git · macOS: xcode-select --install · "
                       "Linux: your package manager")
    return found


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR", "GIT_WORK_TREE")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GCM_INTERACTIVE="never", LC_ALL="C")
    return env


def _hooks_off() -> list[str]:
    empty = Path(tempfile.gettempdir()) / "pythonator-no-hooks"
    empty.mkdir(exist_ok=True)
    return ["-c", f"core.hooksPath={empty}"]


def run(args: list[str], cwd: Path | None = None, *, timeout: float = 300,
        on_line: Callable[[str], None] | None = None) -> str:
    """Run git with the hardening flags; raises GitError with git's own message when it fails."""
    argv = [git_path(), *HARDENING, *_hooks_off(), *args]
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=0x08000000 if os.name == "nt" else 0, startupinfo=quiet_start())
    except OSError as exc:
        raise GitError(f"Couldn't start git: {exc}") from exc
    lines: list[str] = []
    for line in proc.stdout or ():
        lines.append(line)
        if on_line:
            on_line(line)
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise GitError("git took too long and was stopped") from None
    out = "".join(lines)
    if code != 0:
        raise GitError(out.strip().splitlines()[-1] if out.strip() else f"git failed (exit code {code})")
    return out


def version() -> tuple[int, int]:
    text = run(["--version"], timeout=20)  # "git version 2.47.1"
    try:
        major, minor = text.split()[2].split(".")[:2]
        return int(major), int(minor)
    except (IndexError, ValueError):
        return (0, 0)


def clone(url: str, dest: Path, branch: str = "", *, on_line: Callable[[str], None] | None = None) -> None:
    url, branch = validate.git_url(url), validate.git_ref(branch)
    if dest.exists() and any(dest.iterdir()):
        raise GitError(f"{dest} isn't empty")
    args = ["clone", "--no-recurse-submodules", "--single-branch", "--progress"]
    if branch:
        args += ["--branch", branch]
    run([*args, "--", url, str(dest)], on_line=on_line)


def pull(folder: Path, *, on_line: Callable[[str], None] | None = None) -> str:
    """Fetch and fast-forward. Returns ``"old..new"`` or "" when nothing changed."""
    if not (folder / ".git").exists():
        raise GitError(f"{folder} isn't a git checkout")
    before = run(["rev-parse", "--short", "HEAD"], folder, timeout=30).strip()
    run(["fetch", "--no-recurse-submodules", "--prune", "origin"], folder, on_line=on_line)
    run(["merge", "--ff-only", "@{upstream}"], folder, on_line=on_line)
    after = run(["rev-parse", "--short", "HEAD"], folder, timeout=30).strip()
    return "" if before == after else f"{before}..{after}"
