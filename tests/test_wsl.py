"""WSL mode and Docker inside WSL (Windows). On any system, through a stand-in for wsl.exe that runs the Linux
side here: the command it gets, what crosses into Linux, and that Stop ends the whole run by its mark."""

from __future__ import annotations

import os
import queue
import stat
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from pythonator import isolation
from pythonator.core import runtimes
from pythonator.core.models import Mode, Service
from pythonator.daemon.runner import Exited, Runner
from pythonator.isolation import IsolationError, container, wsl
from pythonator.isolation.base import WslRun
from pythonator.paths import IS_WINDOWS, DataDirs

POSIX = pytest.mark.skipif(IS_WINDOWS, reason="the stand-in for wsl.exe runs the Linux side here")

# wsl.exe as far as Pythonator uses it: --distribution, --list --quiet (UTF-16, as WSL writes it), --cd, --exec.
# Like WSL, only the variables named in WSLENV reach the Linux side.
FAKE_WSL = """\
import os, sys
args = sys.argv[1:]
distro = ""
if args[:1] == ["--distribution"]:
    distro, args = args[1], args[2:]
if distro == "Missing":
    sys.stderr.buffer.write("There is no distribution with the supplied name.\\r\\n".encode("utf-16-le"))
    sys.exit(1)
if args == ["--list", "--quiet"]:
    sys.stdout.buffer.write("Ubuntu-24.04\\r\\nDebian\\r\\nbad name!\\r\\n".encode("utf-16-le"))
    sys.exit(0)
if args[:1] == ["--cd"]:
    os.chdir(args[1])
    args = args[2:]
assert args[0] == "--exec", args
env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("TEMP", "/tmp"), "WSL_DISTRO_NAME": distro or "Ubuntu"}
for item in os.environ.get("WSLENV", "").split(":"):
    name = item.split("/")[0]
    if name in os.environ:
        env[name] = os.environ[name]
os.execvpe(args[1], args[1:], env)
"""


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def fake_wsl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """wsl.exe and wslpath stand-ins; Pythonator thinks it runs on Windows."""
    monkeypatch.setattr(wsl, "_places", {})
    tools = tmp_path / "tools"
    tools.mkdir()
    exe = _script(tools / "wsl.exe", f"#!{sys.executable}\n{FAKE_WSL}")
    _script(tools / "wslpath", '#!/bin/sh\n[ "$1" = -a ] && [ "$2" = -u ] && printf "%s\\n" "$3"\n')
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(wsl, "IS_WINDOWS", True)
    monkeypatch.setattr(wsl, "program", lambda: str(exe))
    return exe


def wsl_service(folder: Path, **kw: Any) -> Service:
    data: dict[str, Any] = {"id": "abc123", "name": "bot", "folder": str(folder), "runtime": "python",
                            "entry": "bot.py", "mode": "wsl", **kw}
    return Service.model_validate(data)


def test_wsl_mode_is_not_isolated_and_runs_linux() -> None:
    assert Mode.WSL.runs_as_you and Mode.FULL.runs_as_you
    assert not Mode.SANDBOX.runs_as_you and not Mode.CONTAINER.runs_as_you
    assert Mode.WSL.linux_inside and Mode.CONTAINER.linux_inside and not Mode.FULL.linux_inside


def test_distribution_names_are_checked(tmp_path: Path) -> None:
    assert wsl_service(tmp_path, wsl={"distro": "Ubuntu-24.04"}).wsl.distro == "Ubuntu-24.04"
    assert wsl_service(tmp_path).wsl.distro == ""
    for bad in ("-d evil", "a b", "x;rm", "../x", "--exec"):
        with pytest.raises(ValueError, match="WSL distribution"):
            wsl_service(tmp_path, wsl={"distro": bad})


def test_wsl_output_is_read_in_both_encodings() -> None:
    assert wsl.decode("Ubuntu\r\n".encode("utf-16-le")) == "Ubuntu\r\n"
    assert wsl.decode("\ufeffDebian".encode("utf-16-le")) == "Debian"
    assert wsl.decode(b"Ubuntu\n") == "Ubuntu\n"


def test_wsl_mode_is_for_windows_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wsl, "IS_WINDOWS", False)
    with pytest.raises(IsolationError, match="for Windows"):
        wsl.wrap(wsl_service(tmp_path))
    assert wsl.distros() == []


@POSIX
def test_distributions_are_listed_without_bad_names(fake_wsl: Path) -> None:
    assert wsl.distros() == ["Ubuntu-24.04", "Debian"]


@POSIX
def test_limits_and_no_network_are_refused_not_ignored(fake_wsl: Path, tmp_path: Path) -> None:
    with pytest.raises(IsolationError, match="RAM or CPU limits"):
        wsl.wrap(wsl_service(tmp_path, memory_mb=512))
    with pytest.raises(IsolationError, match="RAM or CPU limits"):
        wsl.wrap(wsl_service(tmp_path, cpu_percent=50))
    with pytest.raises(IsolationError, match="network off"):
        wsl.wrap(wsl_service(tmp_path, network={"enabled": False}))
    with pytest.raises(IsolationError, match="no distribution"):
        wsl.wrap(wsl_service(tmp_path, wsl={"distro": "Missing"}))
    # an install step isn't the service: it may run with the service's limits set
    assert wsl.wrap(wsl_service(tmp_path, memory_mb=512), task=["python3", "-m", "venv", ".venv-wsl"])


@POSIX
def test_the_command_and_what_crosses_into_linux(fake_wsl: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "my bot"
    folder.mkdir()
    (folder / "bot.py").write_text("print('hi')\n")
    (folder / ".env").write_text("API_TOKEN=from-env-file\n")
    monkeypatch.setenv("GITHUB_TOKEN", "yours")  # Windows' own variables stay on Windows
    service = wsl_service(folder, wsl={"distro": "Ubuntu-24.04"}, env_file=".env", env={"MODE": "prod"},
                          network={"ports": [8000]}, args=["--verbose"])
    cmd = isolation.prepare(service, DataDirs(tmp_path / "data"))
    assert cmd.argv[:6] == [str(fake_wsl), "--distribution", "Ubuntu-24.04", "--cd", str(folder), "--exec"]
    assert cmd.argv[6:9] == ["/bin/sh", "-lc", 'exec "$0" "$@"']
    assert cmd.shown == ["python3", "-u", f"{folder}/bot.py", "--verbose"] == cmd.argv[9:]
    assert cmd.wsl is not None and cmd.wsl.distro == "Ubuntu-24.04" and len(cmd.wsl.token) == 16
    assert cmd.env[wsl.MARK] == cmd.wsl.token
    assert cmd.env["API_TOKEN"] == "from-env-file" and cmd.env["MODE"] == "prod"
    assert (cmd.env["PORT"], cmd.env["HOST"]) == ("8000", "127.0.0.1")
    named = cmd.env["WSLENV"].split(":")
    assert {"API_TOKEN/u", "MODE/u", "PORT/u", f"{wsl.MARK}/u"} <= set(named)
    assert "GITHUB_TOKEN" not in cmd.env and "GITHUB_TOKEN/u" not in named
    assert not any("from-env-file" in a for a in cmd.argv), "values never go on the command line"
    assert "Windows files under /mnt" in cmd.describe and "serves 8000 for this computer" in cmd.describe


@POSIX
def test_python_runs_wsls_own_python_and_its_own_venv(fake_wsl: Path, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\n")
    service = wsl_service(tmp_path)
    assert wsl.install_steps(service) == [
        ["python3", "-m", "venv", "./.venv-wsl"],
        ["./.venv-wsl/bin/python", "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"]]
    (tmp_path / ".venv-wsl").mkdir()
    assert wsl.wrap(service).shown[0] == f"{tmp_path}/.venv-wsl/bin/python"
    assert runtimes.env_folders("python", tmp_path, wsl=True) == [tmp_path / ".venv-wsl"]


def test_programs_found_on_windows_run_by_their_linux_name(tmp_path: Path) -> None:
    assert container.linux_name(r"C:\Program Files\nodejs\npm.cmd") == "npm"
    assert container.linux_name(r"C:\Users\me\AppData\Local\pnpm\pnpm.EXE") == "pnpm"
    assert container.linux_name("/usr/bin/npm") == "npm"
    assert container.linux_name("node") == "node"
    node = Service.model_validate({"name": "web", "folder": str(tmp_path), "runtime": "node", "mode": "container"})
    assert container.linux_argv(node, None)[0] == "npm"  # npm start, without an entry file
    py = Service.model_validate({"name": "py", "folder": str(tmp_path), "runtime": "python", "entry": "a.py",
                                 "mode": "container"})
    assert container.linux_argv(py, None)[0] == "python"  # the image's, also when this computer has only "py"


@POSIX
def test_docker_inside_wsl(fake_wsl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "engine", lambda choice="auto", distro="": f"wsl:{distro}" if choice == "wsl"
                        else "")
    monkeypatch.setattr(wsl, "locate", lambda distro, folder: wsl.Place(f"/mnt/c/{folder.name}", 1000, 1000))
    folder = tmp_path / "web"
    folder.mkdir()
    service = Service.model_validate({"id": "abc123", "name": "web", "folder": str(folder), "runtime": "python",
                                      "entry": "app.py", "mode": "container", "env": {"SECRET": "s3"},
                                      "container": {"engine": "wsl"}, "wsl": {"distro": "Ubuntu"},
                                      "network": {"ports": [8000]}})
    cmd = isolation.prepare(service, DataDirs(tmp_path / "data"))
    assert cmd.argv[:6] == [str(fake_wsl), "--distribution", "Ubuntu", "--exec", "docker", "run"]
    assert "type=bind,source=/mnt/c/web,target=/app" in cmd.argv
    assert cmd.argv[cmd.argv.index("--user") + 1] == "1000:1000"
    assert cmd.argv[cmd.argv.index("-p"):cmd.argv.index("-p") + 2] == ["-p", "127.0.0.1:8000:8000"]
    assert "SECRET/u" in cmd.env["WSLENV"].split(":") and cmd.env["SECRET"] == "s3"
    assert "s3" not in " ".join(cmd.argv)
    assert cmd.engine == "wsl:Ubuntu" and "docker in WSL (Ubuntu)" in cmd.describe
    assert container.cli("wsl:") == [str(fake_wsl), "--exec", "docker"]
    assert container.cli("/usr/bin/docker") == ["/usr/bin/docker"]
    (folder / "compose.yaml").write_text("services: {}\n")
    stack = service.model_copy(update={"runtime": "compose", "entry": "compose.yaml"})
    with pytest.raises(IsolationError, match="Docker Desktop"):
        container.wrap(stack)


def test_auto_never_starts_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def wsl_engine(distro: str) -> str:
        asked.append(distro)
        return ""

    monkeypatch.setattr(container, "_wsl_engine", wsl_engine)
    monkeypatch.setattr(runtimes, "which", lambda name: None)
    container._engine_cache.clear()
    assert container.engine("auto") == ""
    assert asked == []
    assert container.engine("wsl", "Debian") == ""
    assert asked == ["Debian"]


def _wait_exit(sink: queue.SimpleQueue[Any], timeout: float) -> Exited | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            item = sink.get(timeout=0.1)
        except queue.Empty:
            continue
        if isinstance(item, Exited):
            return item
        item.source.take()
    return None


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@POSIX
def test_stop_ends_the_whole_run_inside_linux(fake_wsl: Path, tmp_path: Path) -> None:
    """Ending wsl.exe isn't documented to end what it started: Stop signals every process with the run's mark,
    also one that started a session of its own (a daemonising server)."""
    pidfile = tmp_path / "child.pid"
    # Forks twice, like a daemon: the sleeper leaves both the session and the process tree of the run.
    code = ("import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    if os.fork() == 0:\n"
            f"        open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "        time.sleep(120)\n"
            "    os._exit(0)\n"
            "time.sleep(120)\n")
    (tmp_path / "daemonise.py").write_text(code)
    service = wsl_service(tmp_path, runtime="custom", entry="", command=[sys.executable, "daemonise.py"])
    cmd = wsl.wrap(service)
    sink: queue.SimpleQueue[Any] = queue.SimpleQueue()
    runner = Runner("svc", cmd, sink)
    runner.start()
    end = time.monotonic() + 20
    while not pidfile.exists() and time.monotonic() < end:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(child)
    assert cmd.wsl is not None
    assert os.environ.get(wsl.MARK) is None
    runner.stop(5)
    done = _wait_exit(sink, 20)
    assert done is not None
    end = time.monotonic() + 5
    while _alive(child) and time.monotonic() < end:
        time.sleep(0.05)
    assert not _alive(child), "the process that left its session was ended by the run's mark"


@POSIX
def test_signal_touches_only_its_own_run(fake_wsl: Path, tmp_path: Path) -> None:
    import subprocess

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             env={**os.environ, wsl.MARK: "0123456789abcdef"})
    try:
        wsl.signal(WslRun("", "fedcba9876543210"), "KILL")
        time.sleep(0.2)
        assert other.poll() is None, "another run's processes are left alone"
        wsl.signal(WslRun("", "0123456789abcdef"), "TERM")
        assert other.wait(10) is not None
    finally:
        other.kill()


def test_wsl_services_need_consent_like_full_access(harness: Any, project: Path) -> None:
    sid = harness.add(name="linux", folder=str(project), entry="bot.py", mode="wsl", restart="never")
    ok, _data, err = harness.call({"op": "start", "id": sid})
    assert not ok and err == "confirm_full"


def test_dialog_keeps_wsl_settings_and_asks_again_when_the_mode_changes(qtbot: Any, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.ui.service_dialog import ServiceDialog

    monkeypatch.setattr(wsl, "distros", lambda: ["Debian", "Ubuntu"])  # never the real wsl.exe of a test machine

    (tmp_path / "bot.py").write_text("")
    original = wsl_service(tmp_path, wsl={"distro": "Debian"}, full_access_ok=True)
    dlg = ServiceDialog(original, [])
    qtbot.addWidget(dlg)
    modes = {str(b.property("mode")) for b in dlg.modes.buttons()}
    assert "wsl" in modes  # shown for a WSL service on any system; on Windows always
    assert not dlg.wsl_box.isHidden() and dlg.container_box.isHidden()
    built = dlg.build()
    assert built.mode is Mode.WSL and built.wsl.distro == "Debian" and built.full_access_ok
    assert "WSL's own Python" in dlg.env_line.text() and not dlg.own_env.isEnabled()
    next(b for b in dlg.modes.buttons() if b.property("mode") == "full").setChecked(True)
    assert not dlg.build().full_access_ok, "confirmed for WSL is not confirmed for Full access"
    next(b for b in dlg.modes.buttons() if b.property("mode") == "container").setChecked(True)
    assert dlg.wsl_box.isHidden()
    if dlg.engine.findText("wsl") >= 0:
        dlg.engine.setCurrentText("wsl")
        assert not dlg.wsl_box.isHidden()  # Docker inside WSL uses the same distribution


@POSIX
def test_wsl_is_asked_once_and_in_the_background(fake_wsl: Path, tmp_path: Path, dirs: DataDirs,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Path] = []
    real = wsl._ask_place

    def ask(distro: str, folder: Path) -> wsl.Place:
        asked.append(folder)
        return real(distro, folder)

    monkeypatch.setattr(wsl, "_ask_place", ask)
    monkeypatch.setattr(isolation, "IS_WINDOWS", True)
    service = wsl_service(tmp_path)
    work = isolation.slow_setup(service, dirs)
    assert work is not None and "Waiting for WSL" in isolation.setup_note(service)
    work()
    assert isolation.slow_setup(service, dirs) is None  # known now: the start doesn't wait for WSL
    wsl.wrap(service)
    wsl.wrap(service, task=["true"])
    assert asked == [tmp_path]
