"""What the installers run. Not commands for people.

* ``shutdown``: before an update replaces the app, stop the background service and the services it runs, so their
  files can be replaced. 0.0.9 understands the same word, so the Windows installer can stop either version.
* ``--uninstall``: the same, then on Windows take each sandbox's entries off the folders it was given and delete
  its AppContainer profile. Settings, logs and the services' own files stay, as with any uninstaller.

Only QtCore and QtNetwork are used: nothing opens a window.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

from ..paths import FROZEN, IS_WINDOWS, DataDirs
from .server import socket_name


def stop_daemon(dirs: DataDirs, timeout: float = 60.0) -> int:
    """Ask a running background service to stop everything and quit; 0 once none is left running."""
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtNetwork import QLocalSocket

    try:
        token = dirs.token_file.read_text(encoding="utf-8").strip()  # only there while it runs
    except OSError:
        return 0
    _app = QCoreApplication.instance() or QCoreApplication([])
    sock = QLocalSocket()
    sock.connectToServer(socket_name(dirs))
    if not sock.waitForConnected(3000):
        return 0
    hello = {"op": "hello", "token": token, "req": 1}
    for message in (hello, {"op": "shutdown", "stop_services": True, "req": 2}):
        sock.write(json.dumps(message).encode() + b"\n")
    sock.flush()
    sock.waitForBytesWritten(3000)
    deadline = time.monotonic() + timeout
    while sock.state() != QLocalSocket.LocalSocketState.UnconnectedState and time.monotonic() < deadline:
        sock.waitForReadyRead(250)
        sock.readAll()
    return 0 if sock.state() == QLocalSocket.LocalSocketState.UnconnectedState else 1


def end_strays(timeout: float = 10.0) -> list[int]:
    """Uninstalling: end every other process of this installed Pythonator.exe still running (a window, a plugin
    worker or sandbox launcher a crashed background service left behind), or Windows can't delete the file."""
    if not FROZEN:
        return []  # sys.executable is a Python then: never touch other programs
    import psutil

    exe = Path(sys.executable).resolve()
    strays = []
    for proc in psutil.process_iter(["exe"]):
        with contextlib.suppress(psutil.Error, OSError):
            if proc.pid != os.getpid() and proc.info["exe"] and Path(proc.info["exe"]).resolve() == exe:
                proc.kill()
                strays.append(proc)
    psutil.wait_procs(strays, timeout=timeout)
    return [p.pid for p in strays]


def main(flag: str) -> int:
    dirs = DataDirs()
    code = stop_daemon(dirs)
    if flag == "--uninstall" and IS_WINDOWS:
        from ..isolation import windows

        end_strays()
        windows.forget_all(dirs)
    return code
