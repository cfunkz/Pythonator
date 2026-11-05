"""The window's connection to the background service. Starts the service when it isn't running."""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QLocalSocket

from ..paths import IS_WINDOWS, DataDirs, quiet_start, self_command
from .server import socket_name

Reply = Callable[[bool, Any, str], None]  # ok, data, error


class DaemonClient(QObject):
    message = pyqtSignal(dict)
    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(self, dirs: DataDirs, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.dirs = dirs
        self.sock = QLocalSocket(self)
        self.sock.readyRead.connect(self._read)
        self.sock.disconnected.connect(self._lost)
        self.sock.connected.connect(self._hello)
        self.sock.errorOccurred.connect(self._not_there)
        self._parts: list[bytes] = []  # a message not complete yet
        self._req = 0
        self._pending: dict[int, Reply] = {}
        self.ready = False
        self._spawned = False
        self._closing = False  # after shutdown() or close(): never reconnect, never start a new service
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self.connect_daemon)

    # -- connecting ---------------------------------------------------------------------------------------

    def connect_daemon(self) -> None:
        """Connect without blocking the window: ``connected`` or ``errorOccurred`` follows."""
        if self._closing:
            return
        self.sock.abort()
        self.sock.connectToServer(socket_name(self.dirs))

    def _not_there(self, _error: QLocalSocket.LocalSocketError) -> None:
        if self._closing or self.ready or self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            return
        if not self._spawned:
            self._spawned = True
            spawn_daemon()
        if not self._retry.isActive():
            self._retry.start(250)

    def _hello(self) -> None:
        try:
            token = self.dirs.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            self._retry.start(300)
            return
        self.request({"op": "hello", "token": token}, self._welcome)

    def _welcome(self, ok: bool, data: Any, error: str) -> None:
        if ok:
            self.ready = True
            self._spawned = False
            self.connected.emit()
        else:
            self._retry.start(1000)

    def _lost(self) -> None:
        self._parts.clear()  # half a message from the old connection
        was = self.ready
        self.ready = False
        for cb in self._pending.values():
            cb(False, None, "The background service went away")
        self._pending.clear()
        if was:
            self.disconnected.emit()
        if not self._closing:
            self._retry.start(1000)

    # -- messages -----------------------------------------------------------------------------------------

    def request(self, message: dict[str, Any], reply: Reply | None = None) -> None:
        self._req += 1
        message = {**message, "req": self._req}
        if reply is not None:
            self._pending[self._req] = reply
        if self.sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
            if reply is not None:
                self._pending.pop(self._req, None)
                reply(False, None, "Not connected to the background service")
            return
        self.sock.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")

    def _read(self) -> None:
        data = bytes(self.sock.readAll().data())
        if b"\n" not in data:  # a big message comes in many small reads: keep the pieces, search only the new one
            self._parts.append(data)
            return
        if self._parts:
            data = b"".join([*self._parts, data])
            self._parts.clear()
        *lines, rest = data.split(b"\n")  # once: splitting off one line at a time is quadratic
        if rest:
            self._parts.append(rest)
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("ev") == "reply":
                cb = self._pending.pop(int(msg.get("req", 0)), None)
                if cb is not None:
                    cb(bool(msg.get("ok")), msg.get("data"), str(msg.get("error", "")))
                elif not msg.get("ok") and msg.get("error"):
                    self.message.emit({"ev": "notice", "level": "error", "text": msg["error"]})
            else:
                self.message.emit(msg)

    def close(self) -> None:
        """Disconnect for good: no retries, no callbacks, and no background service started."""
        self._closing = True
        self._retry.stop()
        for sig in (self.sock.readyRead, self.sock.disconnected, self.sock.connected, self.sock.errorOccurred,
                    self._retry.timeout):
            with contextlib.suppress(TypeError, RuntimeError):
                sig.disconnect()
        self.sock.abort()
        self.ready = False
        self._pending.clear()

    def shutdown(self, stop_services: bool = True) -> None:
        """Ask the background service to quit. The connection then ends without starting it again."""
        self._closing = True
        self._retry.stop()
        self.request({"op": "shutdown", "stop_services": stop_services})
        self.sock.flush()
        self.sock.waitForBytesWritten(1000)


def spawn_daemon() -> None:
    """Start the background service detached from the window, so it outlives it."""
    argv = self_command("--daemon")
    flags = 0
    if IS_WINDOWS:
        # DETACHED_PROCESS only: a new process group would make every service ignore Ctrl+C, the polite stop.
        flags = 0x00000008
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, start_new_session=not IS_WINDOWS, close_fds=True, startupinfo=quiet_start())
