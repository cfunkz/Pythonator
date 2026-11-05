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
    app = QApplication([sys.argv[0], *args])
    app.setApplicationName("Pythonator")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Pythonator")
    icon = Path(__file__).resolve().parent.parent / "resources" / "icon.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    dirs = DataDirs()
    dirs.ensure()
    settings = Store(dirs).load_settings()  # read-only here: only the theme, before the first paint
    theme.apply(app, settings.theme)

    from .main_window import MainWindow  # after the theme, so widgets pick it up

    window = MainWindow(dirs)
    window.show()
    return app.exec()
