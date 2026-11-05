"""Start the window. The window shows at once; it connects to the background service right after."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .. import __version__
from ..core.store import Store
from ..paths import DataDirs
from . import theme


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
    settings = Store(dirs).load_settings()  # read-only here: the theme before the first paint, and the tray
    theme.apply(app, settings.theme)

    from .main_window import MainWindow  # after the theme, so widgets pick it up

    window = MainWindow(dirs, settings)
    if not hidden:
        window.show()
    elif not settings.tray:  # no tray to wait in: minimised, so it is still on the taskbar
        window.showMinimized()
    return app.exec()
