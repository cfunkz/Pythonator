from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from pythonator.core.launch import LaunchSpec, base_environment
from pythonator.core.runner import ExitInfo, ProcessRunner

from .conftest import wait_until


class Collector:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.exits: list[ExitInfo] = []
        self.done = threading.Event()

    def out(self, text: str) -> None:
        self.chunks.append(text)

    def exit(self, info: ExitInfo) -> None:
        self.exits.append(info)
        self.done.set()

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def is_gone(pid: int) -> bool:
    """True once ``pid`` no longer runs (exited, reaped or a zombie awaiting its parent)."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def run(code: str, tmp_path: Path) -> tuple[ProcessRunner, Collector]:
    c = Collector()
    env = base_environment()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    r = ProcessRunner(LaunchSpec([sys.executable, "-u", "-c", code], tmp_path, env), c.out, c.exit)
    r.start()
    return r, c


def test_output_and_exit_code(tmp_path: Path) -> None:
    r, c = run("import sys; print('out'); print('err ✓', file=sys.stderr); sys.exit(3)", tmp_path)
    assert c.done.wait(10)
    assert "out" in c.text and "err ✓" in c.text
    assert c.exits[0].code == 3 and not c.exits[0].requested and not c.exits[0].success
    assert r.wait(1)


def test_stdin(tmp_path: Path) -> None:
    r, c = run("name = input('name? '); print('hello', name)", tmp_path)
    assert wait_until(lambda: "name?" in c.text)
    assert r.send("bob\n")
    assert c.done.wait(10)
    assert "hello bob" in c.text
    assert not r.send("late\n")


def test_stop_kills_process_tree(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('child', child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    r, c = run(code, tmp_path)
    assert wait_until(lambda: "child" in c.text)
    child_pid = int(c.text.split()[1])
    started = time.monotonic()
    r.stop(grace=5)
    assert c.done.wait(10)
    assert c.exits[0].requested
    assert time.monotonic() - started < 4  # graceful signal, not the 5s timeout
    assert wait_until(lambda: is_gone(child_pid), 5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_stop_escalates_to_kill(tmp_path: Path) -> None:
    r, c = run("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready')\n"
               "time.sleep(60)", tmp_path)
    assert wait_until(lambda: "ready" in c.text)
    started = time.monotonic()
    r.stop(grace=0.5)
    assert c.done.wait(10)
    assert 0.4 < time.monotonic() - started < 5
    assert c.exits[0].code == -9


def test_orphans_are_reaped_after_exit(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; print(\"orphan up\", flush=True); "
        "time.sleep(60)'])\n"
        "print('parent', child.pid, flush=True)\n"
        "import time; time.sleep(0.5)\n"
    )
    _r, c = run(code, tmp_path)
    assert c.done.wait(15)
    child_pid = int(c.text.split("parent")[1].split()[0])
    assert wait_until(lambda: is_gone(child_pid), 5)


def test_bad_executable(tmp_path: Path) -> None:
    r = ProcessRunner(LaunchSpec([str(tmp_path / "missing")], tmp_path, base_environment()),
                      lambda _t: None, lambda _i: None)
    with pytest.raises(OSError):
        r.start()
    assert not r.running
