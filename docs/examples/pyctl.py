"""pyctl: list, start, stop and follow Pythonator services from a terminal. Standard library only.

    python pyctl.py list
    python pyctl.py start "web api"
    python pyctl.py tail "web api"      # the log so far, then live output (Ctrl+C to end)
"""

import hashlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def data_folder() -> Path:
    if os.environ.get("PYTHONATOR_HOME"):
        return Path(os.environ["PYTHONATOR_HOME"]).resolve()
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"], "Pythonator").resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Pythonator"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Pythonator"


home = data_folder()


def socket_path() -> str:
    """daemon.sock in the data folder, unless that path is too long for a Unix socket (over 100 bytes)."""
    path = home / "daemon.sock"
    if len(os.fsencode(path)) <= 100:
        return str(path)
    name = hashlib.sha256(str(home).encode()).hexdigest()[:16] + ".sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    folders = [Path(runtime) / "pythonator"] if runtime.startswith("/") else []
    folders.append(Path(tempfile.gettempdir()) / f"pythonator-{os.getuid()}")
    return next((str(f / name) for f in folders if (f / name).exists()), str(path))


class Pythonator:
    def __init__(self) -> None:
        if sys.platform == "win32":  # a named pipe, only this user may open it
            name = "pythonator-" + hashlib.sha256(str(home).lower().encode()).hexdigest()[:16]
            self.file = open(rf"\\.\pipe\{name}", "r+b", buffering=0)  # noqa: PTH123, SIM115
        else:  # a Unix socket in the data folder, mode 0600
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(socket_path())
            self.file = sock.makefile("rwb")
        self.req = 0
        self.snapshot = self.call("hello", token=(home / "daemon.token").read_text().strip(), wait_for="snapshot")

    def send(self, op: str, **fields: object) -> int:
        self.req += 1
        self.file.write(json.dumps({"op": op, "req": self.req, **fields}).encode() + b"\n")
        self.file.flush()
        return self.req

    def events(self):  # every message the background service sends, as it sends it
        for line in self.file:
            yield json.loads(line)

    def call(self, op: str, wait_for: str = "", **fields: object) -> object:
        req = self.send(op, **fields)
        result = None
        for msg in self.events():
            if msg["ev"] == "reply" and msg["req"] == req:
                if not msg["ok"]:
                    raise SystemExit(f"{op}: {msg['error']}")
                result = msg.get("data")
                if not wait_for:
                    return result
            elif wait_for and msg["ev"] == wait_for:
                return msg
        return result


def main(args: list[str]) -> None:
    p = Pythonator()
    services = {s["name"]: s["id"] for s in p.snapshot["services"]}
    states = {st["id"]: st["state"] for st in p.snapshot["states"]}
    if not args or args[0] == "list":
        for name, sid in services.items():
            print(f"{states.get(sid, '?'):10} {name}")
    elif args[0] in ("start", "stop", "restart"):
        p.call(args[0], id=services[args[1]], **({"confirmed": False} if args[0] == "start" else {}))
        print(f"{args[0]}: {args[1]}")
    elif args[0] == "tail":
        sid = services[args[1]]
        print(p.call("tail", id=sid), end="")
        for msg in p.events():  # then follow, like tail -f
            if msg["ev"] == "output" and msg["id"] == sid:
                print(msg["text"], end="", flush=True)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
