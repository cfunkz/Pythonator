"""0.0.19, the pre-release audit: a git pull never writes through a link the service planted in ``.git`` and only
fetches the saved address, git's timeout really stops it, Container mode can't mount your home or Pythonator's own
folders, a few more places a service may not write, errors on worker threads reach the window safely, and the file
tree no longer re-reads on every change a running service makes."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation, launcher
from pythonator.core import git
from pythonator.core.models import Service
from pythonator.isolation import base
from pythonator.paths import IS_WINDOWS, DataDirs, source_root

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


# -- G1: nothing in .git may be a link or a special file -----------------------------------------------------------


@needs_git
def test_a_link_inside_git_is_refused(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("precious")
    git.check_checkout(repo, URL)  # a normal checkout is fine
    (repo / ".git" / "logs" / "HEAD").unlink()
    try:
        (repo / ".git" / "logs" / "HEAD").symlink_to(victim)
    except OSError:
        pytest.skip("this account can't make symbolic links")
    with pytest.raises(git.GitError, match="is a link"):
        git.check_checkout(repo, URL)


@needs_git
@posix
def test_a_fifo_or_a_hard_link_inside_git_is_refused(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    os.mkfifo(repo / ".git" / "FETCH_HEAD")
    with pytest.raises(git.GitError, match="special file"):
        git.check_checkout(repo, URL)
    (repo / ".git" / "FETCH_HEAD").unlink()
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (repo / ".git" / "logs" / "HEAD").unlink()
    os.link(victim, repo / ".git" / "logs" / "HEAD")
    with pytest.raises(git.GitError, match="hard link"):
        git.check_checkout(repo, URL)


@needs_git
def test_alternates_are_refused(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    (repo / ".git" / "objects" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(str(tmp_path / "other" / ".git" / "objects"))
    with pytest.raises(git.GitError, match="another repository"):
        git.check_checkout(repo, URL)


@needs_git
@posix
def test_a_pull_writes_no_fetch_head_and_no_name_the_checkout_chose(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit's reproduction, end to end with a local upstream: before, the reflog line held the checkout's own
    user.name (a shell command), appended through a link to any file."""
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
    _git("config", "user.name", "x;touch PWNED;#", cwd=repo)
    real_env = git._env
    monkeypatch.setattr(git, "_env", lambda: {**real_env(), "GIT_ALLOW_PROTOCOL": "file"})
    monkeypatch.setattr(git, "HARDENING", [*git.HARDENING, "-c", "protocol.file.allow=always"])
    from pythonator.core import validate

    monkeypatch.setattr(validate, "git_url", lambda url: url)
    changed = git.pull(repo, str(upstream), "main")
    assert changed and ".." in changed
    assert not (repo / ".git" / "FETCH_HEAD").exists()
    assert "PWNED" not in (repo / ".git" / "logs" / "HEAD").read_text()


# -- G2: the pull fetches only the saved address ------------------------------------------------------------------


@needs_git
def test_an_origin_the_service_changed_is_refused(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    git.check_checkout(repo, "https://example.com/x")  # the same address, written another way
    _git("remote", "set-url", "origin", "git@github.com:you/private.git", cwd=repo)
    with pytest.raises(git.GitError, match="make the two the same"):
        git.check_checkout(repo, URL)


def test_the_fetch_names_the_saved_address_and_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(git, "check_checkout", lambda folder, url="": None)

    def run(args: list[str], *rest: Any, **named: Any) -> str:
        calls.append(args)
        return "main" if args[:2] == ["rev-parse", "--abbrev-ref"] else "abc"

    monkeypatch.setattr(git, "run", run)
    git.pull(tmp_path, URL, "main")
    fetch = next(c for c in calls if c[0] == "fetch")
    assert fetch[-3:] == ["--", URL, f"+refs/heads/main:{git.UPSTREAM}"]
    assert "origin" not in fetch and "--no-write-fetch-head" in fetch
    assert ["merge", "--ff-only", git.UPSTREAM] in calls
    with pytest.raises(git.GitError, match="not dev"):
        git.pull(tmp_path, URL, "dev")


def test_a_service_without_a_git_address_isnt_pulled(dirs: DataDirs, tmp_path: Path) -> None:
    from pythonator.core.store import Store
    from pythonator.daemon.supervisor import Supervisor

    folder = tmp_path / "app"
    (folder / ".git").mkdir(parents=True)
    sup = Supervisor(dirs, Store(dirs))
    try:
        service = Service.model_validate({"name": "web", "folder": str(folder), "entry": "app.py"})
        sup.add(service)
        with pytest.raises(ValueError, match="Add the Git address"):
            sup.pull(service.id)
    finally:
        sup.close()


def test_hardening_pins_the_name_and_turns_reflogs_off() -> None:
    flags = " ".join(git.HARDENING)
    assert "core.logAllRefUpdates=false" in flags and "user.name=Pythonator" in flags


# -- G3: git's timeout counts from the start ----------------------------------------------------------------------


@posix
def test_a_git_that_never_answers_is_stopped_with_its_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nsleep 120 &\necho $! > \"$0.child\"\nsleep 120\n")
    fake.chmod(0o755)
    monkeypatch.setattr(git, "git_path", lambda: str(fake))
    monkeypatch.setattr(git, "_your_settings", lambda *a: [])
    started = time.monotonic()
    with pytest.raises(git.GitError, match="too long"):
        git.run(["fetch"], tmp_path, timeout=1)
    assert time.monotonic() - started < 10
    import psutil

    child = int((tmp_path / "git.child").read_text())
    time.sleep(0.2)
    with contextlib.suppress(psutil.NoSuchProcess):
        assert psutil.Process(child).status() == psutil.STATUS_ZOMBIE  # ended; init hasn't collected it yet


# -- P1, P2: folders a service may not write ----------------------------------------------------------------------


def test_container_mode_refuses_your_home_and_pythonators_data(dirs: DataDirs) -> None:
    for folder in (Path.home(), dirs.root):
        service = Service.model_validate({"name": "c", "folder": str(folder), "entry": "app.py", "mode": "container"})
        with pytest.raises(isolation.IsolationError, match="container service can't use"):
            isolation.prepare(service, dirs)


def test_more_places_that_run_code_as_you_are_refused(dirs: DataDirs, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools" / "bin"
    tools.mkdir(parents=True)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), os.environ.get("PATH", "")]))
    assert "PATH" in base.forbidden_write(tmp_path / "tools", dirs)
    assert "PATH" in base.forbidden_write(tools, dirs)
    assert base.forbidden_write(tmp_path / "project", dirs) == ""
    assert base.forbidden_write(source_root(), dirs) == "Pythonator's own program folder"
    assert base.forbidden_write(Path.home() / ".config" / "autostart", dirs).startswith("a folder of programs that")


def test_inherited_secrets_by_other_names_dont_reach_a_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("MY_PASS", "hunter22"), ("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE"), ("DATABASE_URL", "postgres://db"),
                        ("CACHE", "redis://user:pw@localhost:6379"), ("LANG", "C.UTF-8")):
        monkeypatch.setenv(name, value)
    service = Service.model_validate({"name": "s", "folder": str(tmp_path), "entry": "app.py",
                                      "env": {"MY_PASS": "set-on-purpose"}})
    env = base.base_env(service, sandboxed=True)
    assert not {"AWS_ACCESS_KEY_ID", "DATABASE_URL", "CACHE"} & env.keys()
    assert env["MY_PASS"] == "set-on-purpose" and env["LANG"] == "C.UTF-8"
    assert base.base_env(service, sandboxed=False)["DATABASE_URL"] == "postgres://db"  # Full access: yours


# -- R-PTY: the process-group rule only where bubblewrap isn't used ------------------------------------------------


def test_setsid_is_left_alone_under_bubblewrap() -> None:
    names = {name for _a, name, _c in launcher.seccomp_rules(block_net=False, block_unix=False, group=False)}
    assert "setsid" not in names and "setpgid" not in names
    names = {name for _a, name, _c in launcher.seccomp_rules(block_net=False, block_unix=False)}
    assert {"setsid", "setpgid"} <= names


# -- U1, U2: the window --------------------------------------------------------------------------------------------


def test_an_error_on_a_worker_thread_reaches_the_status_bar_on_the_window_thread(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QWidget

    from pythonator.ui.app import _Notice

    seen: list[tuple[str, bool]] = []

    class Window(QWidget):
        def _status(self, text: str, _ms: int) -> None:
            seen.append((text, threading.current_thread() is threading.main_thread()))

    window = Window()
    qtbot.addWidget(window)
    notice = _Notice(window)  # type: ignore[arg-type]
    worker = threading.Thread(target=notice.said.emit, args=("boom",))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: bool(seen), timeout=2000)
    assert seen == [("boom", True)]


@posix
def test_opening_a_fifo_doesnt_hang_the_window(qtbot: Any, tmp_path: Path) -> None:
    from pythonator.ui.editor import EditorPanel

    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    panel = EditorPanel()
    qtbot.addWidget(panel)
    said: list[str] = []
    panel.status.connect(said.append)
    started = time.monotonic()
    panel.open(fifo)
    assert time.monotonic() - started < 2 and said and "isn't a regular file" in said[0]


def test_the_file_tree_doesnt_watch_and_refresh_shows_new_files(qtbot: Any, tmp_path: Path) -> None:
    from PyQt6.QtGui import QFileSystemModel

    from pythonator.ui.editor import FileTree

    (tmp_path / "sub").mkdir()
    tree = FileTree()
    qtbot.addWidget(tree)
    tree.show()
    tree.set_root(str(tmp_path))
    assert tree.model.testOption(QFileSystemModel.Option.DontWatchForChanges)
    qtbot.waitUntil(lambda: tree.model.rowCount(tree.view.rootIndex()) == 1, timeout=5000)
    tree.view.expand(tree.model.index(str(tmp_path / "sub")))
    (tmp_path / "new.txt").write_text("x")
    tree.refresh()
    qtbot.waitUntil(lambda: tree.model.rowCount(tree.view.rootIndex()) == 2, timeout=5000)
    qtbot.waitUntil(lambda: tree.view.isExpanded(tree.model.index(str(tmp_path / "sub"))), timeout=5000)
