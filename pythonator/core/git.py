"""Clone and pull service code with your own ``git``, hardened.

Your git keeps your credentials, SSH agent and credential manager working. Every call is pinned down: only https
and ssh transports, no hooks, no submodules, no fsmonitor, no prompts, ``--`` before every URL or path, and pulls are
fast-forward only, so a pull never merges or rewrites anything.

A pull runs outside the sandbox, in a folder the sandboxed service can write. So before it, the checkout's own
``.git/config`` may hold only plain settings (:data:`SAFE_KEYS`): anything else could name a program for git to run
as you (``core.sshCommand``, a filter driver, ``credential.helper``, an ``include.path``...).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..paths import DataDirs, quiet_start
from . import validate
from .runtimes import which

HARDENING = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
             "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false", "-c", "credential.interactive=never"]
# The settings a checkout's own config may hold for Pythonator to pull it (section.key, or section.*.key for
# named sections such as a remote or a branch). What git and git-lfs write themselves, and nothing that runs.
SAFE_KEYS = re.compile(
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks|autocrlf|eol"
    r"|safecrlf)|extensions\.(objectformat|refstorage)|lfs\.repositoryformatversion|user\.(name|email)"
    r"|pull\.(rebase|ff)|fetch\.prune|remote\..+\.(url|pushurl|fetch|tagopt|prune)|branch\..+\.(remote|merge|rebase)")


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
    """An empty hooks folder in Pythonator's private data folder (a shared temp folder could be someone else's)."""
    empty = DataDirs().root / "git-no-hooks"
    empty.mkdir(parents=True, exist_ok=True)
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
    check_checkout(folder)
    before = run(["rev-parse", "--short", "HEAD"], folder, timeout=30).strip()
    run(["fetch", "--no-recurse-submodules", "--prune", "origin"], folder, on_line=on_line)
    run(["merge", "--ff-only", "@{upstream}"], folder, on_line=on_line)
    after = run(["rev-parse", "--short", "HEAD"], folder, timeout=30).strip()
    return "" if before == after else f"{before}..{after}"


def check_checkout(folder: Path) -> None:
    """Raise GitError unless ``folder`` is a plain checkout whose own config names nothing for git to run."""
    dot_git = folder / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir() or (dot_git / "commondir").exists():
        raise GitError(f"{folder} isn't a plain git checkout (its .git must be a folder of its own)")
    out = run(["config", "--file", str(dot_git / "config"), "--no-includes", "--null", "--list"], folder, timeout=30)
    unsafe = [key for entry in out.split("\0") if (key := entry.partition("\n")[0])
              and not SAFE_KEYS.fullmatch(key.lower())]
    if unsafe:
        raise GitError(f"{dot_git / 'config'} has settings Pythonator won't pull with, because they can run programs "
                       f"outside the sandbox: {', '.join(sorted(set(unsafe)))}. Remove them, or set them in your own "
                       "~/.gitconfig, which no sandbox can change")
