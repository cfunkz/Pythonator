"""Runtimes and their versions, ports in use or reserved, what else listens, and what a service is measured to use."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from pythonator.core import envcheck, runtimes, templates, versions
from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.supervisor import Supervisor
from pythonator.paths import IS_LINUX, IS_WINDOWS, DataDirs


def test_versions_are_read_from_what_each_program_prints() -> None:
    assert versions.parse("v22.13.0") == (22, 13, 0)
    assert versions.parse("Python 3.13.1") == (3, 13, 1)
    assert versions.parse("go version go1.24.7 linux/amd64") == (1, 24, 7)
    assert versions.parse('openjdk version "21" 2023-09-19') == (21, 0, 0)
    assert versions.parse("no digits") is None


@pytest.mark.parametrize(("version", "spec", "ok"), [
    ((22, 13, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", False),  # Nuxt 4 on the Node.js of the user's log
    ((22, 22, 3), "^22.22.3 || ^24.15.0 || >=26.0.0", True),
    ((23, 0, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", False),
    ((26, 1, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", True),
    ((20, 19, 0), "^20.19.0 || >=22.12.0", True),
    ((21, 0, 0), "^20.19.0 || >=22.12.0", False),
    ((3, 9, 0), ">=3.10", False),
    ((3, 12, 1), ">= 3.10, <4", True),
    ((3, 12, 1), "~=3.10", True),
    ((4, 0, 0), "~=3.10", False),
    ((8, 3, 4), "^8.2|^8.3", True),
    ((22, 5, 0), "22.x", True),
    ((23, 0, 0), "22", False),
    ((1, 2, 3), "latest", True),  # nothing to read: never blocks
])
def test_npm_style_ranges(version: versions.Version, spec: str, ok: bool) -> None:
    assert versions.satisfies(version, spec) is ok


def test_a_shortfall_says_what_to_install() -> None:
    assert versions.problem("node", (22, 13, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", what="Nuxt") == (
        "Nuxt needs Node.js 22.22.3 or newer (this computer has 22.13.0): install the current LTS from nodejs.org")
    assert versions.problem("node", (22, 22, 3), ">=20") == ""
    assert versions.problem("node", None, ">=99") == ""  # unknown: never blocks
    assert versions.shortfall("python", (3, 9, 0), ">=3.10")[0] == "Python 3.10.0 or newer (this computer has 3.9.0)"


def test_each_runtimes_project_files_say_which_versions_it_needs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": ">=22.12", "bun": ">=1.2"}}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.18\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nrust-version = "1.85"\n')
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"php": "^8.2"}}))
    assert versions.declared("node", tmp_path) == (">=22.12", "package.json (engines)")
    assert versions.declared("bun", tmp_path)[0] == ">=1.2"
    assert versions.declared("python", tmp_path) == (">=3.11", "pyproject.toml (requires-python)")
    assert versions.declared("go", tmp_path)[0] == ">=1.18"
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.25.0\n")
    assert versions.declared("go", tmp_path)[0] == ">=1.21"  # Go 1.21+ fetches the toolchain a go.mod asks for
    assert versions.declared("rust", tmp_path)[0] == ">=1.85"
    assert versions.declared("php", tmp_path)[0] == "^8.2"
    assert versions.declared("ruby", tmp_path) == ("", "")
    (tmp_path / "package.json").write_text("{broken")
    assert versions.declared("node", tmp_path) == ("", "")


def test_a_project_that_needs_a_newer_runtime_is_named_before_it_runs(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": "^22.22.3 || >=24.15.0"}}))
    (tmp_path / "index.js").write_text("")
    monkeypatch.setattr(runtimes, "program", lambda rt, folder: "/bin/node")
    monkeypatch.setattr(versions, "installed", lambda prog: (22, 13, 0))
    service = Service.model_validate({"name": "n", "folder": str(tmp_path), "runtime": "node", "entry": "index.js"})
    errors = [text for level, text in envcheck.service_report(service) if level == "error"]
    assert errors == ["This project (package.json (engines)) needs Node.js 22.22.3 or newer (this computer has "
                      "22.13.0): install the current LTS from nodejs.org"]
    monkeypatch.setattr(versions, "installed", lambda prog: (24, 15, 0))
    assert not [t for level, t in envcheck.service_report(service) if level == "error"]


def test_the_version_of_a_real_program_is_asked_once(tmp_path: Path) -> None:
    import sys

    v = versions.installed(sys.executable)
    assert v is not None and v[:2] == sys.version_info[:2]
    assert versions.installed(str(tmp_path / "missing")) is None


def test_a_too_old_node_is_refused_with_what_to_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(versions, "installed", lambda prog: (22, 13, 0))
    with pytest.raises(ValueError, match=r"Nuxt needs Node.js 22.22.3 or newer \(this computer has 22.13.0\): "):
        templates.plan("nuxt", "app", tmp_path, set())
    assert templates.plan("react", "app", tmp_path, set()).steps  # Vite runs on 22.13


def test_a_port_in_use_is_named_before_the_start(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        service = Service.model_validate({"name": "s", "folder": str(tmp_path), "runtime": "python",
                                          "entry": "x.py", "network": {"ports": [port]}})
        problems = envcheck.port_problems(service)
        assert len(problems) == 1 and f"Port {port} is already in use" in problems[0]
        off = service.model_copy(update={"network": service.network.model_copy(update={"enabled": False})})
        assert envcheck.port_problems(off) == []
    assert envcheck.port_problems(service) == []  # free again


def test_a_reserved_windows_port_is_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(envcheck, "IS_WINDOWS", True)
    monkeypatch.setattr(envcheck, "_port_refused", lambda port: "reserved")
    service = Service.model_validate({"name": "s", "folder": str(tmp_path), "runtime": "node", "entry": "x.js",
                                      "network": {"ports": [5173]}})
    (text,) = envcheck.port_problems(service)
    assert "Windows has reserved port 5173" in text and "excludedportrange" in text


def test_rustups_toolchains_are_found_for_a_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.isolation.base import rustup_home

    proxy = tmp_path / ".cargo" / "bin" / "cargo"
    (tmp_path / ".rustup" / "toolchains").mkdir(parents=True)
    monkeypatch.delenv("RUSTUP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert rustup_home(proxy) == tmp_path / ".rustup"
    assert rustup_home(tmp_path / "usr" / "bin" / "cargo") is None  # a distribution's cargo: no rustup
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "elsewhere"))
    assert rustup_home(proxy) is None  # no toolchains there


def test_the_version_check_reads_the_venv_python_instead_of_running_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    venv = tmp_path / ".venv"
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text(f"#!/bin/sh\ntouch {marker}\necho Python 3.13.0\n")
    py.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.1\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    problem = envcheck._declared_version("python", tmp_path, str(py))
    assert "3.11.0 or newer (this computer has 3.9.1)" in problem
    assert not marker.exists()


def test_a_port_in_use_is_named_with_a_free_one_to_use_instead(tmp_path: Path) -> None:
    import socket

    from pythonator.core import envcheck

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        service = Service.model_validate({"name": "web", "folder": str(tmp_path), "runtime": "python",
                                          "entry": "app.py", "network": {"enabled": True, "ports": [port]}})
        [text] = envcheck.port_problems(service)
        assert f"Port {port} is already in use" in text
        near = envcheck.free_port_near(port)
        assert near > port and f"(port {near} is free)" in text
        assert not envcheck.port_reserved(port)
    service.network.ports = [near]
    assert envcheck.port_problems(service) == []


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
    """The decision itself, from a connection list: a listener outside the service's tree on one of its ports."""
    from pythonator.daemon import supervisor

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        mine = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="127.0.0.1", port=8000))
        other = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="0.0.0.0", port=8000))  # noqa: S104
        elsewhere = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="127.0.0.1", port=9000))
        conns = {child.pid: [mine], 999_999: [other], 999_998: [elsewhere]}
        st = supervisor._tree_stats(child.pid, {}, ports=True, conns=conns, wanted=[8000])
    finally:
        child.kill()
        child.wait()
    assert st.ports == [8000] and st.addresses == {8000: ["127.0.0.1"]}
    assert st.others == {8000: [999_999]}  # its own listener and other ports don't count


def test_this_system_lists_other_programs_listeners() -> None:
    """What the decision above is fed: psutil's list of every program's sockets. Where it doesn't show another
    program's listener, the note can't appear, so this says why instead of failing on the decision."""
    import socket

    from pythonator.daemon import supervisor

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        conns = supervisor._connections()
        if conns is None:
            pytest.skip("this system lists other programs' connections only to root")
        found = [(pid, c.laddr) for pid, items in conns.items() for c in items
                 if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port]
        if not found and IS_WINDOWS:  # seen on one Windows 11 PC (124 others listed), never on GitHub's Windows
            pytest.skip(f"this computer's connection list leaves out this test's own listener on {port}, so the "
                        "'another program on this port' note can't appear here")
        assert found, f"psutil didn't list the listener on {port}: {sum(map(len, conns.values()))} connections"
        assert os.getpid() in {pid for pid, _ in found}, found


@pytest.mark.skipif(not IS_LINUX, reason="PSS is Linux's")
def test_forked_workers_shared_memory_is_counted_once() -> None:
    """A prefork server: a parent with 200 MB and four forked workers that only read it. Summed RSS says about
    1 GB; what they hold is about 200 MB, so a 500 MB limit must not stop it."""
    from pythonator.daemon import supervisor

    code = ("import os, time\nb = bytearray(200 * 1024 * 1024)\nfor i in range(0, len(b), 4096): b[i] = 1\n"
            "for _ in range(4):\n    if os.fork() == 0:\n        s = sum(b[i] for i in range(0, len(b), 4096))\n"
            "        time.sleep(30); os._exit(0)\nprint('ready', flush=True)\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == b"ready"
        time.sleep(1.5)
        st = supervisor._tree_stats(proc.pid, {}, ports=False)
        assert st.processes == 5
        assert st.memory > 700 * 1024 * 1024  # what the watchdog first sees
        assert supervisor._own_memory(st) < 450 * 1024 * 1024
    finally:
        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
        proc.kill()
        proc.wait()
