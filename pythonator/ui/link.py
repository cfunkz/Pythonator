"""The window's connection to pythonatord, on the Qt event loop (never blocks the window).
Same protocol and mutual HMAC proof as the CLI client. If the background service isn't running, the window starts
it; closing the window never stops it."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QLocalSocket, QTcpSocket

from pythonator.service import ipc

Callback = Callable[[Any, dict[str, Any] | None], None]  # (result, error)

class Link(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    message = pyqtSignal(str, dict)

    def __init__(self, root: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.tcp = ipc.use_tcp()  # Windows: 127.0.0.1 + HMAC; elsewhere a Unix socket in an owner-only folder
        self.sock: QLocalSocket | QTcpSocket = QTcpSocket(self) if self.tcp else QLocalSocket(self)
        self.sock.readyRead.connect(self._read)
        self.sock.disconnected.connect(self._lost)
        self.sock.errorOccurred.connect(self._error)
        self._buf = b""
        self._stage = "closed"  # closed -> hello -> proof -> ready
        self._nonce_s = self._nonce_c = ""
        self._next = 1
        self._pending: dict[int, Callback] = {}
        self._queue: list[bytes] = []
        self._spawned = False
        self.daemon_version = ""
        self._retry = QTimer(self, singleShot=True, interval=300)
        self._retry.timeout.connect(self.open)

    @property
    def ready(self) -> bool:
        return self._stage == "ready"

    def open(self) -> None:
        idle = (
            self.sock.state() == QAbstractSocket.SocketState.UnconnectedState
            if isinstance(self.sock, QTcpSocket)
            else self.sock.state() == QLocalSocket.LocalSocketState.UnconnectedState
        )
        if not idle:
            return
        self._stage = "hello"
        self._buf = b""
        if isinstance(self.sock, QTcpSocket):
            try:
                port = ipc.read_port(self.root)
            except FileNotFoundError:
                self._error(None)
                return
            self.sock.connectToHost("127.0.0.1", port)
        else:
            self.sock.connectToServer(str(ipc.socket_path(self.root)))

    def _error(self, _err: object) -> None:
        if self._stage in ("hello", "closed") and not self._spawned:
            self._spawned = True
            ipc.spawn_daemon()
        self._stage = "closed"
        self._retry.start()

    def _lost(self) -> None:
        was = self._stage == "ready"
        self._stage = "closed"
        for cb in self._pending.values():
            cb(None, {"code": "IPC-LOST", "message": "The background service went away.", "fix": ""})
        self._pending.clear()
        if was:
            self.disconnected.emit()
        self._retry.start()

    def _send(self, obj: dict[str, Any]) -> None:
        self.sock.write(ipc.dumps(obj))

    def _read(self) -> None:
        self._buf += bytes(self.sock.readAll())
        if len(self._buf) > ipc.MAX_LINE * 2:
            self.sock.abort()
            return
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self._handle(msg)

    def _handle(self, msg: dict[str, Any]) -> None:
        if self._stage == "hello":
            key = ipc.load_key(self.root)
            self._nonce_s, self._nonce_c = str(msg.get("hello", "")), ipc.nonce()
            self.daemon_version = str(msg.get("version", ""))
            self._send({"nonce": self._nonce_c, "proof": ipc.proof(key, b"client", self._nonce_s, self._nonce_c)})
            self._stage = "proof"
            return
        if self._stage == "proof":
            key = ipc.load_key(self.root)
            if not ipc.check(key, b"server", self._nonce_c, self._nonce_s, msg.get("proof")):
                self.sock.abort()  # not our daemon
                return
            self._stage = "ready"
            for q in self._queue:
                self.sock.write(q)
            self._queue.clear()
            self.connected.emit()
            return
        if "event" in msg:
            self.message.emit(str(msg["event"]), msg.get("data") or {})
            return
        cb = self._pending.pop(msg.get("id", -1), None)
        if cb:
            cb(msg.get("result"), msg.get("error"))

    def call(self, method: str, cb: Callback | None = None, **params: Any) -> None:
        rid = self._next
        self._next += 1
        if cb:
            self._pending[rid] = cb
        data = ipc.dumps({"id": rid, "method": method, "params": params})
        if self.ready:
            self.sock.write(data)
        else:
            self._queue.append(data)
