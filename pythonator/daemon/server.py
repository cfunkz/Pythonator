"""The background service: keeps services running while no window is open, and talks to windows over a local socket.

The socket lives in the user's private data folder (a named pipe for the current user on Windows); a window must
present the token from ``daemon.token`` (0600) first, compared in constant time. Only QtCore is used here.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from .. import __version__, isolation
from ..core.envcheck import app_report
from ..core.store import Store, write_atomic
from ..instance import InstanceLock
from ..isolation import private_node
from ..paths import IS_WINDOWS, DataDirs, leave_current_folder
from ..plugins.host import PluginHost
from . import protocol as p
from .supervisor import NeedsConfirmation, Supervisor

log = logging.getLogger(__name__)


SUN_PATH = 100  # a Unix socket's path must fit in sun_path: 104 bytes on macOS, 108 on Linux


def socket_name(dirs: DataDirs) -> str:
    """The background service's address: a pipe name on Windows, a socket path elsewhere."""
    if IS_WINDOWS:  # a pipe name; QLocalServer's UserAccessOption limits it to this user
        return "pythonator-" + hashlib.sha256(str(dirs.root).lower().encode()).hexdigest()[:16]
    path = dirs.root / "daemon.sock"
    if len(os.fsencode(path)) <= SUN_PATH:
        return str(path)
    # A data folder this deep (a long PYTHONATOR_HOME, macOS temp folders) can't hold a socket. Use a private
    # folder with a short path, and name the socket after the data folder so each one still has its own.
    name = hashlib.sha256(str(dirs.root).encode()).hexdigest()[:16] + ".sock"
    for folder in _short_folders():
        if _private_folder(folder) and len(os.fsencode(folder / name)) <= SUN_PATH:
            return str(folder / name)
    return str(path)  # nothing fits: listen() reports the error


def _short_folders() -> list[Path]:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    folders = [Path(runtime) / "pythonator"] if runtime.startswith("/") else []
    return [*folders, Path(tempfile.gettempdir()) / f"pythonator-{os.getuid()}"]


def _private_folder(folder: Path) -> bool:
    """``folder`` exists, is a real folder (not a link) and only this user can use it; made if missing."""
    with contextlib.suppress(FileExistsError, OSError):
        folder.mkdir(mode=0o700)
    try:
        st = folder.lstat()
    except OSError:
        return False
    # Someone else's folder, or one others can write to, could hand the window a socket that collects its token.
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0


class Client:
    def __init__(self, sock: QLocalSocket) -> None:
        self.sock = sock
        self.buf = b""
        self.authed = False
        self.skipping = False
        self.watch: str | None = None  # the one service whose output it wants; None: all


class Daemon:
    def __init__(self, dirs: DataDirs) -> None:
        # Set first: Qt may call back into this object (a socket dropping) at any point after it exists.
        self.clients: list[Client] = []
        self.quitting = False
        self.dirs = dirs
        dirs.ensure()
        leave_current_folder(dirs)
        self.token = secrets.token_urlsafe(32)  # written once this daemon listens: never over a live one's
        self.store = Store(dirs)
        self.sup = Supervisor(dirs, self.store)
        private_node.prepare_ahead(dirs)  # Windows: Node.js for sandboxes, before the first start
        self.plugins = PluginHost(dirs, self.sup)
        self.sup.listeners.append(self.broadcast)
        self.server = QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.setMaxPendingConnections(16)
        self.server.newConnection.connect(self._accept)
        self.timer = QTimer()
        self.timer.timeout.connect(self.sup.tick)

    def listen(self) -> bool:
        name = socket_name(self.dirs)
        if serving(self.dirs):
            return False  # another daemon serves this data folder
        QLocalServer.removeServer(name)
        if not self.server.listen(name):
            log.error("can't listen on %s: %s", name, self.server.errorString())
            return False
        write_atomic(self.dirs.token_file, self.token)
        return True

    def run(self) -> None:
        reaped = self.sup.reap_orphans()
        if reaped:
            log.warning("ended %d processes left by a crashed earlier run", reaped)
        self.timer.start(50)
        self.sup.autostart()
        self.plugins.start_enabled()

    # -- connections --------------------------------------------------------------------------------------

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if sock is None:
                return
            client = Client(sock)
            self.clients.append(client)
            sock.readyRead.connect(lambda c=client: self._read(c))
            sock.disconnected.connect(lambda c=client: self._drop(c))
            # A timer owned by the socket, so it goes with the socket instead of firing at a closed daemon.
            deadline = QTimer(sock)
            deadline.setSingleShot(True)
            deadline.timeout.connect(lambda c=client: None if c.authed else self._drop(c))
            deadline.start(5000)

    def _drop(self, client: Client) -> None:
        if client in self.clients:
            self.clients.remove(client)
            _release(client.sock)

    def _read(self, client: Client) -> None:
        client.buf += bytes(client.sock.readAll().data())
        if len(client.buf) > p.MAX_FRAME and b"\n" not in client.buf:
            self._drop(client)
            return
        *lines, client.buf = client.buf.split(b"\n")  # one split, however many lines arrived
        for line in lines:
            if line.strip():
                self._handle(client, line)
            if client not in self.clients:
                return

    def _send(self, client: Client, message: dict[str, Any]) -> None:
        if client.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            client.sock.write(p.encode(message))

    def broadcast(self, event: dict[str, Any]) -> None:
        data = p.encode(event)
        output = event.get("ev") == "output"
        for client in list(self.clients):
            if not client.authed:
                continue
            if output and client.watch is not None and event.get("id") != client.watch:
                continue
            backlog = client.sock.bytesToWrite()
            if backlog > p.MAX_BACKLOG:
                self._drop(client)
                continue
            if output and backlog > p.MAX_QUEUED:
                if not client.skipping:
                    client.skipping = True
                    client.sock.write(p.encode({"ev": "notice", "level": "warn",
                                                "text": "The window can't keep up; showing the newest output only"}))
                continue
            client.skipping = False
            client.sock.write(data)

    # -- requests -----------------------------------------------------------------------------------------

    def _handle(self, client: Client, line: bytes) -> None:
        try:
            req = p.parse(line)
        except ValueError as exc:
            if not client.authed:
                self._drop(client)
            else:
                self._send(client, {"ev": "reply", "req": _req_of(line), "ok": False, "error": str(exc)})
            return
        if not client.authed:
            if isinstance(req, p.Hello) and hmac.compare_digest(req.token.encode(), self.token.encode()):
                client.authed = True
                self._send(client, {"ev": "reply", "req": req.req, "ok": True,
                                    "data": {"version": __version__, "protocol": p.PROTOCOL}})
                self._send(client, self.sup.snapshot())
                self._send(client, {"ev": "plugins", "plugins": self.plugins.describe()})
                for plugin in self.plugins.plugins.values():
                    if plugin.panel:
                        self._send(client, {"ev": "panel", "plugin": plugin.id, "name": plugin.manifest.name,
                                            "panel": plugin.panel})
                for event in self.plugins.pending_proposals():
                    self._send(client, event)
            else:
                self._drop(client)
            return
        reply: dict[str, Any] = {"ev": "reply", "req": req.req, "ok": True}
        if isinstance(req, p.Watch):
            client.watch = req.id
            client.skipping = False
            self._send(client, reply)
            return
        try:
            data = self._dispatch(req)
            if data is not None:
                reply["data"] = data
        except NeedsConfirmation:
            reply.update(ok=False, error="confirm_full")
        except (KeyError, ValueError, OSError, isolation.IsolationError) as exc:
            reply.update(ok=False, error=str(exc).strip("'\""))
        except Exception as exc:  # never let one request take the daemon down
            log.exception("request failed")
            reply.update(ok=False, error=f"Internal error: {exc}")
        self._send(client, reply)

    def _dispatch(self, req: p.Req) -> Any:
        s, pl = self.sup, self.plugins
        match req:
            case p.Add(service=service):
                s.add(service.model_copy(update={"made_by": None}))  # only Pythonator itself marks what it made
            case p.Update(service=service):
                s.update(service)
            case p.Remove(id=sid, files=True):
                s.delete_project(sid)
            case p.Remove(id=sid):
                s.remove(sid)
            case p.DeleteLeftovers(folders=folders):
                s.delete_leftovers(list(folders))
            case p.Start(id=sid, confirmed=confirmed):
                s.start(sid, confirmed=confirmed)
            case p.Stop(id=sid):
                s.stop(sid)
            case p.Restart(id=sid):
                s.restart(sid)
            case p.Input(id=sid, text=text):
                s.write(sid, text)
            case p.Shell(id=sid, line=line):
                s.shell(sid, line)
            case p.Install(id=sid):
                s.install(sid)
            case p.Recreate(id=sid):
                s.recreate(sid)
            case p.Pull(id=sid):
                s.pull(sid)
            case p.Compose(id=sid, action=action):
                s.compose(sid, action)
            case p.Tail(id=sid):
                return s.tail(sid)
            case p.Clear(id=sid):
                s.clear(sid)
            case p.Check(id=sid):
                return s.check(sid)
            case p.SaveSettings(settings=settings):
                keep = {k: getattr(s.settings, k) for k in ("plugins_enabled", "plugin_settings", "plugin_grants",
                                                             "plugin_live")}
                s.save_settings(settings.model_copy(update=keep))  # plugin state changes only through its ops
            case p.Reorder(ids=ids):
                s.reorder(list(ids))
            case p.StartAll(ids=ids):
                s.start_all(None if ids is None else list(ids))
            case p.Simple(op="stop_all"):
                s.stop_all()
            case p.Simple(op="snapshot"):
                return s.snapshot()
            case p.Simple(op="capabilities"):
                return {"sandbox": isolation.capabilities(), "app": app_report(self.dirs), "services": s.pids()}
            case p.Simple(op="ping"):
                return "pong"
            case p.Shutdown(stop_services=stop):
                QTimer.singleShot(100, lambda: self.quit(stop))
            case p.PluginEnable(plugin=pid, enabled=on):
                pl.set_enabled(pid, on)
            case p.PluginSettings(plugin=pid, values=values):
                pl.set_values(pid, values)
            case p.PluginGrant(plugin=pid, permissions=perms, live=live):
                pl.grant(pid, perms, live)
            case p.PluginAction(plugin=pid, action=action, value=value):
                pl.action(pid, action, value)
            case p.PluginProposal(plugin=pid, proposal=proposal, apply=apply):
                return pl.decide(pid, proposal, apply)
            case p.PluginInstall(source=source):
                return pl.install(source)
            case p.PluginRemove(plugin=pid):
                pl.remove(pid)
            case _:
                raise ValueError("Unknown request")
        return None

    def quit(self, stop_services: bool = True) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.close(stop_services=stop_services)
        app = QCoreApplication.instance()
        if app is not None:
            app.quit()

    def close(self, stop_services: bool = True) -> None:
        """Stop serving and let go of every Qt object and thread, so nothing calls back into this object later."""
        self.timer.stop()
        _disconnect(self.timer.timeout)
        # Stop serving first: a window that starts a new background service meanwhile finds nobody listening, and
        # that one waits for this one to finish instead of leaving the window waiting for a service that is going.
        _disconnect(self.server.newConnection)
        self.server.close()
        for client in self.clients:
            _release(client.sock)
        self.clients.clear()
        self.plugins.shutdown()
        if stop_services:
            self.sup.shutdown()
        self.sup.close()
        self.sup.listeners.clear()
        with contextlib.suppress(OSError):
            self.dirs.token_file.unlink()


def serving(dirs: DataDirs) -> bool:
    """Whether a background service answers on this data folder's socket."""
    probe = QLocalSocket()
    probe.connectToServer(socket_name(dirs))
    if probe.waitForConnected(500):
        probe.abort()
        return True
    return False


def _disconnect(sig: Any) -> None:
    with contextlib.suppress(TypeError, RuntimeError):  # nothing connected, or the object is already gone
        sig.disconnect()


def _release(sock: QLocalSocket) -> None:
    _disconnect(sock.readyRead)
    _disconnect(sock.disconnected)
    sock.abort()
    sock.deleteLater()


def _req_of(line: bytes) -> int:
    """The request number of a message that didn't validate, so its sender still gets its answer."""
    try:
        req = json.loads(line).get("req", 0)
    except (ValueError, AttributeError):
        return 0
    return req if isinstance(req, int) and 0 <= req < 2**53 else 0


def main() -> int:
    from logging.handlers import RotatingFileHandler

    dirs = DataDirs()
    dirs.ensure()
    handlers: list[logging.Handler] = [RotatingFileHandler(dirs.root / "daemon.log", maxBytes=2_000_000,
                                                           backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    app = QCoreApplication(sys.argv[:1])
    app.setApplicationName("Pythonator")
    lock = InstanceLock(dirs.root / "daemon.lock")
    deadline = time.monotonic() + 30
    while not lock.acquire():
        # Another background service has this data folder. Serving: it is the one (two windows started one each).
        # Not serving: it is shutting down (stopping services takes a while), so this one takes over after it.
        if serving(dirs) or time.monotonic() > deadline:
            return 0
        time.sleep(0.25)
    daemon = Daemon(dirs)
    if not daemon.listen():
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: daemon.quit(True))
    keepalive = QTimer()  # lets Python handle signals while Qt waits
    keepalive.timeout.connect(lambda: None)
    keepalive.start(250)
    daemon.run()
    return app.exec()
