"""0.0.19, the final audit: a pull refuses a ``.git`` it can't read in full and runs no automatic gc, always uses the
service's own ``.git``, and git's timeout holds even when a helper outlives git; a sandbox can't open your files to
other accounts or make set-user-ID programs; a plugin manifest that is a link to a device is refused at once; Stop
ends a Full access child that just left the group."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation, launcher
from pythonator.core import git
from pythonator.core.models import Service
from pythonator.isolation import base
from pythonator.paths import IS_WINDOWS, DataDirs

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")
posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX files and process groups")
URL = "https://example.com/x.git"


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout


def _checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "app"
    _git("init", "-q", str(repo))
    _git("remote", "add", "origin", URL, cwd=repo)
    (repo / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-qm", "a", cwd=repo)
    return repo


@needs_git
@posix
def test_a_broken_git_doesnt_make_the_pull_use_a_repository_around_the_folder(tmp_path: Path,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Final audit F-S1: the service deletes its own .git/HEAD, git skips that .git and finds the repository the
    service's folder sits in (a clone of a public project, a home folder kept in git). The pull then fetched into it
    and fast-forwarded it to the service's upstream, writing files outside the folder."""
    public = tmp_path / "public.git"
    _git("init", "-q", "--bare", str(public))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(public), str(seed))
    (seed / "run.sh").write_text("echo hello\n")
    _git("add", "run.sh", cwd=seed)
    _git("commit", "-qm", "1", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    parent = tmp_path / "parent"  # the user's own clone of the public project
    _git("clone", "-q", "--branch", "main", str(public), str(parent))
    bot = tmp_path / "bot.git"  # the service's upstream, which its author controls
    _git("clone", "-q", "--bare", str(public), str(bot))
    svc = parent / "bot"
    _git("clone", "-q", "--branch", "main", str(bot), str(svc))
    (seed / "run.sh").write_text("touch PWNED\n")  # the author's next commit, on top of the public project
    _git("commit", "-qam", "2", cwd=seed)
    _git("push", "-q", str(bot), "HEAD:main", cwd=seed)
    (svc / ".git" / "HEAD").unlink()  # what the sandboxed service does to its own folder
    real_env = git._env
    monkeypatch.setattr(git, "_env", lambda: {**real_env(), "GIT_ALLOW_PROTOCOL": "file"})
    monkeypatch.setattr(git, "HARDENING", [*git.HARDENING, "-c", "protocol.file.allow=always"])
    from pythonator.core import validate

    monkeypatch.setattr(validate, "git_url", lambda url: url)
    head = _git("rev-parse", "HEAD", cwd=parent)
    with pytest.raises(git.GitError):
        git.pull(svc, str(bot), "main")
    assert _git("rev-parse", "HEAD", cwd=parent) == head
    assert (parent / "run.sh").read_text() == "echo hello\n"
    assert not _git("for-each-ref", "refs/pythonator", cwd=parent).strip()


def test_seccomp_keeps_modes_that_open_your_files_to_others_out_on_every_stack() -> None:
    for group in (True, False):  # with bubblewrap too: the service's own folder is a real folder of yours
        rules = launcher.seccomp_rules(block_net=False, block_unix=False, group=group)
        names = {name for _a, name, _c in rules}
        assert {"chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchownat", "openat", "mknodat"} <= names
        assert "openat2" in launcher.UNSUPPORTED
        world = [c for a, name, c in rules if name == "fchmodat" and a == 0x00050000]
        assert [(2, 7, 0o0002, 0o0002)] in world and [(2, 7, 0o4000, 0o4000)] in world


@pytest.mark.skipif(IS_WINDOWS or not launcher.landlock_abi(), reason="Linux Landlock")
def test_a_sandbox_cant_open_your_files_to_other_accounts(tmp_path: Path, dirs: DataDirs,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Final audit F-S6: Landlock doesn't cover chmod. Without bubblewrap a sandboxed service made ~/.bashrc and
    services.json writable by every account, and in its own folder it could make a set-user-ID program."""
    from pythonator.isolation import linux

    real = linux.support()
    if not real.seccomp:
        pytest.skip("needs libseccomp")
    monkeypatch.setattr(linux, "support", lambda: linux.Support(real.landlock, real.seccomp, ""))
    outside = tmp_path / "outside.txt"
    outside.write_text("yours")
    outside.chmod(0o600)
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text(
        "import os\n"
        f"os.chmod({str(outside)!r}, 0o666)\n"
        "open('own', 'w').close()\n"
        "os.chmod('own', 0o4755)\n"
        "try:\n"
        "    os.close(os.open('made', os.O_CREAT | os.O_WRONLY, 0o4755))\n"
        "    print('MADE')\n"
        "except PermissionError:\n"
        "    print('REFUSED')\n"
        "os.chmod('own', 0o750)\n"
        "print('DONE', oct(os.stat('own').st_mode & 0o7777))\n")
    service = Service.model_validate({"name": "chmod", "folder": str(project), "entry": "app.py"})
    cmd = isolation.prepare(service, dirs)
    done = subprocess.run(cmd.argv, cwd=cmd.cwd, env=cmd.env, capture_output=True, text=True, timeout=60, check=False)
    assert "REFUSED" in done.stdout and "DONE 0o750" in done.stdout, done.stdout + done.stderr
    assert outside.stat().st_mode & 0o7777 == 0o600


# -- Final audit (F-A1): a folder the walk can't list, and gc after a pull -----------------------------------------


@needs_git
@posix
@pytest.mark.skipif(not IS_WINDOWS and os.geteuid() == 0, reason="root lists any folder whatever its mode")
def test_a_git_folder_the_walk_cant_list_is_refused(tmp_path: Path) -> None:
    """Before, os.walk skipped a folder it couldn't list, while git still opened the names inside it: a service
    planted a link at .git/logs/HEAD, made .git -wx, and the pull appended to the link's target."""
    repo = _checkout(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (repo / ".git" / "logs" / "HEAD").unlink()
    (repo / ".git" / "logs" / "HEAD").symlink_to(victim)
    for hidden in (repo / ".git" / "logs", repo / ".git"):
        hidden.chmod(0o300)
        try:
            with pytest.raises(git.GitError, match="can't be read"):
                git.check_checkout(repo, URL)
        finally:
            hidden.chmod(0o700)


def test_hardening_turns_automatic_gc_off() -> None:
    flags = " ".join(git.HARDENING)
    assert "gc.auto=0" in flags and "maintenance.auto=false" in flags


@needs_git
@posix
def test_a_pull_runs_no_gc_so_worktree_prune_cant_delete_anything(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """gc's worktree prune deletes .git/worktrees/<id> recursively, following a link there into any folder. A
    service could trigger it with 30 loose objects in objects/17. A pull now never runs gc, so a worktree entry
    (here a plain one, which the walk allows) is still there afterwards."""
    upstream = tmp_path / "up.git"
    _git("init", "-q", "--bare", str(upstream))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(upstream), str(seed))
    (seed / "a").write_text("1")
    _git("add", "a", cwd=seed)
    _git("commit", "-qm", "1", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    repo = tmp_path / "svc"
    _git("clone", "-q", "--branch", "main", str(upstream), str(repo))
    (seed / "a").write_text("2")
    _git("commit", "-qam", "2", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    import hashlib
    import zlib

    made, i = 0, 0
    while made < 30:  # what gc --auto counts: loose objects in objects/17
        i += 1
        raw = b"blob 8\0" + f"{i:07d}\n".encode()
        oid = hashlib.sha1(raw).hexdigest()  # noqa: S324 - git's object name
        if oid.startswith("17") and not (repo / ".git" / "objects" / "17" / oid[2:]).exists():
            (repo / ".git" / "objects" / "17").mkdir(exist_ok=True)
            (repo / ".git" / "objects" / "17" / oid[2:]).write_bytes(zlib.compress(raw))
            made += 1
    (repo / ".git" / "worktrees" / "x").mkdir(parents=True)  # no gitdir file: worktree prune removes it
    real_env = git._env
    monkeypatch.setattr(git, "_env", lambda: {**real_env(), "GIT_ALLOW_PROTOCOL": "file"})
    monkeypatch.setattr(git, "HARDENING", [*git.HARDENING, "-c", "protocol.file.allow=always",
                                           "-c", "gc.autoDetach=false"])
    from pythonator.core import validate

    monkeypatch.setattr(validate, "git_url", lambda url: url)
    assert git.pull(repo, str(upstream), "main")
    assert (repo / ".git" / "worktrees" / "x").is_dir()


@posix
def test_a_plugin_manifest_that_is_a_link_to_dev_zero_is_refused_at_once(tmp_path: Path) -> None:
    """Before, stat() said 0 bytes and read_text() read /dev/zero until the background service ran out of memory
    (a plugin installed from git may hold links; the link check came after the manifest was read)."""
    from pythonator.plugins import manifest

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.toml").symlink_to("/dev/zero")
    started = time.monotonic()
    with pytest.raises(ValueError, match="Can't read"):
        manifest.load(plugin)
    assert time.monotonic() - started < 5
    (plugin / "plugin.toml").unlink()
    (plugin / "plugin.toml").write_text("# " + "x" * 70_000 + "\n")
    with pytest.raises(ValueError, match="larger than 64 KB"):
        manifest.load(plugin)


@posix
def test_a_helper_that_outlives_git_and_holds_its_output_doesnt_hold_the_pull(tmp_path: Path,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Before, the timeout covered git itself only: after git exited, the output reader was joined without a limit,
    so a helper left running with git's output open held the pull (or a plugin install on the daemon's thread)."""
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho fetched\n(sleep 60) &\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(git, "git_path", lambda: str(fake))
    monkeypatch.setattr(git, "_your_settings", lambda *a: [])
    started = time.monotonic()
    assert git.run(["fetch"], tmp_path, timeout=2) == "fetched\n"
    assert time.monotonic() - started < 15


@posix
def test_stop_ends_a_full_access_child_that_just_left_the_group(tmp_path: Path) -> None:
    """Full access has no marker and no group lock: a child that called setsid() less than one stats sample (2 s)
    before Stop was reparented by SIGTERM and outlived Stop and Quit. Stop now looks at the tree first."""
    import queue
    import sys

    import psutil

    from pythonator.daemon.runner import Exited, Runner

    (tmp_path / "app.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import os, time; os.setsid(); time.sleep(3119)'])\n"
        "print('started', flush=True)\ntime.sleep(60)\n")
    sink: queue.SimpleQueue[Any] = queue.SimpleQueue()
    run = Runner("full", base.Command([sys.executable, "app.py"], tmp_path, dict(os.environ)), sink)

    def left() -> list[psutil.Process]:
        return [p for p in psutil.process_iter(["cmdline"]) if "time.sleep(3119)" in " ".join(p.info["cmdline"] or [])]

    run.start()
    try:
        end = time.monotonic() + 20
        while not left() and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.3)  # it has called setsid(); no stats sample has seen it
        assert not run.seen
        run.stop(5.0)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            with contextlib.suppress(queue.Empty):
                if isinstance(sink.get(timeout=0.1), Exited):
                    break
        end = time.monotonic() + 3
        while left() and time.monotonic() < end:
            time.sleep(0.05)
        assert not left(), "a Full access child that left the group outlived Stop"
    finally:
        for p in left():
            p.kill()
