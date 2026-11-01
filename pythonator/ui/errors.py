"""Last line of defence: unexpected exceptions are logged and reported, never fatal.

PyQt6 aborts the whole process when a slot raises and ``sys.excepthook`` is the default one,
which would take every supervised service down with the window. Here every unhandled
exception (GUI thread or worker thread) is written to ``pythonator.log`` and shown once in
a while, and the app keeps running.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

log = logging.getLogger("pythonator")

DIALOG_INTERVAL = 30.0  # seconds between error dialogs, so a failing timer can't flood the screen


def setup_logging(log_file: Path) -> None:
    """Send the app's own log (not the services') to a small rotating file."""
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()  # read-only folder: stderr is better than nothing
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class ErrorReporter(QObject):
    """Installs the exception hooks; shows a non-blocking dialog for errors on any thread."""

    _report = pyqtSignal(str, str)  # (summary, details), delivered on the GUI thread

    def __init__(self, log_file: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.log_file = log_file
        self.count = 0
        self._last_dialog = 0.0
        self._box: QMessageBox | None = None
        self._report.connect(self._show)  # queued automatically when emitted from another thread
        self._previous = sys.excepthook, threading.excepthook

    def install(self) -> None:
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook

    def uninstall(self) -> None:
        sys.excepthook, threading.excepthook = self._previous

    def _sys_hook(self, kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        if issubclass(kind, KeyboardInterrupt):
            self._previous[0](kind, value, tb)
            return
        self.handle(value, tb, where="the window")

    def _thread_hook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None or issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "a background thread"
        self.handle(args.exc_value, args.exc_traceback, where=name)

    def handle(self, exc: BaseException, tb: TracebackType | None, *, where: str) -> None:
        """Log ``exc`` and report it (safe to call from any thread)."""
        self.count += 1
        details = "".join(traceback.format_exception(type(exc), exc, tb))
        log.error("Unhandled error in %s:\n%s", where, details.rstrip())
        summary = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        with contextlib.suppress(RuntimeError):  # reporter already deleted during shutdown
            self._report.emit(summary, details)

    def _show(self, summary: str, details: str) -> None:
        now = time.monotonic()
        if self._box is not None and self._box.isVisible():
            return  # one at a time
        if now - self._last_dialog < DIALOG_INTERVAL:
            return
        self._last_dialog = now
        parent = QApplication.activeWindow()
        box = QMessageBox(parent if isinstance(parent, QWidget) else None)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Pythonator hit an error")
        box.setText("Something went wrong inside Pythonator. Your services keep running.")
        box.setInformativeText(f"{summary}\n\nDetails were saved to {self.log_file}")
        box.setDetailedText(details)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setModal(False)
        box.show()
        self._box = box
        # Close stale dialogs eventually so a tray-only session doesn't accumulate them.
        QTimer.singleShot(int(DIALOG_INTERVAL * 1000 * 4), box.close)
