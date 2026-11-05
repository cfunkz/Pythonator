"""Escape attempts from inside the sandbox. Each probe prints OK (allowed) or NO (blocked).

They run on whatever this computer enforces; a protection that isn't available here (see System check) is
skipped, not failed. CI runs them on Linux, macOS and Windows runners.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pythonator import isolation
from pythonator.core.models import Service
from pythonator.paths import IS_LINUX, IS_WINDOWS, DataDirs

PROBE = r'''
import json, os, socket, sys
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = "OK"
    except Exception as exc:
        out[name] = "NO " + type(exc).__name__
cfg = json.loads(sys.argv[1])
attempt("read_secret", lambda: open(cfg["secret"]).read())
attempt("list_home_secret_dir", lambda: os.listdir(cfg["secret_dir"]))
attempt("read_token", lambda: open(cfg["token"]).read())
try:  # Linux shows the empty folders its own mounts need; macOS refuses the listing outright
    out["data_listing"] = sorted(os.listdir(cfg["data"])) if os.path.isdir(cfg["data"]) else []
except OSError:
    out["data_listing"] = []
attempt("write_outside", lambda: open(cfg["outside"], "w").write("x"))
attempt("write_project", lambda: open("inside.txt", "w").write("x"))
def bind(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.close()
attempt("bind_listed", lambda: bind(cfg["port"]))
attempt("bind_other", lambda: bind(cfg["other"]))
attempt("connect_out", lambda: socket.create_connection(("1.1.1.1", 53), timeout=3).close())
print("RESULT " + json.dumps(out))
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def enforced() -> dict[str, bool]:
    return {k: v == "enforced" for k, v in isolation.capabilities()}


def run_probe(dirs: DataDirs, project: Path, tmp_path: Path, *, network: bool) -> dict[str, str]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "id_ed25519").write_text("PRIVATE KEY")
    (project / "probe.py").write_text(PROBE)
    dirs.token_file.write_text("daemon-secret")
    port, other = free_port(), free_port()
    service = Service.model_validate({
        "name": "probe", "folder": str(project), "entry": "probe.py",
        "args": [json.dumps({"secret": str(secret_dir / "id_ed25519"), "secret_dir": str(secret_dir),
                             "data": str(dirs.root), "token": str(dirs.token_file),
                             "outside": str(tmp_path / "outside.txt"),
                             "port": port, "other": other})],
        "network": {"enabled": network, "ports": [port] if network else []}})
    try:
        cmd = isolation.prepare(service, dirs)
    except isolation.IsolationError as exc:
        pytest.skip(f"no sandbox here: {exc}")
    done = subprocess.run(cmd.argv, cwd=cmd.cwd, env=cmd.env, capture_output=True, text=True, timeout=60,
                          check=False)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")), "")
    assert line, f"probe didn't run: {done.returncode}\n{done.stdout}\n{done.stderr}"
    result: dict[str, str] = json.loads(line.removeprefix("RESULT "))
    return result


@pytest.mark.skipif(IS_WINDOWS, reason="AppContainer probes need a real desktop session; covered in CI notes")
def test_files_outside_are_out_of_reach(dirs: DataDirs, project: Path, tmp_path: Path) -> None:
    caps = enforced()
    if not caps.get("Files outside its folders"):
        pytest.skip("file isolation isn't available on this computer")
    r = run_probe(dirs, project, tmp_path, network=True)
    assert r["write_project"] == "OK"
    assert r["read_secret"].startswith("NO")
    assert r["list_home_secret_dir"].startswith("NO")
    assert r["read_token"].startswith("NO"), "a service must never see Pythonator's own data (tokens, logs)"
    assert "daemon.token" not in r["data_listing"]  # at most the empty folders its own mounts need
    # Refused, or (under /tmp) written into the service's private /tmp: either way the real file isn't touched.
    assert not (tmp_path / "outside.txt").exists(), r["write_outside"]


@pytest.mark.skipif(IS_WINDOWS, reason="see above")
def test_only_listed_ports(dirs: DataDirs, project: Path, tmp_path: Path) -> None:
    if not enforced().get("Only listed ports"):
        pytest.skip("port rules aren't available on this computer")
    r = run_probe(dirs, project, tmp_path, network=True)
    assert r["bind_listed"] == "OK"
    assert r["bind_other"].startswith("NO")


@pytest.mark.skipif(IS_WINDOWS, reason="see above")
def test_network_off(dirs: DataDirs, project: Path, tmp_path: Path) -> None:
    if not enforced().get("Network off"):
        pytest.skip("network isolation isn't available on this computer")
    r = run_probe(dirs, project, tmp_path, network=False)
    assert r["bind_listed"].startswith("NO")
    assert r["connect_out"].startswith("NO")


@pytest.mark.skipif(not IS_LINUX, reason="Linux kernel attack surface")
def test_seccomp_blocks_ptrace(dirs: DataDirs, project: Path) -> None:
    if not enforced().get("Kernel attack surface (ptrace, bpf, io_uring...)"):
        pytest.skip("seccomp isn't available")
    (project / "pt.py").write_text(
        "import ctypes, os\nlibc = ctypes.CDLL(None, use_errno=True)\n"
        "r = libc.ptrace(16, os.getppid(), 0, 0)\nprint('PTRACE', r, ctypes.get_errno())\n")
    service = Service.model_validate({"name": "pt", "folder": str(project), "entry": "pt.py"})
    cmd = isolation.prepare(service, dirs)
    done = subprocess.run(cmd.argv, cwd=cmd.cwd, env=cmd.env, capture_output=True, text=True, timeout=60,
                          check=False)
    assert "PTRACE -1" in done.stdout, done.stdout + done.stderr


def test_full_mode_is_plain(dirs: DataDirs, project: Path) -> None:
    (project / "a.py").write_text("print(1)")
    service = Service.model_validate({"name": "f", "folder": str(project), "entry": "a.py", "mode": "full"})
    cmd = isolation.prepare(service, dirs)
    assert cmd.argv == cmd.shown
    assert "Full access" in cmd.describe


def test_container_flags(dirs: DataDirs, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.isolation import container

    monkeypatch.setattr(container, "engine", lambda _choice="auto": "/usr/bin/podman")
    (project / "app.py").write_text("")
    service = Service.model_validate({"name": "c", "folder": str(project), "entry": "app.py", "mode": "container",
                                      "env": {"API_TOKEN": "s3cret-value"}, "network": {"ports": [8000]},
                                      "memory_mb": 256, "cpu_percent": 150})
    argv = isolation.prepare(service, dirs).argv
    joined = " ".join(argv)
    for flag in ("--cap-drop ALL", "no-new-privileges", "--read-only", "--pids-limit", "-p 127.0.0.1:8000:8000",
                 "--memory 256m", "--cpus 1.5"):
        assert flag in joined
    assert "s3cret-value" not in joined, "secrets go through the environment, never the command line"
    assert "--privileged" not in argv and "host" not in argv


SERVER = r'''
import socket, sys
s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(8)
print("LISTENING", flush=True)
while True:
    c, _ = s.accept()
    c.sendall(b"hello from the sandbox")
    c.close()
'''


def _reach(port: int) -> str:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
            c.settimeout(3)
            return c.recv(64).decode(errors="replace") or "EMPTY"
    except OSError as exc:
        return f"NO {type(exc).__name__}"


def _is_admin() -> bool:
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined,unused-ignore]


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows keeps AppContainers off loopback; elsewhere there's nothing to do")
def test_windows_make_reachable(dirs: DataDirs, project: Path) -> None:
    """A sandboxed server on 127.0.0.1 is out of this PC's reach, Make reachable lets the browser in, Undo shuts it
    again. CI runners are administrators, so the elevated script runs here without a UAC prompt."""
    from pythonator import launcher
    from pythonator.isolation import windows

    if not _is_admin():
        pytest.skip("needs an administrator (CI runners are)")
    if not windows.available():
        pytest.skip("no AppContainer sandbox here")
    port = free_port()
    (project / "server.py").write_text(SERVER)
    service = Service.model_validate({"name": "reach", "folder": str(project), "entry": "server.py",
                                      "args": [str(port)], "network": {"enabled": True, "ports": [port]}})
    cmd = isolation.prepare(service, dirs)
    container = windows.container_name(service.id)

    def apply(*, undo: bool) -> None:
        sid, _folder = launcher.windows_profile(container)
        script = windows.reach_script(sid, container, [port], lan=False, undo=undo)
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-EncodedCommand", windows.encoded(script)], capture_output=True, text=True,
                              timeout=180, check=False)
        assert done.returncode == 0, f"exit {done.returncode}\n{done.stdout}\n{done.stderr}"

    proc = subprocess.Popen(cmd.argv, cwd=cmd.cwd, env=cmd.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        assert proc.stdout is not None
        # The launcher may print notes first (granting the sandbox Python's folder takes a while the first time).
        seen: list[str] = []
        reader = threading.Thread(target=lambda: seen.extend(iter(proc.stdout.readline, "")), daemon=True)  # type: ignore[union-attr]
        reader.start()
        deadline = time.monotonic() + 180
        while not any("LISTENING" in line for line in seen) and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert any("LISTENING" in line for line in seen), "".join(seen) or f"exit {proc.poll()}"
        before = _reach(port)
        apply(undo=False)
        after = _reach(port)
        apply(undo=True)
        undone = _reach(port)
    finally:
        proc.kill()
        proc.wait(10)
        windows.forget(dirs, service.id)
    diagnostics = f"before={before!r} after={after!r} undone={undone!r}"
    assert after == "hello from the sandbox", diagnostics
    if before.startswith("NO"):  # this Windows blocks loopback into AppContainers: Undo must block it again
        assert undone.startswith("NO"), diagnostics


def test_no_process_start_shows_the_busy_pointer() -> None:
    """Windows turns the mouse pointer into the busy one for every process started without STARTF_FORCEOFFFEEDBACK:
    starting or stopping a service must never do that."""
    import ast

    import pythonator
    from pythonator.paths import STARTF_FORCEOFFFEEDBACK, quiet_start

    unix_only = {"linux.py", "macos.py", "keychain.py"}
    root = Path(pythonator.__file__).parent
    missing = []
    for path in root.rglob("*.py"):
        if path.name in unix_only:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("Popen", "run") and getattr(node.func.value, "id", "") == "subprocess" \
                    and not any(k.arg == "startupinfo" for k in node.keywords):
                missing.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not missing
    assert "STARTF_FORCEOFFFEEDBACK" in (root / "launcher.py").read_text(encoding="utf-8")
    info = quiet_start()
    if sys.platform == "win32":
        assert info.dwFlags & STARTF_FORCEOFFFEEDBACK  # type: ignore[attr-defined]
    else:
        assert info is None
