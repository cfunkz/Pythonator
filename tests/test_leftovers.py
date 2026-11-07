"""What a removed service can leave behind: Make reachable's firewall rules on Windows (undone on removal, and
found by the System check if that was cancelled), containers still running, and processes still listening. The
System check finds each one and cleans it up."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation, launcher
from pythonator.core.models import Service
from pythonator.isolation import leftovers, windows
from pythonator.isolation.leftovers import Found, Leftover
from pythonator.paths import IS_WINDOWS, DataDirs
from tests.conftest import DaemonHarness

SERVER = r'''
import socket, sys, time
s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(8)
print("LISTENING", flush=True)
time.sleep(600)
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _listening(port: int, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


# -- every service's processes carry its mark --------------------------------------------------------------------


def test_every_service_process_carries_its_mark(dirs: DataDirs, project: Path) -> None:
    (project / "main.py").write_text("print(1)\n")
    full = Service.model_validate({"name": "full", "folder": str(project), "entry": "main.py", "mode": "full"})
    assert isolation.prepare(full, dirs).env[leftovers.MARKER] == full.id
    sandboxed = Service.model_validate({"name": "sandboxed", "folder": str(project), "entry": "main.py"})
    try:
        cmd = isolation.prepare(sandboxed, dirs)
    except isolation.IsolationError as exc:
        pytest.skip(f"no sandbox here: {exc}")
    assert cmd.env[leftovers.MARKER] == sandboxed.id


def test_a_process_left_listening_is_found_and_stopped(tmp_path: Path) -> None:
    port = free_port()
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    proc = subprocess.Popen([sys.executable, str(script), str(port)],
                            env={**os.environ, leftovers.MARKER: "gone-svc"}, stdout=subprocess.DEVNULL)
    try:
        assert _listening(port)

        def mine(services: dict[str, list[int]]) -> list[Leftover]:
            return [i for i in leftovers.find(services).items if i.kind == "process" and i.service == "gone-svc"]

        assert mine({"gone-svc": [proc.pid]}) == []  # Pythonator runs it: not a leftover
        stray = mine({"gone-svc": []})  # its service exists but is stopped: a stray
        assert len(stray) == 1 and not stray[0].gone and stray[0].ports == (port,)
        found = mine({})  # its service is gone
        assert len(found) == 1 and found[0].gone and f"process {proc.pid}" in found[0].what
        assert "removed service" in leftovers.origin(found[0], {})
        assert leftovers.clean(found) == []
        proc.wait(10)
        assert not _listening(port, timeout=1)
    finally:
        proc.kill()
        proc.wait(10)


def test_clean_never_kills_a_pid_that_isnt_the_leftover_any_more(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            env={**os.environ, leftovers.MARKER: "gone-svc"})
    try:
        import psutil

        created = psutil.Process(proc.pid).create_time()
        other_start = Leftover("process", f"{proc.pid}:{created + 100}", "gone-svc", "x")
        other_mark = Leftover("process", f"{proc.pid}:{created}", "another-svc", "x")
        assert leftovers.clean([other_start, other_mark]) == []
        time.sleep(0.3)
        assert proc.poll() is None  # both described another process: it lives
    finally:
        proc.kill()
        proc.wait(10)


def test_the_system_check_gets_what_pythonator_runs(harness: DaemonHarness, project: Path) -> None:
    """The background service says which processes it runs for each service, so a listener of a running service
    is never offered for clean-up; the same listener, its service removed, is."""
    port = free_port()
    (project / "server.py").write_text(SERVER)
    sid = harness.add(name="web", folder=str(project), entry="server.py", args=[str(port)], mode="full",
                      restart="never")
    assert harness.ok({"op": "capabilities"})["services"] == {sid: []}
    harness.ok({"op": "start", "id": sid, "confirmed": True})
    harness.wait(lambda: "LISTENING" in harness.output(sid), timeout=30_000)
    services = harness.ok({"op": "capabilities"})["services"]
    assert len(services[sid]) == 1
    assert [i for i in leftovers.find(services).items if i.service == sid] == []
    left = [i for i in leftovers.find({}).items if i.kind == "process" and i.service == sid]
    assert left and left[0].ports == (port,) and left[0].gone
    harness.ok({"op": "stop", "id": sid})
    harness.wait(lambda: harness.states(sid)[-1] == "stopped", timeout=30_000)


# -- containers ----------------------------------------------------------------------------------------------


PS = "\n".join([
    "pythonator-old\t127.0.0.1:8000->8000/tcp\tpythonator=1",
    "pythonator-live\t127.0.0.1:8001->8001/tcp\tpythonator=1",
    "pythonator-stack-web-1\t0.0.0.0:9000->80/tcp, :::9000->80/tcp\t"
    "com.docker.compose.project=pythonator-stack,com.docker.compose.service=web",
    "pythonator-stack-db-1\t\tcom.docker.compose.project=pythonator-stack,com.docker.compose.service=db",
    "postgres\t0.0.0.0:5432->5432/tcp\tfoo=bar",
    "pythonator-fake\t0.0.0.0:7000->7000/tcp\tsomeone=else",
    "pythonator-pod-app-1\t0.0.0.0:7100->7100/tcp\t"
    "map[com.docker.compose.project:pythonator-pod io.podman.compose.project:pythonator-pod]",
])


def test_containers_left_running_are_found_by_label_and_compose_project(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, PS if argv[1] == "ps" else "", "")

    monkeypatch.setattr(subprocess, "run", run)
    found = Found()
    leftovers._containers({"live": [123], "stack": []}, "docker", found)
    by = {i.service: i for i in found.items}
    assert set(by) == {"old", "stack", "pod"}  # live runs; postgres and an unlabelled look-alike aren't ours
    assert by["old"].gone and by["old"].ports == (8000,)
    assert not by["stack"].gone and by["stack"].ports == (9000,)  # stopped in Pythonator, running in Docker
    assert by["stack"].key.split("\n") == ["docker", "pythonator-stack-db-1", "pythonator-stack-web-1"]
    assert by["pod"].gone and by["pod"].ports == (7100,)
    leftovers._containers({}, "/usr/bin/docker", found)  # Podman's docker stand-in lists them again
    assert len(found.items) == 4  # only "live" is new

    monkeypatch.setattr(leftovers, "_engines", lambda: ["docker"])
    calls.clear()
    assert leftovers.clean([by["old"], by["stack"]]) == []
    assert calls == [["docker", "rm", "-f", "pythonator-old", "pythonator-stack-db-1", "pythonator-stack-web-1"]]
    monkeypatch.setattr(leftovers, "_engines", lambda: [])
    assert leftovers.clean([by["old"]]) == ["docker isn't running any more"]


# -- Windows: Make reachable -------------------------------------------------------------------------------------


def test_windows_firewall_rules_are_read_with_their_ports() -> None:
    text = "\n".join(["Pythonator Pythonator.web (this PC)\t8000,8001", "Pythonator Pythonator.web\t8000 8001",
                      "Pythonator Pythonator.any (this PC)\tAny", "Pythonator something else\t80",
                      "Some other app\t443", ""])
    assert windows.parse_rules(text) == {"Pythonator.any": [], "Pythonator.web": [8000, 8001]}


def test_undo_takes_back_everything_for_each_sandbox_in_one_script() -> None:
    script = windows.unreach_script([("S-1-15-2-1-2", "Pythonator.a"), ("S-1-15-2-3-4", "Pythonator.b")])
    assert script.count("Add-Type") == 1  # compiled once: a second Add-Type of the same type fails
    for sid, name in (("S-1-15-2-1-2", "Pythonator.a"), ("S-1-15-2-3-4", "Pythonator.b")):
        assert f"LoopbackExempt -d '-p={sid}'" in script
        assert f"[PythonatorWfp]::Run('{sid}', 'Pythonator {name} (this PC, loopback)', [int[]]@(), $true)" in script
        assert f"-DisplayName 'Pythonator {name} (this PC)'" in script and f"-DisplayName 'Pythonator {name}'" in script
    assert "New-NetFirewallRule" not in script and script.endswith("exit 0")
    with pytest.raises(ValueError):
        windows.unreach_script([("S-1-5-21-1", "Pythonator.a")])
    with pytest.raises(ValueError):
        windows.unreach_script([("S-1-15-2-1", "Other'; rm")])


def test_windows_finds_what_a_removed_sandbox_still_has(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "reach_rules", lambda: {"Pythonator.a": [8000]})
    monkeypatch.setattr(windows, "_exempt_sids", lambda: {"s-1-15-2-9"})
    sids = {"Pythonator.a": "S-1-15-2-1", "Pythonator.b": "S-1-15-2-9", "Pythonator.c": "S-1-15-2-5"}
    monkeypatch.setattr(launcher, "windows_sid", lambda name: sids[name])
    assert windows.reach_left(["Pythonator.a", "Pythonator.b", "Pythonator.c"]) == ["Pythonator.a", "Pythonator.b"]
    monkeypatch.setattr(windows, "reach_rules", lambda: None)
    monkeypatch.setattr(windows, "_exempt_sids", lambda: None)
    assert windows.reach_left(["Pythonator.a"]) is None  # Windows can't say: nothing is undone blindly

    monkeypatch.setattr(leftovers, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "reach_rules", lambda: {"Pythonator.keep": [], "Pythonator.gone": [8000]})
    found = Found()
    leftovers._reach({"keep": []}, found)
    assert [(i.kind, i.key, i.service, i.ports) for i in found.items] == [
        ("reach", "Pythonator.gone", "gone", (8000,))]


def test_removing_a_service_undoes_make_reachable(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: after Remove, the window takes back the removed services' Make reachable (one administrator
    prompt) when there is something to take back, and says how it went."""
    from PyQt6.QtWidgets import QWidget

    from pythonator.ui.main_window import MainWindow

    class Window(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.notices: list[tuple[str, str]] = []

        def _notice(self, level: str, text: str) -> None:
            self.notices.append((level, text))

    undone: list[list[str]] = []
    monkeypatch.setattr(windows, "reach_left", lambda names: [n for n in names if n == "Pythonator.web"])
    def unreach(names: list[str]) -> tuple[bool, str]:
        undone.append(list(names))
        return True, "Done: removed"

    monkeypatch.setattr(windows, "unreach", unreach)
    window = Window()
    qtbot.addWidget(window)
    MainWindow._unreach_removed(window, ["web", "worker"])  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: ("ok", "Done: removed") in window.notices)
    assert undone == [["Pythonator.web"]] and window.notices[0][0] == "info"
    window.notices.clear()
    cancelled = "Cancelled: Windows didn't get administrator rights."
    monkeypatch.setattr(windows, "unreach", lambda names: (False, cancelled))
    MainWindow._unreach_removed(window, ["web"])  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: any(level == "warn" for level, _t in window.notices))
    assert "System check" in window.notices[-1][1]
    window.notices.clear()
    MainWindow._unreach_removed(window, ["worker"])  # type: ignore[arg-type]
    qtbot.wait(300)
    assert window.notices == []  # nothing to take back: no prompt, no message


def _is_admin() -> bool:
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined,unused-ignore]


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", windows.encoded(script)], capture_output=True, text=True, timeout=180,
                          check=False)


@pytest.mark.skipif(not IS_WINDOWS, reason="only Windows has Make reachable")
def test_windows_rules_of_a_removed_service_are_found_and_taken_back(dirs: DataDirs) -> None:
    """For real: Make reachable, then remove the service the old way (its rules stay), and the System check's
    finder sees them; the undo takes them back although the sandbox's profile is gone. CI runners are
    administrators, so the elevated script runs here without a UAC prompt."""
    if not _is_admin():
        pytest.skip("needs an administrator (CI runners are)")
    if not windows.available():
        pytest.skip("no AppContainer sandbox here")
    ident = "leftover-test"
    container = windows.container_name(ident)
    sid, _folder = launcher.windows_profile(container)
    assert launcher.windows_sid(container) == sid  # the same SID, with or without the profile
    port = free_port()
    try:
        done = _powershell(windows.reach_script(sid, container, [port], lan=True))
        assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
        windows.forget(dirs, ident)  # the profile goes, as on removal
        assert launcher.windows_sid(container) == sid
        rules = windows.reach_rules()
        assert rules is not None and rules.get(container) == [port], rules
        assert windows.reach_left([container]) == [container]
        assert [i.key for i in leftovers.find({}).items if i.kind == "reach"].count(container) == 1
        assert [i for i in leftovers.find({ident: []}).items if i.key == container] == []  # still a service
    finally:
        undo = _powershell(windows.unreach_script([(sid, container)]))
        launcher.windows_delete_profile(container)
    assert undo.returncode == 0, f"{undo.stdout}\n{undo.stderr}"
    rules = windows.reach_rules()
    assert rules is not None and container not in rules
    assert sid.lower() not in (windows._exempt_sids() or set())
    assert windows.reach_left([container]) == []
    again = _powershell(windows.unreach_script([(sid, container)]))  # nothing left: still fine
    assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"


# -- the System check --------------------------------------------------------------------------------------------


def test_system_check_lists_leftovers_and_cleans_them_up(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QLabel, QMessageBox, QPushButton

    from pythonator.ui import about

    item = Leftover("process", "1:2", "gone-svc", "python (process 1) still listening (port 8000)", (8000,))
    looked: list[dict[str, list[int]]] = []
    answers = [Found([item], ["Couldn't list the running containers: denied"]), Found()]

    def find(services: dict[str, list[int]]) -> Found:
        looked.append(services)
        return answers[min(len(looked), len(answers)) - 1]

    cleaned: list[list[Leftover]] = []
    said: list[str] = []
    monkeypatch.setattr(leftovers, "find", find)
    def clean(items: list[Leftover]) -> list[str]:
        cleaned.append(list(items))
        return []

    monkeypatch.setattr(leftovers, "clean", clean)
    monkeypatch.setattr(about, "ask", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(about, "info", lambda _p, _t, text: said.append(text))
    dlg = about.SystemCheckDialog({"app": [], "sandbox": [], "services": {"web": [42], "bad": "x"}}, names={})
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: any(line.startswith("[leftover]") for line in dlg.report))
    assert looked[0] == {"web": [42]}
    text = " ".join(w.text() for w in dlg.findChildren(QLabel))
    assert "still listening (port 8000)" in text and "removed service" in text and "denied" in text
    button = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Clean up…")
    button.click()
    qtbot.waitUntil(lambda: bool(said))
    assert cleaned == [[item]] and said == ["Done: nothing is left behind."]
    qtbot.waitUntil(lambda: len(looked) == 2)  # looked again
    qtbot.waitUntil(lambda: "No firewall rules" in " ".join(w.text() for w in dlg.findChildren(QLabel)))
