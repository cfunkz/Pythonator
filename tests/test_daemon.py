"""The background service end to end: authentication, starting and stopping, restarts, Full-access consent."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtNetwork import QLocalSocket

from pythonator.daemon.server import socket_name
from pythonator.paths import IS_WINDOWS, DataDirs
from tests.conftest import DaemonHarness


def raw_socket(dirs: DataDirs, qtbot: Any) -> QLocalSocket:
    sock = QLocalSocket()
    sock.connectToServer(socket_name(dirs))
    assert sock.waitForConnected(2000)
    return sock


def test_wrong_token_is_dropped(harness: DaemonHarness, dirs: DataDirs, qtbot: Any) -> None:
    sock = raw_socket(dirs, qtbot)
    sock.write(b'{"op": "hello", "req": 1, "token": "guess"}\n')
    sock.flush()
    qtbot.waitUntil(lambda: sock.state() == QLocalSocket.LocalSocketState.UnconnectedState, timeout=3000)
    assert b"snapshot" not in bytes(sock.readAll().data())


def test_requests_before_hello_are_dropped(harness: DaemonHarness, dirs: DataDirs, qtbot: Any) -> None:
    sock = raw_socket(dirs, qtbot)
    sock.write(b'{"op": "snapshot", "req": 1}\n')
    sock.flush()
    qtbot.waitUntil(lambda: sock.state() == QLocalSocket.LocalSocketState.UnconnectedState, timeout=3000)


def test_token_file_is_private(harness: DaemonHarness, dirs: DataDirs) -> None:
    if not IS_WINDOWS:  # Windows has no mode bits; the per-user profile folder's ACL keeps it private
        assert dirs.token_file.stat().st_mode & 0o077 == 0


def test_bad_requests_get_an_error_not_a_crash(harness: DaemonHarness) -> None:
    for message in ({"op": "start", "id": "../../etc"}, {"op": "add", "service": {"name": "\x00"}},
                    {"op": "nope"}, {"op": "start", "id": "abc", "extra": 1}):
        ok, _data, err = harness.call(message)
        assert not ok and err
    assert harness.ok({"op": "ping"}) == "pong"


def test_start_output_stop(harness: DaemonHarness, project: Path) -> None:
    (project / "loop.py").write_text("import time\nprint('hello from the sandbox', flush=True)\n"
                                     "while True: time.sleep(0.2)\n")
    sid = harness.add(name="loop", folder=str(project), entry="loop.py", restart="never")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "hello from the sandbox" in harness.output(sid))
    assert "running" in harness.states(sid)
    harness.ok({"op": "stop", "id": sid})
    harness.wait(lambda: harness.states(sid)[-1] == "stopped")


def test_input_reaches_the_program(harness: DaemonHarness, project: Path) -> None:
    (project / "echo.py").write_text("import sys\nfor line in sys.stdin:\n    print('got', line.strip(), flush=True)\n")
    sid = harness.add(name="echo", folder=str(project), entry="echo.py", restart="never")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "running" in harness.states(sid))
    harness.ok({"op": "input", "id": sid, "text": "ping"})
    harness.wait(lambda: "got ping" in harness.output(sid))


def test_crash_loop_gives_up(harness: DaemonHarness, project: Path) -> None:
    (project / "crash.py").write_text("raise SystemExit(3)\n")
    sid = harness.add(name="crash", folder=str(project), entry="crash.py", restart="on-failure", max_restarts=2)
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: harness.states(sid)[-1:] == ["failed"] and harness.states(sid).count("starting") >= 3,
                 timeout=20_000)
    assert "waiting" in harness.states(sid)  # it backed off between attempts


def test_full_access_needs_consent_once(harness: DaemonHarness, project: Path) -> None:
    (project / "hi.py").write_text("print('hi')\n")
    sid = harness.add(name="full", folder=str(project), entry="hi.py", mode="full", restart="never")
    ok, _data, err = harness.call({"op": "start", "id": sid})
    assert not ok and err == "confirm_full"
    harness.ok({"op": "start", "id": sid, "confirmed": True})
    harness.wait(lambda: "hi" in harness.output(sid))


def test_services_survive_a_restart_of_the_daemon(harness: DaemonHarness, dirs: DataDirs, project: Path) -> None:
    harness.add(name="kept", folder=str(project), entry="x.py")
    data = json.loads(dirs.services_file.read_text())
    assert [s["name"] for s in data["services"]] == ["kept"]


@pytest.mark.skipif(IS_WINDOWS, reason="Windows uses a named pipe")
def test_deep_data_folder_still_gets_a_socket(tmp_path: Path, qtbot: Any) -> None:
    from pythonator.daemon.server import SUN_PATH, Daemon

    deep = DataDirs(tmp_path / ("d" * 60) / ("e" * 60) / "data")
    name = socket_name(deep)
    assert len(os.fsencode(name)) <= SUN_PATH
    assert Path(name).parent.stat().st_mode & 0o077 == 0
    daemon = Daemon(deep)
    try:
        assert daemon.listen()
        sock = QLocalSocket()
        sock.connectToServer(name)
        assert sock.waitForConnected(2000)
        sock.abort()
    finally:
        daemon.close()


def test_shutdown_does_not_start_it_again(harness: DaemonHarness, qtbot: Any) -> None:
    client = harness.client
    with qtbot.waitSignal(client.disconnected, timeout=5000):
        client.shutdown(stop_services=True)
    qtbot.wait(1500)  # longer than the reconnect delay
    assert not client.ready
    assert not client._spawned
    assert not client._retry.isActive()


STUBBORN = """
import signal, time
for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
print("up", flush=True)
while True:
    time.sleep(0.2)
"""


def test_window_start_all_and_stop_all(harness: DaemonHarness, dirs: DataDirs, project: Path, qtbot: Any) -> None:
    """The window's Start all and Stop all return at once; Stop all again kills what is still stopping."""
    from pythonator.ui.main_window import MainWindow

    (project / "a.py").write_text("import time\nprint('a', flush=True)\nwhile True:\n    time.sleep(0.2)\n")
    (project / "b.py").write_text(STUBBORN)
    a = harness.add(name="a", folder=str(project), entry="a.py", restart="never")
    b = harness.add(name="b", folder=str(project), entry="b.py", restart="never", stop_timeout=60)
    window = MainWindow(dirs)
    try:
        qtbot.waitUntil(lambda: window.client.ready and len(window.services) == 2, timeout=10_000)
        window.act_start_all.trigger()
        # One after another; the first also makes the folder's .venv (slow in a Windows AppContainer).
        qtbot.waitUntil(lambda: all(window.states.get(s, {}).get("state") == "running" for s in (a, b)),
                        timeout=90_000)
        qtbot.waitUntil(lambda: "up" in harness.output(b), timeout=20_000)
        assert window.sb_running.text() == "2 of 2 running"
        window.act_stop_all.trigger()
        qtbot.waitUntil(lambda: window.states[a].get("state") == "stopped", timeout=20_000)
        if not IS_WINDOWS:  # Windows may have no console to send Ctrl+C to, and then ends it at once
            assert window.states[b].get("state") == "stopping"
        window.act_stop_all.trigger()  # b ignores the polite stop and has 60 s of grace: this kills it now
        qtbot.waitUntil(lambda: window.states[b].get("state") in ("stopped", "failed"), timeout=10_000)
    finally:
        window.client.close()
        window.hide()
        window.deleteLater()


def test_start_all_starts_in_order_a_few_at_a_time(harness: DaemonHarness, project: Path) -> None:
    """Start all queues the services in list order and starts "start at once" of them together; reorder changes
    that order."""
    (project / "s.py").write_text("import time\nprint('up', flush=True)\nwhile True:\n    time.sleep(0.2)\n")
    sids = [harness.add(name=f"s{i}", folder=str(project), entry="s.py", restart="never") for i in range(3)]
    harness.ok({"op": "reorder", "ids": [sids[2], sids[0], sids[1]]})
    snap = harness.ok({"op": "snapshot"})
    assert [s["id"] for s in snap["services"]] == [sids[2], sids[0], sids[1]]
    harness.ok({"op": "settings", "settings": {**snap["settings"], "start_at_once": 1}})
    harness.ok({"op": "start_all"})
    harness.wait(lambda: all("running" in harness.states(s) for s in sids), timeout=30_000)
    started = [e["id"] for e in harness.events if e.get("ev") == "state" and e.get("state") == "starting"]
    assert started[:3] == [sids[2], sids[0], sids[1]]  # one after another, in the new order
    harness.ok({"op": "stop_all"})
    harness.wait(lambda: all(harness.states(s)[-1] == "stopped" for s in sids), timeout=20_000)


def test_services_sharing_a_folder_make_its_environment_once(harness: DaemonHarness, project: Path) -> None:
    """All at once, two services of one folder: one makes its .venv, the other waits for it, then both run."""
    (project / "s.py").write_text("import time\nprint('up', flush=True)\nwhile True:\n    time.sleep(0.2)\n")
    sids = [harness.add(name=f"w{i}", folder=str(project), entry="s.py", restart="never") for i in range(2)]
    snap = harness.ok({"op": "snapshot"})
    harness.ok({"op": "settings", "settings": {**snap["settings"], "start_at_once": 0}})
    harness.ok({"op": "start_all", "ids": sids})
    harness.wait(lambda: all("up" in harness.output(s) for s in sids), timeout=120_000)
    out = harness.output(sids[0]) + harness.output(sids[1])
    assert out.count("Making its own environment first") == 1
    assert "which is making this folder's environment" in out
    harness.ok({"op": "stop_all"})
    harness.wait(lambda: all(harness.states(s)[-1] == "stopped" for s in sids), timeout=20_000)


def test_installer_shutdown_stops_the_background_service(dirs: DataDirs, project: Path) -> None:
    """What the Windows installer runs before an update (and the uninstaller with --uninstall)."""
    import subprocess
    import time

    from pythonator.paths import self_command

    env = {**os.environ, "PYTHONATOR_HOME": str(dirs.root)}
    daemon = subprocess.Popen(self_command("--daemon"), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not dirs.token_file.is_file():
            assert daemon.poll() is None and time.monotonic() < deadline, "the background service didn't start"
            time.sleep(0.1)
        time.sleep(0.3)
        done = subprocess.run([*self_command("shutdown")], env=env, timeout=90, check=False)
        assert done.returncode == 0
        assert daemon.wait(30) == 0
        assert not dirs.token_file.exists()
        # Nothing running: it returns at once, and never starts a window or a service.
        again = subprocess.run([*self_command("--uninstall")], env=env, timeout=30, check=False)
        assert again.returncode == 0
    finally:
        if daemon.poll() is None:
            daemon.kill()


def test_floods_keep_everything_in_the_log_and_stop_fast(harness: DaemonHarness, dirs: DataDirs,
                                                         project: Path) -> None:
    """A program that writes as fast as it can loses nothing in its log, and still stops at once."""
    import time

    (project / "burst.py").write_text("for i in range(200_000):\n    print('line', i)\nprint('THE END')\n")
    sid = harness.add(name="burst", folder=str(project), entry="burst.py", restart="never")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: harness.states(sid)[-1:] == ["stopped"], timeout=60_000)
    log = (dirs.logs / f"{sid}.log").read_text(encoding="utf-8")
    assert "line 199999\nTHE END" in log
    (project / "flood.py").write_text("while True:\n    print('x' * 200)\n")
    fid = harness.add(name="flood", folder=str(project), entry="flood.py", restart="never")
    harness.ok({"op": "start", "id": fid})
    harness.wait(lambda: "running" in harness.states(fid))
    time.sleep(1.5)  # let it flood
    began = time.monotonic()
    harness.ok({"op": "stop", "id": fid})
    harness.wait(lambda: harness.states(fid)[-1:] == ["stopped"], timeout=10_000)
    assert time.monotonic() - began < 5, "a stop must not wait behind a backlog of output"


BUSY = r'''
import subprocess, sys
kids = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(2)]
print("BUSY", flush=True)
while True:
    pass
'''


def test_cpu_limit_holds_a_busy_tree(tmp_path: Path) -> None:
    """Three busy processes held to half of one core together (Windows: the job's hard cap; elsewhere: the
    throttle), and released for a quick stop."""
    import queue
    import sys
    import time

    import psutil

    from pythonator.core import limits
    from pythonator.daemon.runner import Runner
    from pythonator.isolation import Command

    if limits.cores() < 2:
        pytest.skip("needs two cores to show a limit")
    runner = Runner("busy", Command([sys.executable, "-c", BUSY], tmp_path, dict(os.environ)), queue.SimpleQueue(),
                    cpu_percent=50)
    runner.start()
    try:
        time.sleep(2.0)  # the children start, the throttle measures and settles

        def used() -> float:
            total = 0.0
            for p in runner.tree():
                try:
                    t = p.cpu_times()
                    total += t.user + t.system
                except psutil.Error:
                    pass
            return total

        assert len(runner.tree()) >= 3
        before, began = used(), time.monotonic()
        time.sleep(4.0)
        percent = (used() - before) / (time.monotonic() - began) * 100
        assert percent < 90, f"three busy processes used {percent:.0f}% of one core with a 50% limit"
        assert percent > 10, f"held far too low: {percent:.0f}%"
    finally:
        began = time.monotonic()
        runner.stop(5)
        assert runner.proc is not None
        runner.proc.wait(10)
        assert time.monotonic() - began < 4, "a throttled service must still stop at once"
        runner.kill()


def test_limits_are_named_at_start(harness: DaemonHarness, dirs: DataDirs, project: Path) -> None:
    (project / "idle.py").write_text("import time\nprint('up', flush=True)\ntime.sleep(60)\n")
    sid = harness.add(name="idle", folder=str(project), entry="idle.py", memory_mb=256, cpu_percent=50,
                      mode="full", full_access_ok=True)
    harness.ok({"op": "start", "id": sid, "confirmed": True})
    harness.wait(lambda: "running" in harness.states(sid))
    harness.ok({"op": "stop", "id": sid})
    harness.wait(lambda: harness.states(sid)[-1:] == ["stopped"], timeout=10_000)
    log = (dirs.logs / f"{sid}.log").read_text(encoding="utf-8")
    assert "Limits: memory 256 MB" in log and "CPU 0.5 cores (50% of one core)" in log, log


def test_own_environment_is_made_on_start_and_recreated(harness: DaemonHarness, project: Path) -> None:
    (project / "where.py").write_text("import sys\nprint('prefix', sys.prefix, flush=True)\n")
    sid = harness.add(name="env", folder=str(project), entry="where.py", restart="never")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "prefix" in harness.output(sid), timeout=120_000)
    assert (project / ".venv" / "pyvenv.cfg").is_file()
    assert ".venv" in harness.output(sid).split("prefix", 1)[1]  # never the system Python
    harness.wait(lambda: harness.states(sid)[-1] in ("stopped", "failed"))
    marker = project / ".venv" / "old-marker"
    marker.write_text("x")
    harness.ok({"op": "recreate", "id": sid})
    harness.wait(lambda: "Deleted .venv" in harness.output(sid), timeout=30_000)
    harness.wait(lambda: harness.states(sid)[-1] == "stopped", timeout=120_000)
    assert (project / ".venv" / "pyvenv.cfg").is_file() and not marker.exists()


def test_own_environment_installs_requirements_in_the_sandbox(harness: DaemonHarness, project: Path) -> None:
    """The setup steps (venv, pip, requirements) write .venv inside the project even in Sandbox mode, where the
    venv's own interpreter is otherwise only read."""
    (project / "where.py").write_text("import sys\nprint('prefix', sys.prefix, flush=True)\n")
    (project / "requirements.txt").write_text("")  # an empty list: pip runs, offline is fine
    sid = harness.add(name="env-sandbox", folder=str(project), entry="where.py", restart="never", mode="sandbox")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "prefix" in harness.output(sid) or harness.states(sid)[-1:] == ["failed"],
                 timeout=180_000)
    assert "prefix" in harness.output(sid), harness.output(sid)
    assert any((project / ".venv").glob("lib*/python*/site-packages/pip")) or \
        (project / ".venv" / "Lib" / "site-packages" / "pip").is_dir()


def test_own_environment_off_uses_the_runtime_as_is(harness: DaemonHarness, project: Path) -> None:
    (project / "hi.py").write_text("print('hi there', flush=True)\n")
    sid = harness.add(name="global", folder=str(project), entry="hi.py", restart="never", own_env=False)
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "hi there" in harness.output(sid))
    assert not (project / ".venv").exists()


def test_console_shell_runs_next_to_the_service_in_its_environment(harness: DaemonHarness, project: Path) -> None:
    """The console's shell: a command in the service's folder, while the service runs, with its own output."""
    (project / "s.py").write_text("import time\nprint('up', flush=True)\nwhile True:\n    time.sleep(0.2)\n")
    sid = harness.add(name="sh", folder=str(project), entry="s.py", restart="never", own_env=False)
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "up" in harness.output(sid))
    harness.ok({"op": "shell", "id": sid, "line": "echo from-the-shell"})
    harness.wait(lambda: "$ done (exit code 0)" in harness.output(sid))
    out = harness.output(sid)
    assert "$ echo from-the-shell" in out and "from-the-shell" in out.split("$ echo from-the-shell", 1)[1]
    assert harness.states(sid)[-1] == "running"  # the service itself goes on
    harness.ok({"op": "stop", "id": sid})
    harness.wait(lambda: harness.states(sid)[-1] == "stopped", timeout=20_000)


def test_uninstall_ends_only_the_installed_apps_own_processes() -> None:
    """From source, sys.executable is a Python: the uninstaller's clean-up must never end other programs."""
    from pythonator.daemon import installer

    assert installer.end_strays() == []
