"""About (credits), Licensing, Support and System check: small dialogs behind the toolbar's buttons."""

from __future__ import annotations

import platform
from importlib import metadata
from typing import Any

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from . import theme
from .widgets import label

REPO = "https://github.com/cfunkz/Pythonator"
SITE = "https://cfunkz.github.io/Pythonator"
AUTHOR = "cfunkz"
DONATE = "https://ko-fi.com/cfunkz81112"
COMPONENTS = (  # (distribution, licence, what it does here)
    ("PyQt6", "GPL-3.0", "the window"), ("PyQt6-Qt6", "LGPL-3.0 (Qt)", "Qt itself"),
    ("PyQt6-QScintilla", "GPL-3.0", "the code editor"), ("psutil", "BSD-3-Clause", "process trees and stats"),
    ("pydantic", "MIT", "validated settings and messages"), ("pydantic-core", "MIT", "pydantic's engine"),
    ("platformdirs", "MIT", "data folders"), ("cronsim", "BSD-3-Clause", "schedules"),
)
SUPPORT = (
    ("Documentation", f"{SITE}/docs/", "How everything works, step by step"),
    ("FAQ", f"{SITE}/docs/faq.html", "Answers to common questions"),
    ("Ask a question", f"{REPO}/discussions", "GitHub Discussions"),
    ("Report a bug or ask for a feature", f"{REPO}/issues/new/choose", "GitHub Issues"),
    ("Report a security problem privately", f"{REPO}/security/advisories/new", "Not in public issues, please"),
    ("Buy me a coffee", DONATE, "Support development"),
)


def _version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "bundled"


def _table(headers: list[str], rows: list[list[str]]) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    vh = table.verticalHeader()
    if vh is not None:
        vh.hide()
    hh = table.horizontalHeader()
    if hh is not None:
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setStretchLastSection(True)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(cell))
    return table


def diagnostics() -> str:
    """Versions only, safe to paste into a bug report: no settings, paths or output."""
    return "\n".join((f"Pythonator {__version__}", f"OS: {platform.platform()} ({platform.machine()})",
                      f"Python {platform.python_version()} · Qt {QT_VERSION_STR} · PyQt {PYQT_VERSION_STR}"))


class AboutDialog(QDialog):
    """Who made it, what it is built on, and the versions for a bug report."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Pythonator")
        self.resize(520, 420)
        layout = QVBoxLayout(self)
        title = QLabel(f"<h2>Pythonator {__version__}</h2>")
        layout.addWidget(title)
        text = QLabel("Run and supervise long-running scripts, web servers and bots, in any language, sandboxed."
                      f"<p>Made by <b>{AUTHOR}</b>. "
                      f'<a href="{REPO}">{REPO}</a><br>Like it? <a href="{DONATE}">Buy me a coffee</a> '
                      "or star the repository.</p>"
                      "<p>Built with Qt, PyQt6 and QScintilla (Riverbank Computing), psutil, pydantic, "
                      "platformdirs and cronsim. Thanks to their authors.</p>")
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        layout.addWidget(text)
        diag = QLabel(diagnostics())
        diag.setStyleSheet(f"color: {theme.color('muted')}")
        diag.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(diag)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy diagnostics", QDialogButtonBox.ButtonRole.ActionRole)
        licensing = buttons.addButton("Licensing", QDialogButtonBox.ButtonRole.ActionRole)
        support = buttons.addButton("Support", QDialogButtonBox.ButtonRole.ActionRole)
        if copy and licensing and support:
            copy.clicked.connect(self._copy)
            licensing.clicked.connect(lambda: LicensingDialog(self).exec())
            support.clicked.connect(lambda: SupportDialog(self).exec())
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _copy() -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(diagnostics())


class LicensingDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Licensing")
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        head = label(f"Pythonator {__version__} is free software under the GNU General Public License v3.0 or "
                      "later. You may use, study, share and change it. The programs you run with Pythonator are "
                      "yours: their licence isn't affected.")
        layout.addWidget(head)
        layout.addWidget(QLabel("Bundled components:"))
        layout.addWidget(_table(["Component", "Version", "Licence", "Used for"],
                                [[d, _version(d), lic, use] for d, lic, use in COMPONENTS]), 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        full = QPushButton("Read the full licence")
        full.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"{REPO}/blob/main/LICENSE")))
        buttons.addButton(full, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SupportDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Support")
        self.resize(520, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Pythonator {__version__}. Help is a click away:"))
        for title, url, text in SUPPORT:
            button = QPushButton(title)
            button.setToolTip(url)
            button.clicked.connect(lambda _c=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            note = QLabel(text)
            note.setProperty("muted", True)
            layout.addWidget(button)
            layout.addWidget(note)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SystemCheckDialog(QDialog):
    """What Pythonator runs on, and what the sandbox enforces on this computer."""

    def __init__(self, data: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System check")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        icons = {"ok": "✓", "warn": "!", "error": "✗"}
        rows = [[icons.get(level, "•"), text] for level, text in data.get("app", [])]
        layout.addWidget(QLabel("Pythonator"))
        layout.addWidget(_table(["", "Check"], rows))
        layout.addWidget(QLabel("Sandbox on this computer"))
        caps = [["✓" if v == "enforced" else "•", k, v] for k, v in data.get("sandbox", [])]
        layout.addWidget(_table(["", "Protection", "Status"], caps), 1)
        note = label("“Not enforced” items are explained in the documentation, under Isolation modes, "
                     "with how to turn them on.")
        note.setProperty("muted", True)
        note.setStyleSheet(f"color: {theme.color('muted')}")
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
