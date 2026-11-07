"""Sandbox rules that need no escape attempt: folders a service may not be given, what it inherits, its devices,
ports and process group, and what a service writes in its own folder can't harm Pythonator."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation, launcher
from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.runner import Exited, Runner
from pythonator.daemon.supervisor import Supervisor
from pythonator.isolation import base, linux
from pythonator.paths import IS_LINUX, IS_WINDOWS, DataDirs, source_root


def test_a_sandbox_with_ports_may_also_take_one_the_system_picks(tmp_path: Path) -> None:
    from pythonator import launcher

    if launcher.landlock_abi() < 4:
        pytest.skip("Landlock can't restrict ports on this kernel")
    import subprocess
    import sys

    code = ("import socket\nfrom pythonator import launcher\n"
            "launcher.apply_landlock(['/'], ['/tmp'], block_tcp=False, scope=False, bind_ports=PORTS)\n"
            "s = socket.socket()\ntry:\n    s.bind(('127.0.0.1', 0)); print('picked')\n"
            "except OSError:\n    print('refused')\n"
            "t = socket.socket()\ntry:\n    t.bind(('127.0.0.1', 1)); print('other')\n"
            "except OSError:\n    print('no other')\n")
    root = str(Path(__file__).resolve().parent.parent)

    def run(ports: list[int]) -> str:
        done = subprocess.run([sys.executable, "-c", code.replace("PORTS", repr(ports))], capture_output=True,
                              text=True, cwd=root, check=False, timeout=60)
        return " ".join(done.stdout.split())

    assert run([5999]) == "refused no other"
    assert run([5999, 0]) == "picked no other"


def test_linux_sandbox_plans_add_the_picked_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pythonator.isolation import linux
    from pythonator.isolation.base import Policy

    monkeypatch.setattr(linux, "support", lambda: linux.Support(landlock=4, seccomp="libseccomp.so.2", bwrap=""))
    for ports, expected in (((5173,), [5173, 0]), ((), [])):
        cmd = linux.wrap(["node"], tmp_path, {}, Policy(network=True, ports=ports), tmp_path / "h", tmp_path / "t")
        plan = json.loads(cmd.argv[-1])
        assert plan["landlock"]["ports"] == expected


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account can't make symbolic links")


def test_env_file_links_out_of_the_folder_are_not_read(tmp_path: Path) -> None:
    folder, outside = tmp_path / "svc", tmp_path / "home"
    folder.mkdir()
    outside.mkdir()
    (outside / "credentials").write_text("aws_secret_access_key = hunter2hunter2\n")
    _link(folder / ".env", outside / "credentials")
    assert base.read_env_file(folder, ".env") == {}
    assert base.read_env_file(folder, "../home/credentials") == {}


def test_env_files_cant_change_programs_outside_the_sandbox(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LD_LIBRARY_PATH=/x\nPATH=/x\nDOCKER_HOST=ssh://x\nDOCKER_CONFIG=/x\n"
                                   "HOME=/x\nXDG_RUNTIME_DIR=/x\nGCONV_PATH=/x\nAPI_KEY=fine\n")
    assert base.read_env_file(tmp_path, ".env") == {"API_KEY": "fine"}


def test_inherited_secrets_stay_out_of_a_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.core.models import Service

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_inherited")
    monkeypatch.setenv("LANG", "C.UTF-8")
    service = Service(name="s", folder=str(tmp_path), runtime="custom", command=["x"], env={"MY_TOKEN": "mine"})
    sandboxed = base.base_env(service, sandboxed=True)
    assert "GITHUB_TOKEN" not in sandboxed and sandboxed["MY_TOKEN"] == "mine" and sandboxed["LANG"] == "C.UTF-8"
    assert base.base_env(service, sandboxed=False)["GITHUB_TOKEN"] == "ghp_inherited"  # Full access: yours


def test_pyvenv_cfg_cant_grant_a_sandbox_any_folder(tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()  # runtime_folders resolves (macOS /private/var, Windows short names)
    project, victim = tmp_path / "project", tmp_path / "victim"
    (victim / "bin").mkdir(parents=True)
    (victim / "secret.txt").write_text("x")
    venv = project / ".venv"
    py = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = {victim / 'bin'}\n")
    folders = base.runtime_folders(str(py), project)
    assert victim not in folders and venv in folders
    (victim / "lib" / "python3.13").mkdir(parents=True)  # a real Python installation is fine
    (victim / "lib" / "python3.13" / "os.py").write_text("")
    (victim / "Lib").mkdir(exist_ok=True)
    (victim / "Lib" / "os.py").write_text("")
    assert victim in base.runtime_folders(str(py), project)


def test_a_folder_that_holds_keys_cant_be_given_to_a_sandbox(dirs: DataDirs) -> None:
    assert base.forbidden_read(Path.home() / ".config", dirs) == "a folder of passwords or keys"
    assert base.forbidden_write(Path.home() / ".ssh" / "x", dirs) == "a folder of passwords or keys"
    assert base.forbidden_read(Path.home() / "projects" / "app", dirs) == ""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox plan")
def test_linux_sandbox_reads_only_the_device_files_it_needs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from pythonator.isolation import linux

    sup = linux.Support(landlock=5, seccomp="libseccomp.so.2", bwrap="")
    monkeypatch.setattr(linux, "support", lambda: sup)
    cmd = linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
    plan = json.loads(cmd.argv[-1])
    assert "/dev" not in plan["landlock"]["ro"] and "/dev/null" in plan["landlock"]["rw"]
    assert "/dev/pts" not in plan["landlock"]["rw"]  # without bubblewrap those are your own terminals
    assert plan["seccomp"]["unix"]
    with_bwrap = linux.Support(landlock=5, seccomp="libseccomp.so.2", bwrap="/usr/bin/bwrap")
    monkeypatch.setattr(linux, "support", lambda: with_bwrap)
    cmd = linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
    assert json.loads(cmd.argv[-1])["seccomp"]["unix"]  # Landlock 5 can't keep X11's abstract socket out


def test_landlock_without_seccomp_or_bubblewrap_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Landlock (before ABI 9) doesn't stop connect() to a Unix socket by path: the session bus, docker.sock."""
    alone = linux.Support(landlock=7, seccomp="", bwrap="")
    monkeypatch.setattr(linux, "support", lambda: alone)
    with pytest.raises(base.IsolationError, match="libseccomp2 or bubblewrap"):
        linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
    assert dict(alone.capabilities())["Other processes"] == "not enforced"
    for usable in (linux.Support(7, "libseccomp.so.2", ""), linux.Support(7, "", "/usr/bin/bwrap")):
        monkeypatch.setattr(linux, "support", lambda usable=usable: usable)
        linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
        assert dict(usable.capabilities())["Other processes"] == "enforced"


@pytest.fixture
def sup(dirs: DataDirs) -> Any:
    supervisor = Supervisor(dirs, Store(dirs))
    yield supervisor
    supervisor.close()


@pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap is Linux only")
def test_bubblewrap_check_tries_what_a_start_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """In Docker or LXC bubblewrap can make a user namespace but not mount /proc: the check must find that, so
    Sandbox mode falls back to Landlock + seccomp instead of every start failing."""
    seen: list[list[str]] = []

    def run(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        return subprocess.CompletedProcess(args, 1, "", "bwrap: Can't mount proc on /newroot/proc: Operation not "
                                                        "permitted\n")

    monkeypatch.setattr(linux, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(subprocess, "run", run)
    linux.support.cache_clear()
    try:
        sup = linux.support()
    finally:
        linux.support.cache_clear()
    assert {"--unshare-pid", "--proc", "--dev"} <= set(seen[0])
    assert sup.bwrap == "" and "mount proc" in sup.bwrap_problem


def test_seccomp_makes_setsid_and_setpgid_do_nothing() -> None:
    rules = {name: action for action, name, _cmps in launcher.seccomp_rules(block_net=False, block_unix=True)}
    assert rules["setsid"] == rules["setpgid"] == 0x00050000  # SCMP_ACT_ERRNO(0): "done", and nothing changes


def _sandboxed(project: Path, dirs: DataDirs, script: str) -> base.Command:
    (project / "app.py").write_text(textwrap.dedent(script))
    service = Service.model_validate({"name": "hop", "folder": str(project), "entry": "app.py"})
    try:
        cmd = isolation.prepare(service, dirs)
    except isolation.IsolationError as exc:
        pytest.skip(f"no sandbox here: {exc}")
    if IS_LINUX and not linux.support().seccomp:
        pytest.skip("the group lock needs libseccomp (bubblewrap's own PID namespace covers the rest)")
    return cmd


@pytest.mark.skipif(not IS_LINUX, reason="seccomp is Linux's")
def test_a_sandboxed_program_cant_leave_its_process_group(project: Path, dirs: DataDirs,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    real = linux.support()  # without bubblewrap: under it, its PID namespace does this job and setsid is real
    monkeypatch.setattr(linux, "support", lambda: linux.Support(real.landlock, real.seccomp, ""))
    cmd = _sandboxed(project, dirs, """
        import os
        before = os.getpgrp()
        os.setsid()
        print("GROUP", before == os.getpgrp(), flush=True)
    """)
    done = subprocess.run(cmd.argv, cwd=cmd.cwd, env=cmd.env, capture_output=True, text=True, timeout=60, check=False)
    assert "GROUP True" in done.stdout, done.stdout + done.stderr


def _wait_exit(sink: queue.SimpleQueue[Any], timeout: float) -> Exited | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            item = sink.get(timeout=0.1)
        except queue.Empty:
            continue
        if isinstance(item, Exited):
            return item
        item.source.take()  # output: keep the program from waiting on its pipe
    return None


@pytest.mark.skipif(not IS_LINUX, reason="seccomp is Linux's")
@pytest.mark.parametrize("bubblewrap", [True, False], ids=["as-is", "without-bubblewrap"])
def test_stop_ends_a_child_that_starts_a_new_session_and_keeps_changing_pid(
        project: Path, dirs: DataDirs, monkeypatch: pytest.MonkeyPatch, bubblewrap: bool) -> None:
    """The 0.0.18 review's experiment: a child calls setsid() and forks a new copy of itself every half second, so
    it is never in a stats sample and never in the group. It survived Stop, and could then race a git pull.
    Without bubblewrap (Ubuntu's default) nothing but the group lock stops it."""
    if not bubblewrap:
        real = linux.support()
        monkeypatch.setattr(linux, "support", lambda: linux.Support(real.landlock, real.seccomp, ""))
    cmd = _sandboxed(project, dirs, """
        import os, sys, time
        if os.fork() == 0:
            os.setsid()
            while True:
                if os.fork():
                    os._exit(0)
                with open("hop.pid", "a") as f:
                    f.write(".")
                time.sleep(0.5)
        print("started", flush=True)
        time.sleep(60)
    """)
    sink: queue.SimpleQueue[Any] = queue.SimpleQueue()
    run = Runner("svc", cmd, sink)
    run.start()
    pid_file = project / "hop.pid"
    end = time.monotonic() + 20
    while not pid_file.exists() and time.monotonic() < end:
        time.sleep(0.1)
    time.sleep(1.2)
    run.stop(2.0)
    assert _wait_exit(sink, 20) is not None
    time.sleep(0.5)
    beats = pid_file.read_text()
    time.sleep(2.0)  # four more beats if it still lives
    assert pid_file.read_text() == beats, "the hopping child outlived Stop"


POSIX = pytest.mark.skipif(IS_WINDOWS, reason="process groups and signals")


@POSIX
def test_a_sandboxed_run_carries_its_own_marker(tmp_path: Path) -> None:
    sink: queue.SimpleQueue[Any] = queue.SimpleQueue()
    cmd = base.Command([sys.executable, "-c", "import os; print(os.environ.get('PYTHONATOR_RUN', ''))"], tmp_path,
                       dict(os.environ), sandboxed=True)
    run = Runner("svc", cmd, sink)
    run.start()
    assert _wait_exit(sink, 20) is not None
    assert len(run.run_id) == 16
    assert not Runner("x", base.Command(["true"], tmp_path, {}), sink).run_id  # Full access: none, nothing is hunted


@pytest.mark.skipif(IS_WINDOWS, reason="FIFOs are POSIX")
def test_a_fifo_or_huge_file_in_a_service_folder_never_blocks_pythonator(tmp_path: Path) -> None:
    """A sandboxed program may write its folder: ``mkfifo package.json`` made the background service wait forever
    at the next start (every service with it), and a file of gigabytes filled its memory. Each read here returns
    at once, as if the file weren't there."""
    from pythonator.core import readfile, runtimes, versions
    from pythonator.isolation.base import read_env_file
    from pythonator.plugins.files import FileRefused, ServiceFiles

    for name in (".env", "package.json", "pyproject.toml"):
        os.mkfifo(tmp_path / name)
    (tmp_path / ".venv").mkdir()
    os.mkfifo(tmp_path / ".venv" / "pyvenv.cfg")
    started = time.monotonic()
    assert read_env_file(tmp_path, ".env") == {}
    assert runtimes.env_missing("node", tmp_path) == ""
    assert runtimes.venv_config(tmp_path / ".venv", "home") == ""
    assert versions.declared("python", tmp_path) == ("", "")
    with pytest.raises(FileRefused):
        ServiceFiles(tmp_path).read("package.json")
    assert time.monotonic() - started < 5

    big = tmp_path / "big.json"
    with big.open("wb") as fh:
        fh.truncate(readfile.MAX_PROJECT_FILE * 8)  # sparse: no disk used
    with pytest.raises(OSError, match="larger than"):
        readfile.read_regular(big)
    assert len(readfile.read_regular(big, 100, head=True)) == 101
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "package.json").write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    assert versions.declared("node", tmp_path / "deep") == ("", "")
    assert runtimes.env_missing("node", tmp_path / "deep") == ""


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


def test_setsid_is_left_alone_under_bubblewrap() -> None:
    names = {name for _a, name, _c in launcher.seccomp_rules(block_net=False, block_unix=False, group=False)}
    assert "setsid" not in names and "setpgid" not in names
    names = {name for _a, name, _c in launcher.seccomp_rules(block_net=False, block_unix=False)}
    assert {"setsid", "setpgid"} <= names
