"""0.0.19, the 0.0.18 release review: nothing a sandboxed service starts outlives Stop, a crash is seen even when a
child still holds the output, git ignores a checkout's own transports, plugins never run unsandboxed and end with
everything they started, Container mode's default images exist and can install, bots stop cleanly, an error in
one panel doesn't close the window, and the System check and file tree stay inside their window."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from pythonator import errors, isolation, launcher
from pythonator.core import git, runtimes, templates
from pythonator.core import template_files as tf
from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.runner import Exited, Runner
from pythonator.daemon.supervisor import Supervisor, _write_folders
from pythonator.isolation import base, container, linux, windows
from pythonator.paths import IS_LINUX, IS_WINDOWS, DataDirs

ROOT = Path(__file__).resolve().parent.parent
POSIX = pytest.mark.skipif(IS_WINDOWS, reason="process groups and signals")


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


# -- A6: an exit is seen even while a child still holds the output -------------------------------------------------


@POSIX
def test_a_crash_is_seen_while_a_child_still_holds_the_output(tmp_path: Path) -> None:
    """A bot that crashed while its worker (a pool, an ffmpeg) still runs showed as running and was never
    restarted: the exit was only reported once every holder of the output had closed it."""
    code = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "sys.exit(3)")
    sink: queue.SimpleQueue[Any] = queue.SimpleQueue()
    run = Runner("svc", base.Command([sys.executable, "-c", code], tmp_path, dict(os.environ)), sink)
    started = time.monotonic()
    run.start()
    done = _wait_exit(sink, 20)
    assert done is not None and done.code == 3
    assert time.monotonic() - started < 10, "the exit waited for the child"
    time.sleep(0.3)
    assert not [p for p in psutil.Process().children(recursive=True) if "time.sleep(60)" in " ".join(p.cmdline())]


# -- A1: nothing leaves a sandboxed service's process group ---------------------------------------------------------


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
def test_a_sandboxed_program_cant_leave_its_process_group(project: Path, dirs: DataDirs) -> None:
    cmd = _sandboxed(project, dirs, """
        import os
        before = os.getpgrp()
        os.setsid()
        print("GROUP", before == os.getpgrp(), flush=True)
    """)
    done = subprocess.run(cmd.argv, cwd=cmd.cwd, env=cmd.env, capture_output=True, text=True, timeout=60, check=False)
    assert "GROUP True" in done.stdout, done.stdout + done.stderr


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


# -- A2, A3: git ---------------------------------------------------------------------------------------------------


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
        web = Service.model_validate({"name": "web", "folder": str(checkout), "entry": "app.py"})
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


# -- A4, A5: plugins -----------------------------------------------------------------------------------------------


def test_official_plugins_dont_run_without_their_sandbox(dirs: DataDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.plugins import host

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise isolation.IsolationError("needs libseccomp2 or bubblewrap")

    for mod in (linux, isolation.macos if hasattr(isolation, "macos") else None, windows):
        if mod is not None:
            monkeypatch.setattr(mod, "wrap", refuse)
    folder = dirs.root / "plugin"
    folder.mkdir()
    plugin = SimpleNamespace(id="guard", folder=folder, official=True, manifest=SimpleNamespace(permissions=[]))
    stub = SimpleNamespace(dirs=dirs, granted=lambda p: set())
    with pytest.raises(isolation.IsolationError, match="Plugins only run sandboxed"):
        host.PluginHost._command(stub, plugin)  # type: ignore[arg-type]


@POSIX
def test_ending_a_plugin_ends_what_it_started() -> None:
    from pythonator.plugins.host import PluginHost

    code = ("import subprocess, sys, time; "
            "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']).pid, flush=True); "
            "time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stdout is not None
    child = int(proc.stdout.readline())
    PluginHost._end(SimpleNamespace(_jobs={}), proc)  # type: ignore[arg-type]
    proc.wait(10)
    end = time.monotonic() + 5
    while psutil.pid_exists(child) and psutil.Process(child).status() != psutil.STATUS_ZOMBIE \
            and time.monotonic() < end:
        time.sleep(0.05)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


# -- Windows: WSL's bash is no sandbox -----------------------------------------------------------------------------


def test_wsl_bash_is_refused_for_a_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    system = tmp_path / "System32"
    monkeypatch.setattr(windows, "system32", lambda: system)
    for name in ("bash.exe", "wsl.exe", "BASH.EXE"):
        with pytest.raises(base.IsolationError, match="WSL"):
            windows.check_not_wsl(str(system / name))
    windows.check_not_wsl(str(tmp_path / "Git" / "bin" / "bash.exe"))  # Git Bash is a normal program
    windows.check_not_wsl(str(system / "cmd.exe"))


# -- Container mode: images that exist, caches it can write --------------------------------------------------------


def test_default_images_are_current_and_exist() -> None:
    images = {k: r.image for k, r in runtimes.RUNTIMES.items()}
    assert images["deno"] == "denoland/deno:debian"  # there is no bare "2" tag
    assert images["go"] == "golang:1.27"  # 1.24 ended on 2026-02-10
    assert images["dotnet"].endswith("sdk:10.0")  # .NET 9 ends on 2026-11-10
    assert images["node"] == "node:24-slim"
    docs = (ROOT / "docs" / "languages.md").read_text(encoding="utf-8")
    for image in images.values():
        if image:
            assert f"`{image}`" in docs or "compose" in image


def test_container_caches_live_in_the_project_and_php_installs_with_composer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "engine", lambda choice="auto": "/usr/bin/docker")
    for rt, var in (("go", "GOMODCACHE"), ("rust", "CARGO_HOME"), ("ruby", "BUNDLE_PATH"), ("deno", "DENO_DIR"),
                    ("dotnet", "NUGET_PACKAGES")):
        service = Service.model_validate({"name": rt, "folder": str(tmp_path), "runtime": rt, "entry": "x",
                                          "mode": "container"})
        cmd = container.wrap(service)
        assert cmd.env[var].startswith("/app/.container-cache/"), rt
        assert ["-e", var] == cmd.argv[cmd.argv.index(var) - 1:cmd.argv.index(var) + 1]
    php = Service.model_validate({"name": "php", "folder": str(tmp_path), "runtime": "php", "entry": "index.php",
                                  "mode": "container"})
    assert "composer:2" in container.wrap(php, task=["composer", "install"]).argv
    assert "php:8.4-cli" in container.wrap(php).argv
    mine = php.model_copy(update={"env": {"BUNDLE_PATH": "x"}, "runtime": "ruby"})
    assert container.wrap(mine).env["BUNDLE_PATH"] == "x"  # the service's own value wins


# -- Templates: bots stop cleanly, no end-of-life floors -----------------------------------------------------------


def test_bot_templates_stop_cleanly_on_sigterm() -> None:
    assert "add_signal_handler(signal.SIGTERM" in tf.DISCORD_PY and "bot.close()" in tf.DISCORD_PY
    assert 'process.once(signal' in tf.DISCORD_JS and "client.destroy()" in tf.DISCORD_JS
    assert 'process.once("SIGTERM", () => bot.stop())' in tf.TELEGRAM_JS
    compile(tf.DISCORD_PY, "bot.py", "exec")


def test_templates_ask_for_supported_runtimes() -> None:
    from pythonator.core import versions

    floors = {"python": (3, 11), "node": (22,), "go": (1, 26), "ruby": (3, 3), "php": (8, 3), "java": (21,)}
    for t in templates.TEMPLATES.values():
        if not t.engines or t.runtime not in floors or "||" in t.engines:
            continue
        low = versions.parse(t.engines.lstrip(">=^~ ").split()[0]) if hasattr(versions, "parse") else None
        if low is not None:
            assert tuple(low)[:len(floors[t.runtime])] >= floors[t.runtime], (t.key, t.engines)
        else:
            assert not t.engines.startswith((">=3.8", ">=3.9", ">=3.10", ">=20", ">=1.22", ">=8.0", ">=11")), t.key
    discord = templates.TEMPLATES["discord-py"]
    assert "aiohttp>=3.14.5" in discord.requirements


# -- Release builds: no unhashed setuptools ------------------------------------------------------------------------


def test_builds_dont_fetch_setuptools_unhashed() -> None:
    for name in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "requirements-build.lock" in line and "pip install --no-deps" in line:
                assert "--no-build-isolation" in line, line


# -- The window: an error in one place doesn't close it -----------------------------------------------------------


def test_an_unexpected_error_is_logged_and_the_program_goes_on(monkeypatch: pytest.MonkeyPatch,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    said: list[str] = []
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    errors.keep_running(said.append)
    try:
        raise ZeroDivisionError("boom")
    except ZeroDivisionError:
        sys.excepthook(*sys.exc_info())
    assert said and "ZeroDivisionError" in said[0] and "nothing was closed" in said[0]
    assert "Unexpected error" in caplog.text
    errors._notify.clear()


def test_a_slot_that_raises_doesnt_end_the_window() -> None:
    code = textwrap.dedent("""
        from PyQt6.QtCore import QCoreApplication, QTimer
        from pythonator import errors
        app = QCoreApplication([])
        errors.keep_running()
        QTimer.singleShot(50, lambda: 1 / 0)
        QTimer.singleShot(300, lambda: (print("ALIVE", flush=True), app.quit()))
        app.exec()
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert "ALIVE" in done.stdout, done.stderr[-2000:]


def test_file_tree_doesnt_list_generated_folders(qtbot: Any, tmp_path: Path) -> None:
    from pythonator.ui.editor import NOT_LISTED, FileTree

    for name in ("node_modules", ".venv", "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.js").write_text("")
    tree = FileTree()
    qtbot.addWidget(tree)
    tree.set_root(str(tmp_path))
    qtbot.waitUntil(lambda: tree.model.rowCount(tree.view.rootIndex()) == 3, timeout=5000)
    model = tree.model
    assert {"node_modules", ".venv"} <= NOT_LISTED
    assert not model.hasChildren(model.index(str(tmp_path / "node_modules")))
    assert not model.canFetchMore(model.index(str(tmp_path / ".venv")))
    assert model.hasChildren(model.index(str(tmp_path / "src")))


def test_system_check_wraps_long_paths_and_hides_the_home_folder() -> None:
    from pythonator.ui import about

    home = str(Path.home())
    path = home + r"\AppData\Local\Microsoft\WinGet\Packages\DenoLand.Deno_8wekyb3d8bbwe\deno.exe"
    shown = about._private(path)
    assert shown.startswith("~") and home not in shown
    assert about._breakable(r"C:\a\b") == "C:\\\u200ba\\\u200bb"
    assert about._breakable("a / b") == "a / b"  # only inside a path
    if shutil.which("true"):
        assert about._private("nothing to hide") == "nothing to hide"


# -- Ports: taken, reserved, IPv6 only ------------------------------------------------------------------------------


def test_a_port_in_use_is_named_with_a_free_one_to_use_instead() -> None:
    import socket

    from pythonator.core import envcheck

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        text = envcheck.port_problem(port)
        assert f"Port {port} is already in use" in text
        near = envcheck.free_port_near(port)
        assert near > port and f"Port {near} is free" in text
    assert envcheck.port_problem(near) == ""


def test_a_service_listening_on_ipv6_only_or_beside_another_program_gets_a_note(
        dirs: DataDirs, tmp_path: Path) -> None:
    from pythonator.daemon.supervisor import State, Stats

    sup = Supervisor(dirs, Store(dirs))
    try:
        service = Service.model_validate({"name": "deno", "folder": str(tmp_path), "runtime": "deno",
                                          "entry": "main.ts", "network": {"enabled": True, "ports": [8000]}})
        sup.add(service)
        slot = sup.slots[service.id]
        notes: list[str] = []
        sup.note = lambda _slot, text: notes.append(text)  # type: ignore[method-assign,assignment]
        slot.state, slot.runner, slot.started_wall = State.RUNNING, SimpleNamespace(pid=1), time.time()  # type: ignore[assignment]
        slot.stats = Stats(ports=[8000], addresses={8000: ["::1"]}, others={8000: [os.getpid()]})
        sup._check_listening(slot)
        assert any("IPv6 only" in n and "127.0.0.1" in n for n in notes), notes
        assert any("also listens on port 8000" in n for n in notes), notes
        notes.clear()
        slot.listen_checked = False
        slot.stats = Stats(ports=[8000], addresses={8000: ["127.0.0.1"]})
        sup._check_listening(slot)
        assert notes == []
    finally:
        sup.close()


def test_tree_stats_see_other_programs_on_its_port() -> None:
    import socket

    from pythonator.daemon import supervisor

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        conns = supervisor._connections()
        if conns is None:
            pytest.skip("this system lists other programs' connections only to root")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            st = supervisor._tree_stats(child.pid, {}, ports=True, conns=conns, wanted=[port])
        finally:
            child.kill()
            child.wait()
        assert st.others.get(port) == [os.getpid()]
