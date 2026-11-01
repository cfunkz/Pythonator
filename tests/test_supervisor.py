from __future__ import annotations

import sys
from pathlib import Path

from pythonator.core.models import LaunchMode, RestartPolicy, ServiceConfig
from pythonator.core.paths import DataPaths
from pythonator.supervisor import Status, Supervisor

from .conftest import wait_until


def make(tmp_path: Path, qapp: object) -> Supervisor:
    return Supervisor(DataPaths(tmp_path / "data"))


def script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def all_text(sup: Supervisor, sid: str) -> str:
    svc = sup.get(sid)
    assert svc is not None
    return "\n".join(ln.text for ln in svc.log.snapshot())


def test_start_exit_and_persist(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="hello", target=script(tmp_path, "a.py", "print('hi there')"),
                                python=sys.executable, restart=RestartPolicy.NEVER))
    assert sup.start(svc.id)
    assert svc.status is Status.RUNNING
    assert wait_until(lambda: svc.status is Status.EXITED, qt=True)
    assert "hi there" in all_text(sup, svc.id)
    assert "Exited normally" in all_text(sup, svc.id)
    sup.shutdown()
    again = Supervisor(DataPaths(tmp_path / "data"))
    assert [s.name for s in again.services()] == ["hello"]
    again.shutdown()


def test_crash_restarts_with_backoff(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="crashy", target=script(tmp_path, "c.py", "raise SystemExit(2)"),
                                python=sys.executable, restart=RestartPolicy.ON_FAILURE))
    sup.start(svc.id)
    assert wait_until(lambda: svc.restarts >= 2, timeout=15, qt=True)
    assert svc.failures >= 2
    sup.stop(svc.id)  # cancels a pending restart or stops the current run
    assert wait_until(lambda: svc.status in (Status.STOPPED,), timeout=10, qt=True)
    sup.shutdown()


def test_stop_restart_and_stdin(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    body = "import sys\nfor line in sys.stdin:\n    print('echo:', line.strip(), flush=True)\n"
    svc = sup.add(ServiceConfig(name="echo", target=script(tmp_path, "e.py", body), python=sys.executable))
    sup.start(svc.id)
    first_pid = svc.pid
    assert sup.send_input(svc.id, "ping")
    assert wait_until(lambda: "echo: ping" in all_text(sup, svc.id), qt=True)
    sup.restart(svc.id)
    assert wait_until(lambda: svc.status is Status.RUNNING and svc.pid not in (0, first_pid), qt=True)
    sup.stop(svc.id)
    assert wait_until(lambda: svc.status is Status.STOPPED, qt=True)
    assert not sup.send_input(svc.id, "nobody home")
    sup.shutdown()


def test_launch_error_is_reported(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="broken", target=str(tmp_path / "missing.py")))
    assert not sup.start(svc.id)
    assert svc.status is Status.FAILED
    assert "Script not found" in all_text(sup, svc.id)
    sup.shutdown()


def test_shell_task_and_rename(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="env", mode=LaunchMode.MODULE, target="http.server",
                                workdir=str(tmp_path), env={"GREETING": "hey"}))
    command = "echo $env:GREETING" if sys.platform == "win32" else "echo $GREETING"  # PowerShell on Windows
    assert sup.run_shell(svc.id, command)
    assert wait_until(lambda: svc.task is None and "hey" in all_text(sup, svc.id), qt=True)
    old_log = svc.log.path
    sup.update(svc.config.copy(name="renamed"))
    assert svc.log.path != old_log and "renamed" in svc.log.path.name
    assert svc.log.path.exists() and not old_log.exists()
    sup.remove(svc.id, delete_logs=True)
    assert sup.services() == []
    sup.shutdown()


def _shell_python(tmp_path: Path, name: str, body: str) -> str:
    path = script(tmp_path, name, body)
    quote = '"' if sys.platform == "win32" else "'"
    return f"{quote}{sys.executable}{quote} -u {quote}{path}{quote}"


def test_stop_ends_a_shell_command_even_when_the_service_is_not_running(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="svc", target=script(tmp_path, "main.py", "print(1)"), python=sys.executable))
    server = _shell_python(tmp_path, "server.py", "import time\nprint('serving', flush=True)\ntime.sleep(120)")
    assert sup.run_shell(svc.id, server)
    assert wait_until(lambda: "serving" in all_text(sup, svc.id), qt=True)
    assert not sup.run_shell(svc.id, "echo second")
    assert "Press Stop" in all_text(sup, svc.id)
    sup.stop(svc.id)
    assert wait_until(lambda: svc.task is None, timeout=15, qt=True), all_text(sup, svc.id)
    assert "cancelled" in all_text(sup, svc.id)
    assert sup.run_shell(svc.id, "echo free-again")
    assert wait_until(lambda: svc.task is None and "free-again" in all_text(sup, svc.id), qt=True)
    sup.shutdown()


def test_second_stop_kills_a_shell_command_that_ignores_the_first(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="svc", target=script(tmp_path, "main.py", "print(1)"), python=sys.executable))
    body = ("import signal, time\n"
            "for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):\n"
            "    if hasattr(signal, name):\n"
            "        signal.signal(getattr(signal, name), lambda *a: print('ignored', flush=True))\n"
            "print('stubborn', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.1)\n")
    assert sup.run_shell(svc.id, _shell_python(tmp_path, "stubborn.py", body))
    assert wait_until(lambda: "stubborn" in all_text(sup, svc.id), qt=True)
    sup.stop(svc.id)
    assert svc.task is not None and svc.task.stop_requested
    sup.stop(svc.id)
    assert wait_until(lambda: svc.task is None, timeout=10, qt=True), all_text(sup, svc.id)
    assert "Killing" in all_text(sup, svc.id)
    sup.shutdown()


def test_restart_leaves_a_running_task_alone(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="svc", target=script(tmp_path, "main.py", "import time\ntime.sleep(60)"),
                                python=sys.executable))
    assert sup.start(svc.id)
    assert sup.run_shell(svc.id, _shell_python(tmp_path, "long.py", "import time\ntime.sleep(60)"))
    task = svc.task
    sup.restart(svc.id)
    assert wait_until(lambda: all_text(sup, svc.id).count("Started") >= 2 and svc.status is Status.RUNNING,
                      timeout=20, qt=True), all_text(sup, svc.id)
    assert svc.task is task and task is not None and not task.stop_requested
    sup.shutdown()


def test_shutdown_stops_everything(tmp_path: Path, qapp: object) -> None:
    sup = make(tmp_path, qapp)
    svc = sup.add(ServiceConfig(name="sleepy", target=script(tmp_path, "s.py", "import time\ntime.sleep(60)"),
                                python=sys.executable))
    sup.start(svc.id)
    runner = svc.runner
    assert runner is not None
    sup.shutdown(timeout=3)
    assert not runner.running
    assert svc.status is Status.STOPPED
