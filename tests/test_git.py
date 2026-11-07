"""Git pulls and clones: what a checkout may set, links and files planted in ``.git``, fetching only the saved
address and branch, git's timeout, and pulls that wait for every sandboxed service that may write the checkout."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pythonator.core import git
from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.supervisor import Slot, State, Supervisor, _write_folders
from pythonator.paths import IS_WINDOWS, DataDirs

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")


@needs_git
def test_a_pull_refuses_a_checkout_whose_config_names_a_program(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/x.git"], check=True)
    git.check_checkout(repo)  # what git init and a clone write is fine
    subprocess.run(["git", "-C", str(repo), "config", "core.sshCommand", "touch pwned"], check=True)
    with pytest.raises(git.GitError, match=r"core\.sshcommand"):
        git.check_checkout(repo)


@needs_git
@pytest.mark.parametrize("key", ["filter.x.smudge", "credential.helper", "include.path", "core.gitProxy"])
def test_every_setting_that_runs_something_is_refused(tmp_path: Path, key: str) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", key, "x"], check=True)
    with pytest.raises(git.GitError, match="run programs"):
        git.check_checkout(repo)


def test_git_hooks_folder_is_private(dirs: DataDirs) -> None:
    flag = git._hooks_off()[1]
    assert flag.startswith("core.hooksPath=") and Path(flag.split("=", 1)[1]).is_relative_to(dirs.root)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/x.git"], check=True)
    return repo


@needs_git
def test_git_pins_sign_in_programs_to_your_own_config(dirs: DataDirs, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout's config changed after the check (the race) still can't name a program git runs as you."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    repo = _repo(tmp_path)
    git.check_checkout(repo)
    marker = tmp_path / "ran"
    # What a sandboxed service in the same folder writes between the check and the pull.
    subprocess.run(["git", "-C", str(repo), "config", "core.sshCommand", "planted-ssh"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "credential.helper", f"!echo x > '{marker.as_posix()}'"],
                   check=True)

    assert git.run(["config", "core.sshCommand"], repo).strip() == "ssh"

    def fill(flags: list[str]) -> None:
        subprocess.run([git.git_path(), *git.HARDENING, *flags, "credential", "fill"], cwd=repo, env=git._env(),
                       input="protocol=https\nhost=example.com\n\n", capture_output=True, text=True, timeout=60,
                       check=False)

    fill(git._your_settings(git.git_path(), git._env(), repo))
    assert not marker.exists()
    fill([])  # the same without the pins: the planted helper runs, so the check above means something
    assert marker.exists()

    # Your own settings still count: a global helper and SSH command are passed through.
    gitconfig.write_text('[credential]\n\thelper = store\n[core]\n\tsshCommand = ssh -i ~/.ssh/work\n',
                         encoding="utf-8")
    flags = git._your_settings(git.git_path(), git._env(), repo)
    assert flags[:2] == ["-c", "credential.helper="]
    assert "credential.helper=store" in flags and "core.sshCommand=ssh -i ~/.ssh/work" in flags
    assert git.run(["config", "core.sshCommand"], repo).strip() == "ssh -i ~/.ssh/work"


def test_git_ssh_becomes_a_command_no_config_can_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_SSH", "C:/Program Files/PuTTY/plink.exe")
    assert git._env()["GIT_SSH_COMMAND"] == "'C:/Program Files/PuTTY/plink.exe'"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -v")
    assert git._env()["GIT_SSH_COMMAND"] == "ssh -v"


def test_a_pull_checks_the_config_again_before_the_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    steps: list[str] = []
    monkeypatch.setattr(git, "check_checkout", lambda folder, url="": steps.append("check"))
    def run(args: list[str], *rest: Any, **named: Any) -> str:
        steps.append(args[0])
        return "abc"

    monkeypatch.setattr(git, "run", run)
    git.pull(tmp_path, "https://example.com/x.git")
    assert steps == ["check", "rev-parse", "fetch", "check", "merge", "rev-parse"]


def _service(name: str, folder: Path, mode: str = "sandbox", **more: Any) -> Service:
    return Service.model_validate({"name": name, "folder": str(folder), "runtime": "python", "entry": "app.py",
                                   "mode": mode, **more})


@pytest.fixture
def sup(dirs: DataDirs) -> Any:
    supervisor = Supervisor(dirs, Store(dirs))
    yield supervisor
    supervisor.close()


def _checkout(tmp_path: Path) -> Path:
    folder = tmp_path / "app"
    (folder / ".git").mkdir(parents=True)
    (folder / "worker").mkdir()
    return folder


class _Running:
    """Stands in for a Runner: something of that service is running."""

    pid = 0
    created = 1.0
    seen: tuple[Any, ...] = ()
    limit_hit = ""

    def kill(self) -> None:
        pass

    def stop(self, grace: float) -> None:
        pass


def _slot(sup: Supervisor, service: Service) -> Slot:
    sup.add(service)
    return sup.slots[service.id]


def test_a_pull_waits_for_every_sandboxed_service_in_the_folder(sup: Supervisor, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _checkout(tmp_path)
    pulled: list[str] = []
    monkeypatch.setattr(sup, "_pull", lambda slot, then: pulled.append(slot.service.name))
    web = _slot(sup, _service("web", folder, git={"url": "https://example.com/x.git"}))
    worker = _slot(sup, _service("worker", folder / "worker"))  # inside the checkout: it can write it too
    elsewhere = _slot(sup, _service("elsewhere", tmp_path / "other"))
    worker.runner = elsewhere.runner = _Running()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="Stop worker first"):
        sup.pull(web.service.id)
    worker.runner = None
    worker.shell = _Running()  # type: ignore[assignment]  # a console command counts too
    with pytest.raises(ValueError, match="Stop worker first"):
        sup.pull(web.service.id)
    worker.shell = None
    sup.pull(web.service.id)  # another folder's service doesn't matter
    assert pulled == ["web"]
    full = _slot(sup, _service("full", folder, mode="full", full_access_ok=True))
    full.runner = _Running()  # type: ignore[assignment]
    sup.pull(web.service.id)  # Full access runs as you anyway
    assert pulled == ["web", "web"]


def test_pull_on_start_is_skipped_while_another_sandboxed_service_runs_there(
        sup: Supervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _checkout(tmp_path)
    pulled: list[str] = []
    started: list[str] = []
    monkeypatch.setattr(sup, "_pull", lambda slot, then: pulled.append(slot.service.name))
    monkeypatch.setattr(sup, "_prepare_env", lambda slot: started.append(slot.service.name))
    web = _slot(sup, _service("web", folder, git={"url": "https://example.com/x.git", "pull_on_start": True}))
    worker = _slot(sup, _service("worker", folder))
    worker.runner = _Running()  # type: ignore[assignment]
    sup.start(web.service.id)
    assert pulled == [] and started == ["web"]
    assert "Didn't git pull: worker is sandboxed in this folder" in web.log.tail()
    worker.runner = None
    web.state = State.STOPPED
    sup.start(web.service.id)
    assert pulled == ["web"]


def test_nothing_sandboxed_starts_in_a_folder_git_is_pulling(sup: Supervisor, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _checkout(tmp_path)
    started: list[str] = []
    monkeypatch.setattr(sup, "_prepare_env", lambda slot: started.append(slot.service.name))
    web = _slot(sup, _service("web", folder))
    worker = _slot(sup, _service("worker", folder))
    full = _slot(sup, _service("full", folder, mode="full", full_access_ok=True))
    web.pulling = True
    sup.start(worker.service.id)
    sup.start(full.service.id)
    assert started == ["full"]
    assert "Waiting for git to finish pulling web's folder" in worker.log.tail()
    with pytest.raises(ValueError, match="Wait for git to finish pulling web's folder"):
        sup.shell(worker.service.id, "echo hi")
    with pytest.raises(ValueError, match="Wait for git"):
        sup.install(worker.service.id)
    sup.tick()
    assert started == ["full"]  # still pulling
    web.pulling = False
    sup.tick()
    assert started == ["full", "worker"]


def test_git_allows_only_https_and_ssh_whatever_a_checkout_says() -> None:
    """A checkout's own protocol.ext.allow=always beats -c protocol.allow=never; GIT_ALLOW_PROTOCOL beats both."""
    assert git._env()["GIT_ALLOW_PROTOCOL"] == "https:ssh"


def test_write_grants_under_more_folders_count_as_writers_of_a_checkout(tmp_path: Path) -> None:
    other = tmp_path / "b"
    other.mkdir()
    service = Service.model_validate({"name": "b", "folder": str(other), "entry": "app.py",
                                      "sandbox": {"folders": [{"path": str(tmp_path / "a"), "write": True},
                                                              {"path": str(tmp_path / "c")}]}})
    assert _write_folders(service) == [str(other), str(tmp_path / "a")]  # read-only grants don't count
    full = service.model_copy(update={"mode": "full"})
    assert _write_folders(full) == [str(other)]


def test_a_pull_waits_for_a_service_that_may_write_the_checkout_from_more_folders(
        dirs: DataDirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sup = Supervisor(dirs, Store(dirs))
    try:
        checkout = tmp_path / "a"
        (checkout / ".git").mkdir(parents=True)
        (tmp_path / "b").mkdir()
        web = Service.model_validate({"name": "web", "folder": str(checkout), "entry": "app.py",
                                      "git": {"url": "https://example.com/x.git"}})
        other = Service.model_validate({"name": "other", "folder": str(tmp_path / "b"), "entry": "app.py",
                                        "sandbox": {"folders": [{"path": str(checkout), "write": True}]}})
        sup.add(web)
        sup.add(other)
        sup.slots[other.id].runner = SimpleNamespace(pid=0, seen=(), kill=lambda *a: None)  # type: ignore[assignment]
        monkeypatch.setattr(sup, "_pull", lambda slot, then: None)
        with pytest.raises(ValueError, match="Stop other first"):
            sup.pull(web.id)
    finally:
        sup.close()


URL = "https://example.com/x.git"


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout


def _cloned(tmp_path: Path) -> Path:
    repo = tmp_path / "app"
    _git("init", "-q", str(repo))
    _git("remote", "add", "origin", URL, cwd=repo)
    (repo / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-qm", "a", cwd=repo)
    return repo


@needs_git
def test_a_link_inside_git_is_refused(tmp_path: Path) -> None:
    repo = _cloned(tmp_path)
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


posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX files and process groups")


@needs_git
@posix
def test_a_fifo_or_a_hard_link_inside_git_is_refused(tmp_path: Path) -> None:
    repo = _cloned(tmp_path)
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
    repo = _cloned(tmp_path)
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


@needs_git
def test_an_origin_the_service_changed_is_refused(tmp_path: Path) -> None:
    repo = _cloned(tmp_path)
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
