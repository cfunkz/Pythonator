"""The background service end to end: auth over both transports, apply, approve, and a run in a real jail (the jail
tests are skipped without bubblewrap, so on Windows and macOS only the transport and plugin-store tests run)."""

import os
import shutil
import sys
import threading
import time

import pytest

needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap") or os.name != "posix", reason="needs bubblewrap")


@pytest.fixture
def daemon(tmp_path, monkeypatch, request):
    import asyncio

    if getattr(request, "param", "") == "tcp":
        monkeypatch.setenv("PYTHONATOR_IPC", "tcp")  # the Windows transport, on any OS

    from pythonator.service.daemon import Daemon

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    d = Daemon(root)
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=lambda: loop.run_until_complete(d.serve()), daemon=True)
    th.start()
    from pythonator.service import ipc

    for _ in range(100):
        if ipc.endpoint_exists(root):
            break
        time.sleep(0.05)
    yield root
    loop.call_soon_threadsafe(d._stop.set)
    th.join(10)


@pytest.mark.parametrize("daemon", ["native", "tcp"], indirect=True)
def test_ping_and_wrong_key_is_refused(daemon, tmp_path):
    from pythonator.service.client import Client

    with Client(daemon, timeout=5) as c:
        assert c.call("ping")
    (daemon / "ipc.key").write_text("00" * 32)
    with pytest.raises(ConnectionError):
        Client(daemon, timeout=5).call("ping")


@needs_bwrap
def test_sandboxed_run(daemon, tmp_path):
    from pythonator.service import ipc
    from pythonator.service.client import Client

    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "app.py").write_text(
        "import os\nprint('cwd', os.getcwd())\n"
        "try:\n    os.listdir(os.path.expanduser('~/..'))\n    print('home', os.listdir('/home'))\nexcept OSError as e: print('err', e)\n"
    )
    with Client(daemon) as c:
        res = c.call("apply", spec={"name": "t", "folder": str(folder), "command": ["app.py"]})
        assert not res["approved"] and res["widens"]
        with pytest.raises(ipc.RpcError):
            c.call("approve", name="t", hash="0" * 64)
        c.call("approve", name="t", hash=res["hash"])
        c.call("start", name="t")
        for _ in range(100):
            lines = [x[3] for x in c.call("logs", name="t", count=50)["lines"]]
            if any(x.startswith("stop") for x in lines):
                break
            time.sleep(0.1)
    assert "cwd /app" in lines, lines
    # /home is an empty tmpfs with only the jail's own home, and Landlock denies listing it at all
    assert any(x.startswith(("home ['service']", "err [Errno 13]")) for x in lines), lines


EVIL = """
import os, socket
from pythonator_sdk import plugin

found = []
for path in ("/root", os.path.expanduser("~"), "/etc/shadow", "/home"):
    try:
        found.append((path, len(os.listdir(path)) if os.path.isdir(path) else len(open(path).read())))
    except OSError as e:
        found.append((path, type(e).__name__))
try:
    socket.create_connection(("1.1.1.1", 53), timeout=1)
    found.append(("net", "open"))
except OSError as e:
    found.append(("net", type(e).__name__))

@plugin.hook("policy.check")
def check(change):
    try:
        plugin.notify("hi")
        return {"allow": False, "reason": "notify worked without a grant"}
    except PermissionError:
        pass
    return {"allow": False, "reason": repr(found)}
"""


@needs_bwrap
def test_plugin_jail_and_grants(daemon, tmp_path):
    from pythonator.service import ipc
    from pythonator.service.client import Client

    src = tmp_path / "evil"
    src.mkdir()
    (src / "plugin.toml").write_text(
        '[plugin]\nid = "evil"\nname = "Evil"\nentry = "main.py"\npermissions = ["policy.check", "ui.notify"]\n'
    )
    (src / "main.py").write_text(EVIL)
    folder = tmp_path / "proj"
    folder.mkdir()
    with Client(daemon) as c:
        info = c.call("plugins.install", path=str(src))
        assert not info["official"] and all(p["granted"] is None for p in info["permissions"])
        with pytest.raises(ipc.RpcError):
            c.call("plugins.grant", id="evil", permission="services.read")  # it didn't ask for it
        c.call("plugins.grant", id="evil", permission="policy.check")
        c.call("plugins.enable", id="evil", on=True)
        for _ in range(100):  # its jail starts in the background; review begins once it answered hello
            if next(x for x in c.call("plugins.list")["installed"] if x["id"] == "evil")["state"] == "running":
                break
            time.sleep(0.05)
        with pytest.raises(ipc.RpcError) as e:
            c.call("apply", spec={"name": "x", "folder": str(folder), "command": ["a.py"]})
        reason = e.value.message
        assert "notify worked" not in reason  # ui.notify wasn't granted, so it was refused
        assert "('net', 'open')" not in reason
        # nothing of the host is visible: /root and /etc/shadow don't exist in its world, its own home is unreadable
        for seen in (
            "('/root', 'FileNotFoundError')",
            "('/etc/shadow', 'FileNotFoundError')",
            "('/home', 'PermissionError')",
            "('net', 'PermissionError')",  # seccomp: no sockets at all
        ):
            assert seen in reason, reason
        snap = c.call("plugins.list")
        evil = next(x for x in snap["installed"] if x["id"] == "evil")
        assert evil["calls"]["ui.notify"]["refused"] == 1
        # new code: grants are gone
        (src / "main.py").write_text(EVIL + "\n# v2\n")
        info = c.call("plugins.install", path=str(src))
        assert info["code_changed"] and all(p["granted"] is None for p in info["permissions"])
        c.call("apply", spec={"name": "x", "folder": str(folder), "command": ["a.py"]})  # no longer reviewing


def test_guard_settings_keep_grants(daemon, tmp_path):
    from pythonator.plugins.host import BUNDLED
    from pythonator.service import ipc
    from pythonator.service.client import Client

    with Client(daemon) as c:
        c.call("plugins.install", path=str(BUNDLED / "guard"))
        c.call("plugins.grant", id="guard", permission="policy.check")
        with pytest.raises(ipc.RpcError):
            c.call("plugins.settings", id="guard", settings=["not", "an", "object"])
        snap = c.call("plugins.settings", id="guard", settings={"lan_ports": True})
        g = next(x for x in snap["installed"] if x["id"] == "guard")
        assert g["settings"] == {"lan_ports": True}
        assert next(p for p in g["permissions"] if p["key"] == "policy.check")["granted"] is True


def _run_to_end(c, name, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        lines = [x[3] for x in c.call("logs", name=name, count=80)["lines"]]
        if any(x.startswith("stop") for x in lines):
            return lines
        time.sleep(0.1)
    return lines


@needs_bwrap
def test_gate_blocks_hosts_not_allowed_and_private_addresses(daemon, tmp_path):
    """End to end: jail without a network, relay inside it, egress gate outside. Allowed-host traffic would go out;
    a host not on the list and one resolving to this computer are refused with 403, and a direct socket fails."""
    from pythonator.service.client import Client

    folder = tmp_path / "gated"
    folder.mkdir()
    (folder / "app.py").write_text(
        "import os, socket, urllib.request, urllib.error\n"
        "for url in ('http://not-allowed.example/', 'http://localhost/'):\n"
        "    try:\n        urllib.request.urlopen(url, timeout=5); print('open', url)\n"
        "    except urllib.error.HTTPError as e: print('gate', e.code, url)\n"
        "    except OSError as e: print('fail', url, e)\n"
        "try:\n    socket.create_connection(('1.1.1.1', 80), timeout=2); print('direct open')\n"
        "except OSError as e: print('direct blocked', type(e).__name__)\n"
        "print('proxy', os.environ.get('HTTPS_PROXY'))\n"
    )
    spec = {
        "name": "gated",
        "folder": str(folder),
        "command": ["app.py"],
        "network": {"traffic": {"mode": "hosts", "hosts": ["localhost", "pypi.org"]}},
    }
    with Client(daemon) as c:
        res = c.call("apply", spec=spec)
        c.call("approve", name="gated", hash=res["hash"])
        c.call("start", name="gated")
        lines = _run_to_end(c, "gated")
    assert "gate 403 http://not-allowed.example/" in lines, lines
    assert "gate 403 http://localhost/" in lines, lines  # allowed by name, refused by address: it's this computer
    assert "direct blocked OSError" in lines or any(x.startswith("direct blocked") for x in lines), lines
    assert "proxy http://127.0.0.1:3128" in lines


@needs_bwrap
def test_gate_listener_reaches_a_port_with_traffic_off(daemon, tmp_path):
    import socket

    from pythonator.service.client import Client

    folder = tmp_path / "srv"
    folder.mkdir()
    port = 18000 + os.getpid() % 1000
    (folder / "app.py").write_text(
        "import socket\n"
        f"s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(1); print('listening', flush=True)\n"
        "c, _ = s.accept(); c.sendall(b'hello from the jail'); c.close()\n"
    )
    spec = {"name": "srv", "folder": str(folder), "command": ["app.py"], "network": {"ports": [{"port": port}]}}
    with Client(daemon) as c:
        res = c.call("apply", spec=spec)
        c.call("approve", name="srv", hash=res["hash"])
        c.call("start", name="srv")
        for _ in range(100):
            if "listening" in [x[3] for x in c.call("logs", name="srv", count=20)["lines"]]:
                break
            time.sleep(0.1)
        for _ in range(50):  # the gate's listener opens with the start; give a loaded machine a moment
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except ConnectionRefusedError:
                time.sleep(0.1)
        with s:
            assert s.recv(100) == b"hello from the jail"
        _run_to_end(c, "srv")


@pytest.mark.skipif(sys.platform == "win32", reason="the real setup would run")
def test_wsl_setup_is_a_daemon_verb_and_explains_itself_off_windows(daemon):
    from pythonator.service import ipc
    from pythonator.service.client import Client

    with Client(daemon, timeout=5) as c, pytest.raises(ipc.RpcError) as e:
        c.call("wsl.setup")
    assert e.value.code == "RUN-WSL-001"


def test_a_long_job_never_holds_up_other_requests(tmp_path, monkeypatch):
    """WSL setup takes minutes: on the same connection the window must still get answers (open a file, start a
    service), see the job in the status bar, and be told if it asks for the same job twice."""
    import asyncio
    import dataclasses
    import json

    from pythonator.jail import wslsetup
    from pythonator.service import ipc
    from pythonator.service.client import Client
    from pythonator.service.daemon import Daemon

    release = threading.Event()

    def slow_setup(_wsl, _root, say):
        say("Installing Debian…")
        release.wait(20)
        return True, "ready"

    monkeypatch.setattr(wslsetup, "setup", slow_setup)
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    d = Daemon(root)
    d.host = d.rec.host = dataclasses.replace(d.host, os="windows", wsl="wsl.exe")
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=lambda: loop.run_until_complete(d.serve()), daemon=True)
    th.start()
    for _ in range(100):
        if ipc.endpoint_exists(root):
            break
        time.sleep(0.05)
    try:
        with Client(root, timeout=5) as c:
            c.call("subscribe", topics=["job"])
            c.sock.sendall(ipc.dumps({"id": 100, "method": "wsl.setup", "params": {}}))
            t0 = time.monotonic()
            assert c.call("ping")["version"]  # answered while the setup is still running
            assert time.monotonic() - t0 < 2
            assert [j["title"] for j in c.call("snapshot")["jobs"]] == ["WSL setup"]
            with pytest.raises(ipc.RpcError) as e:
                c.call("wsl.setup")
            assert e.value.code == "JOB-BUSY"
            release.set()
            while True:
                msg = json.loads(c.f.readline())
                c.events += [msg] if "event" in msg else []
                if msg.get("id") == 100:
                    assert msg["result"] == "ready"
                    break
            lines = [m["data"].get("line") for m in c.events if m.get("event") == "job"]
            assert "Installing Debian…" in lines
            assert c.call("snapshot")["jobs"] == []
    finally:
        release.set()
        loop.call_soon_threadsafe(d._stop.set)
        th.join(10)
