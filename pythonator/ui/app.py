"""Application bootstrap: QApplication, theme, single-instance lock and the main window."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

from PyQt6.QtCore import QEvent, QLockFile, QObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from .. import __version__
from ..core.login import MINIMIZED
from ..core.macos import disable_app_nap
from ..core.paths import APP_NAME, IS_MAC, DataPaths, data_dir
from ..core.safety import elevation
from ..core.userenv import completed_path, raise_open_file_limit
from ..supervisor import Supervisor
from . import theme
from .errors import ErrorReporter, setup_logging
from .main_window import TRAY, MainWindow
from .prompts import confirm_elevated, confirm_exposed_autostart


class QuitRouter(QObject):
    """Sends every request to quit through the window's clean shutdown.

    On macOS ⌘Q, Dock → Quit and logging out arrive as an application quit request, not as a
    window close; without this, Qt would end the app with services still running (or, with the
    window hidden in the menu bar, quit without stopping them). The window stops the services,
    saves its state and then lets the quit go ahead; if the user cancels (unsaved files), the
    quit is refused.
    """

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if event is not None and event.type() == QEvent.Type.Quit and not self._window.closed:
            self._window.quit(from_system=True)
            if not self._window.closed:
                event.ignore()
                return True
        return False


def _show_on_reactivate(app: QApplication, window: MainWindow, quiet_until: float) -> None:
    """macOS: clicking the Dock icon (or ⌘Tab to the app) brings back a window hidden in the menu bar."""

    def on_state(state: Qt.ApplicationState) -> None:
        if (state == Qt.ApplicationState.ApplicationActive and window.isHidden() and not window.closed
                and time.monotonic() > quiet_until):
            window.show_window()

    app.applicationStateChanged.connect(on_state)


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    minimized = MINIMIZED in argv
    argv = [a for a in argv if a != MINIMIZED]
    # Opened from the Finder, Dock or a login item, macOS gives apps a bare PATH without Homebrew,
    # nvm, pyenv, ... Ask the login shell for the real one while Qt starts up.
    found_path: list[str | None] = [None]
    path_thread = threading.Thread(target=lambda: found_path.__setitem__(0, completed_path()),
                                   name="pythonator-path", daemon=True)
    path_thread.start()
    if IS_MAC:
        raise_open_file_limit()  # macOS starts apps with room for only 256 open files
    if sys.platform == "win32":
        # Own taskbar group/icon instead of python.exe's.
        import ctypes  # noqa: PLC0415

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"cfunkz.pythonator.{__version__}")

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(theme.app_icon())
    theme.apply_theme(app)

    try:
        paths = DataPaths(data_dir()).ensure()
    except OSError as exc:
        QMessageBox.critical(
            None, APP_NAME,
            f"{APP_NAME} can't create its data folder:\n{exc}\n\n"
            "Make the folder writable, or set PYTHONATOR_HOME to another folder.",
        )
        return 2
    log_file = paths.logs / "pythonator.log"
    setup_logging(log_file)
    reporter = ErrorReporter(log_file, app)
    reporter.install()
    lock = QLockFile(str(paths.lock))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if not minimized:  # started at login while already open: nothing to say
            QMessageBox.warning(
                None, APP_NAME,
                f"{APP_NAME} is already running with this data folder:\n{paths.root}\n\n"
                f"Look for its icon in the {TRAY}.",
            )
        return 1
    if IS_MAC:
        disable_app_nap()  # hidden in the menu bar, it must still restart and schedule on time

    path_thread.join(8.0)  # nothing may start before PATH is complete
    if new_path := found_path[0]:
        os.environ["PATH"] = new_path

    supervisor = Supervisor(paths)
    settings = supervisor.config.settings
    if (who := elevation()) and not settings.allow_elevated:
        go_on, never_again = confirm_elevated(who)
        if not go_on:
            supervisor.shutdown()
            lock.unlock()
            reporter.uninstall()
            return 0
        if never_again:
            settings.allow_elevated = True
            supervisor.save()
    window = MainWindow(supervisor)
    app.installEventFilter(QuitRouter(window))
    if IS_MAC:
        _show_on_reactivate(app, window, time.monotonic() + (3.0 if minimized else 0.0))
    if minimized and window.tray is not None:
        window.hide()  # started at login: straight to the tray / menu bar
    else:
        window.show()
    autostart = [s for s in supervisor.services() if s.config.autostart]
    if not autostart or not paths.exposed or confirm_exposed_autostart(paths.exposed, len(autostart)):
        supervisor.autostart()

    # Let Ctrl+C in a terminal close the app cleanly (Python only sees signals between Qt events).
    signal.signal(signal.SIGINT, lambda *_: window.quit())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)

    code = app.exec()
    lock.unlock()
    reporter.uninstall()
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
