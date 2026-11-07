"""Start the window. The window shows at once; it connects to the background service right after."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from .. import __version__, errors
from ..core.store import Store
from ..instance import InstanceLock
from ..paths import IS_WINDOWS, DataDirs, leave_current_folder
from . import theme

if TYPE_CHECKING:
    from .main_window import MainWindow


class _Notice(QObject):
    """Carries the "something went wrong" line to the status bar. An error on any thread emits it; Qt queues it to
    the window's thread, the only one that may touch a widget."""

    said = pyqtSignal(str)

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.target = window
        self.said.connect(self._show)

    def _show(self, text: str) -> None:
        self.target._status(text, 15000)


def window_socket(dirs: DataDirs) -> str:
    """Where the running window listens for a second start, to come to the front instead."""
    from ..daemon.server import socket_name

    name = socket_name(dirs)
    return f"{name}-window" if IS_WINDOWS else str(Path(name).with_name(f"{Path(name).stem}-window.sock"))


def wake_first(dirs: DataDirs) -> bool:
    """Ask the window already running to come to the front. True if it answered."""
    sock = QLocalSocket()
    sock.connectToServer(window_socket(dirs))
    if not sock.waitForConnected(1500):
        return False
    sock.write(b"show\n")
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _log_to(path: Path) -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    if not root.handlers:
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.WARNING)


def main(args: list[str]) -> int:
    from .login import HIDDEN

    hidden = HIDDEN in args  # started at sign-in
    app = QApplication([sys.argv[0], *(a for a in args if a != HIDDEN)])
    app.setApplicationName("Pythonator")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Pythonator")
    icon = Path(__file__).resolve().parent.parent / "resources" / "icon.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    dirs = DataDirs()
    dirs.ensure()
    _log_to(dirs.root / "window.log")
    errors.keep_running()  # a bug in one panel writes to window.log instead of closing the window
    leave_current_folder(dirs)  # Make reachable starts an elevated PowerShell from this process
    lock = InstanceLock(dirs.root / "window.lock")
    if not lock.acquire():  # one window per data folder: two would each ask every question, and run twice
        if not hidden:
            wake_first(dirs)
        return 0
    settings = Store(dirs).load_settings()  # read-only here: the theme before the first paint, and the tray
    theme.apply(app, settings.theme)

    from .main_window import MainWindow  # after the theme, so widgets pick it up

    window = MainWindow(dirs, settings)
    notice = _Notice(window)  # lives on the window's thread; a failed worker thread only emits to it
    errors.keep_running(notice.said.emit)
    woken = QLocalServer(app)
    woken.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(window_socket(dirs))
    woken.listen(window_socket(dirs))  # if it can't, a second start still only exits

    def wake() -> None:
        while (conn := woken.nextPendingConnection()) is not None:
            conn.disconnected.connect(conn.deleteLater)
            conn.disconnectFromServer()
            window.show_window()

    woken.newConnection.connect(wake)
    if not hidden:
        window.show()
    elif not settings.tray:  # no tray to wait in: minimised, so it is still on the taskbar
        window.showMinimized()
    try:
        return app.exec()
    finally:
        lock.release()
