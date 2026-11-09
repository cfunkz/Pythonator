"""Clients for pythonatord: a blocking one for the CLI and tests. The window has its own on the Qt loop (ui/link.py)."""

from __future__ import annotations

import itertools
import json
import socket
from pathlib import Path
from typing import Any

from pythonator.service import ipc


class Client:
    def __init__(self, root: Path, timeout: float = 30.0) -> None:
        self.key = ipc.load_key(root)
        if ipc.use_tcp():
            self.sock = socket.create_connection(("127.0.0.1", ipc.read_port(root)), timeout=timeout)
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(str(ipc.socket_path(root)))
        self.f = self.sock.makefile("rb")
        self._ids = itertools.count(1)
        self.events: list[dict[str, Any]] = []
        hello = self._read()
        n_s, n_c = hello["hello"], ipc.nonce()
        self.sock.sendall(ipc.dumps({"nonce": n_c, "proof": ipc.proof(self.key, b"client", n_s, n_c)}))
        reply = self._read()
        if not ipc.check(self.key, b"server", n_c, n_s, reply.get("proof")):
            self.close()
            raise ConnectionError("the background service didn't prove it is Pythonator's")
        self.version = hello.get("version", "")

    def _read(self) -> dict[str, Any]:
        raw = self.f.readline(ipc.MAX_LINE)
        if not raw:
            raise ConnectionError("pythonatord closed the connection")
        return json.loads(raw)

    def call(self, method: str, **params: Any) -> Any:
        rid = next(self._ids)
        self.sock.sendall(ipc.dumps({"id": rid, "method": method, "params": params}))
        while True:
            msg = self._read()
            if "event" in msg:
                self.events.append(msg)
                continue
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                e = msg["error"]
                raise ipc.RpcError(e.get("code", "?"), e.get("message", ""), e.get("fix", ""))
            return msg.get("result")

    def close(self) -> None:
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
