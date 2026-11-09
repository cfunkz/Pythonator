"""WSL mode's egress gate, run the way wslrun.py runs it inside Pythonator's distribution (needs root, Linux)."""

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent / "pythonator"
pytestmark = pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0, reason="needs root on Linux")


def test_gate_in_wsl_runner_blocks_signs_and_reloads(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for f in (ROOT / "jail" / "wslrun.py", ROOT / "jail" / "launcher.py", ROOT / "net" / "gate.py"):
        shutil.copy(f, lib / f.name)
    key = "ab" * 16
    script = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(lib)!r})
        import wslrun
        wslrun.LIB = {str(lib)!r}
        wslrun.GATE_DIR = {str(tmp_path / 'run')!r}
        sock = wslrun.start_gate("api", 1000, {{"hosts": ["allowed.example.com"], "allow_lan": False, "key": {key!r}}})
        print(sock, flush=True)
        time.sleep(4)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    sock = p.stdout.readline().strip()
    assert sock.endswith("/api/gate.sock")
    st = os.stat(sock)
    assert st.st_uid == 1000 and st.st_mode & 0o777 == 0o600

    def ask(host):
        c = socket.socket(socket.AF_UNIX)
        c.connect(sock)
        c.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        data = c.recv(200)
        c.close()
        return data

    assert b" 403 " in ask("evil.example.net")
    assert b" 403 " in ask("169.254.169.254")
    rule = tmp_path / "run" / "api" / "rule.json"
    assert json.loads(rule.read_text())["hosts"] == ["allowed.example.com"]
    sys.path.insert(0, str(lib))
    import wslrun

    wslrun.GATE_DIR = str(tmp_path / "run")
    wslrun.set_rule("api", json.dumps({"hosts": ["evil.example.net"]}))
    time.sleep(1.5)
    p.wait(10)
    err = p.stderr.read().split("\n")  # not splitlines(): it splits on \x1e too
    events = []
    for line in err:
        if not line:
            continue
        assert line.startswith("\x1epythonator-gate "), line
        sig, _, body = line[len("\x1epythonator-gate "):].partition(" ")
        assert hmac.compare_digest(sig, hmac.new(bytes.fromhex(key), body.encode(), hashlib.sha256).hexdigest())
        events.append(json.loads(body))
    assert [(e["host"], e["verdict"], e["why"]) for e in events] == [
        ("evil.example.net", "blocked", "not in the allowed hosts"),
        ("169.254.169.254", "blocked", "cloud metadata"),
    ]
