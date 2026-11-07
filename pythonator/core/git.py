"""Clone and pull service code with your own ``git``, hardened.

Your git keeps your credentials, SSH agent and credential manager working. Every call is pinned down: only https
and ssh transports, no hooks, no submodules, no fsmonitor, no prompts, ``--`` before every URL or path, and pulls are
fast-forward only, so a pull never merges or rewrites anything.

A pull runs outside the sandbox, in a folder the sandboxed service can write. So before it, the checkout's own
``.git/config`` may hold only plain settings (:data:`SAFE_KEYS`): anything else could name a program for git to run
as you (``core.sshCommand``, a filter driver, ``credential.helper``, an ``include.path``...). Nothing in ``.git`` may
be a link or a special file either: git, running as you, would write through it (a reflog line appended to your
``~/.bashrc``). And a pull fetches the address and branch saved in the service's settings, never the checkout's own
``origin``, which the service can change to any repository your credentials can read.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import stat
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ..paths import DataDirs, quiet_start
from . import validate
from .runtimes import which

HARDENING = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
             "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false", "-c", "credential.interactive=never",
             "-c", "core.askPass=",
             # Reflogs aren't started, and the name in one that exists is Pythonator's, not one the checkout set.
             "-c", "core.logAllRefUpdates=false", "-c", "user.name=Pythonator", "-c", "user.email=pythonator@localhost"]
UPSTREAM = "refs/pythonator/upstream"  # where a pull fetches to: not a branch or remote ref the checkout names
# The settings that name a program git runs to sign in. They are pinned on the command line, which git reads last,
# to what your own ~/.gitconfig says, so a checkout's config can't add one even if it changes between the check
# and the pull. An empty credential.helper drops every helper read before it.
PINNED = re.compile(r"credential\..*helper|core\.sshcommand")
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
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GCM_INTERACTIVE="never", LC_ALL="C",
               # Wins over every protocol.* setting, including a checkout's own protocol.ext.allow=always, which
               # is more specific than the -c protocol.allow=never on the command line and would beat it.
               GIT_ALLOW_PROTOCOL="https:ssh")
    if not env.get("GIT_SSH_COMMAND") and env.get("GIT_SSH"):
        # A checkout's core.sshCommand would win over GIT_SSH; GIT_SSH_COMMAND wins over any config.
        env["GIT_SSH_COMMAND"] = "'" + env["GIT_SSH"].replace("'", "'\\''") + "'"
    return env


def _your_settings(git: str, env: dict[str, str], cwd: Path | None) -> list[str]:
    """``-c`` flags that pin credential helpers and the SSH command to your own global git config (read in the
    checkout, so ``includeIf "gitdir:..."`` sections for it count)."""
    try:
        done = subprocess.run([git, "config", "--global", "--includes", "--null", "--get-regexp",
                               r"^(credential\..*helper|core\.sshcommand)$"], cwd=cwd or DataDirs().root, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, check=False,
                              creationflags=0x08000000 if os.name == "nt" else 0, startupinfo=quiet_start())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"Couldn't read your git settings: {exc}") from exc
    if done.returncode not in (0, 1):  # 1: none set
        raise GitError(f"Couldn't read your git settings: {done.stderr.strip() or done.returncode}")
    flags = ["-c", "credential.helper="]
    ssh = ""
    for entry in done.stdout.split("\0"):
        key, _, value = entry.partition("\n")
        if not key or not PINNED.fullmatch(key.lower()) or "=" in key:
            continue
        if key.lower() == "core.sshcommand":
            ssh = value
        else:
            flags += ["-c", f"{key}={value}"]
    if not env.get("GIT_SSH_COMMAND"):  # that variable wins over any config
        flags += ["-c", f"core.sshCommand={ssh or 'ssh'}"]
    return flags


def _hooks_off() -> list[str]:
    """An empty hooks folder in Pythonator's private data folder (a shared temp folder could be someone else's)."""
    empty = DataDirs().root / "git-no-hooks"
    empty.mkdir(parents=True, exist_ok=True)
    return ["-c", f"core.hooksPath={empty}"]


def run(args: list[str], cwd: Path | None = None, *, timeout: float = 300,
        on_line: Callable[[str], None] | None = None) -> str:
    """Run git with the hardening flags; raises GitError with git's own message when it fails."""
    git, env = git_path(), _env()
    argv = [git, *HARDENING, *_hooks_off(), *_your_settings(git, env, cwd), *args]
    try:
        # Its own process group, so a timeout ends git-remote-https and ssh with it.
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=0x08000000 if os.name == "nt" else 0, startupinfo=quiet_start(),
                                start_new_session=os.name != "nt")
    except OSError as exc:
        raise GitError(f"Couldn't start git: {exc}") from exc
    lines: list[str] = []

    def read() -> None:
        for line in proc.stdout or ():
            lines.append(line)
            if on_line:
                on_line(line)

    # Output is read on a thread, so the timeout counts from the start: a host that never answers, or a FIFO git
    # waits on, can't hold a pull (or the background service) forever.
    reader = threading.Thread(target=read, name="git-output", daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        reader.join(5)
        raise GitError("git took too long and was stopped") from None
    reader.join()
    out = "".join(lines)
    if code != 0:
        raise GitError(out.strip().splitlines()[-1] if out.strip() else f"git failed (exit code {code})")
    return out


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        import psutil

        try:
            children = psutil.Process(proc.pid).children(recursive=True)
        except psutil.Error:
            children = []
        for child in children:
            with contextlib.suppress(psutil.Error):
                child.kill()
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
    proc.wait()


def clone(url: str, dest: Path, branch: str = "", *, on_line: Callable[[str], None] | None = None) -> None:
    url, branch = validate.git_url(url), validate.git_ref(branch)
    if dest.exists() and any(dest.iterdir()):
        raise GitError(f"{dest} isn't empty")
    args = ["clone", "--no-recurse-submodules", "--single-branch", "--progress"]
    if branch:
        args += ["--branch", branch]
    run([*args, "--", url, str(dest)], on_line=on_line)


def pull(folder: Path, url: str, branch: str = "", *, on_line: Callable[[str], None] | None = None) -> str:
    """Fetch ``branch`` (empty: the default one) of ``url``, the address saved in the service's settings, and
    fast-forward to it. Returns ``"old..new"`` or "" when nothing changed."""
    url, branch = validate.git_url(url), validate.git_ref(branch)
    check_checkout(folder, url)
    before = run(["rev-parse", "--short", "HEAD"], folder, timeout=30).strip()
    if branch and (on := run(["rev-parse", "--abbrev-ref", "HEAD"], folder, timeout=30).strip()) != branch:
        raise GitError(f"The checkout is on {on}, not {branch}. Switch it to {branch}, or change the branch in the "
                       "service's settings")
    run(["fetch", "--no-recurse-submodules", "--no-tags", "--no-write-fetch-head", "--", url,
         f"+{f'refs/heads/{branch}' if branch else 'HEAD'}:{UPSTREAM}"], folder, on_line=on_line)
    check_checkout(folder, url)  # again: the merge writes files, so its config must still be plain
    run(["merge", "--ff-only", UPSTREAM], folder, on_line=on_line)
    after = run(["rev-parse", "--short", "HEAD"], folder, timeout=30).strip()
    return "" if before == after else f"{before}..{after}"


def _same_address(a: str, b: str) -> bool:
    def plain(u: str) -> str:
        return u.strip().rstrip("/").removesuffix(".git")

    return plain(a) == plain(b)


def _plain_files(dot_git: Path) -> None:
    """Raise GitError if anything in ``.git`` is a link, a junction, a special file, a second name for a file
    (a hard link) or points git at another repository's objects: git, running as you, would write or read
    through it. Objects are left out of the hard-link rule: git never writes into one that exists, and a local
    clone shares them by hard link."""
    for top, folders, files in os.walk(dot_git, followlinks=False):
        for name in (*folders, *files):
            path = Path(top, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or os.path.isjunction(path):
                what = "a link"
            elif stat.S_ISDIR(info.st_mode):
                continue
            elif not stat.S_ISREG(info.st_mode):
                what = "a special file"
            elif info.st_nlink > 1 and path.relative_to(dot_git).parts[0] != "objects":
                what = "a hard link"
            elif path.relative_to(dot_git).as_posix() in ("objects/info/alternates", "objects/info/http-alternates"):
                what = "a pointer to another repository"
            else:
                continue
            raise GitError(f"{path} is {what}. Pythonator won't pull: git runs as you, outside the sandbox, and "
                           "would write through it. Delete it, or clone the project again")


def check_checkout(folder: Path, url: str = "") -> None:
    """Raise GitError unless ``folder`` is a plain checkout whose own config names nothing for git to run, nothing
    in ``.git`` is a link or special file, and its ``origin`` (if ``url`` is given) is that address."""
    dot_git = folder / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir() or (dot_git / "commondir").exists():
        raise GitError(f"{folder} isn't a plain git checkout (its .git must be a folder of its own)")
    _plain_files(dot_git)
    out = run(["config", "--file", str(dot_git / "config"), "--no-includes", "--null", "--list"], folder, timeout=30)
    entries = [entry.partition("\n") for entry in out.split("\0")]
    if url and (origin := next((v for k, _, v in entries if k.lower() == "remote.origin.url"), "")) \
            and not _same_address(origin, url):
        raise GitError(f"This checkout's origin is {origin}, but the service's Git address is {url}. Pythonator "
                       "pulls only the address in the service's settings: make the two the same")
    unsafe = [key for key, _, _ in entries if key and not SAFE_KEYS.fullmatch(key.lower())]
    if unsafe:
        raise GitError(f"{dot_git / 'config'} has settings Pythonator won't pull with, because they can run programs "
                       f"outside the sandbox: {', '.join(sorted(set(unsafe)))}. Remove them, or set them in your own "
                       "~/.gitconfig, which no sandbox can change")
