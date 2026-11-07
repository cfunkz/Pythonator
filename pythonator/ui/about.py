"""About (credits), Licensing, Support and System check: small dialogs behind the toolbar's buttons."""

from __future__ import annotations

import contextlib
import platform
import re
import sys
import threading
from importlib import metadata
from pathlib import Path
from typing import Any

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import sysinfo
from ..isolation import leftovers
from . import theme
from .widgets import ask, info, label, scroll, warn

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


class _Found(QObject):
    """Brings the slow part of the system check (each runtime's version) back to the window's thread."""

    done = pyqtSignal(object, object)


def _section(title: str) -> QLabel:
    head = QLabel(title.upper())
    head.setProperty("section", True)
    head.setContentsMargins(2, 10, 0, 2)
    return head


def _card() -> tuple[QFrame, QGridLayout]:
    card = QFrame()
    card.setObjectName("CheckCard")
    card.setStyleSheet(f"QFrame#CheckCard {{ background: {theme.color('panel')}; border: 1px solid "
                       f"{theme.color('border')}; border-radius: {theme.R_MD}px; }} "
                       "QFrame#CheckCard QLabel { background: transparent; border: none; }")
    grid = QGridLayout(card)
    grid.setContentsMargins(12, 8, 12, 8)
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(5)
    grid.setColumnStretch(2, 1)
    return card, grid


def _private(text: str) -> str:
    """``text`` with your home folder shown as ``~``: a program's path would otherwise show your user name."""
    home = str(Path.home())
    if len(home) < 3:
        return text
    return re.sub(re.escape(home), "~", text, flags=re.IGNORECASE if sys.platform == "win32" else 0)


def _breakable(text: str) -> str:
    """Lets a long path wrap after each folder (a zero-width space after every slash) instead of widening the
    whole dialog past its window."""
    return re.sub(r"([\\/])(?=\S)", "\\1\u200b", text)


def _wrapping(widget: QLabel, full: str) -> QLabel:
    """A label that wraps inside its column and never makes the dialog wider; the whole text is its tooltip."""
    widget.setMinimumWidth(1)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if len(full) > 60:
        widget.setToolTip(full)
    return widget


def _row(grid: QGridLayout, mark: str, tone: str, name: str, value: str, note: str = "") -> None:
    r = grid.rowCount()
    sign = QLabel(mark)
    sign.setFixedWidth(14)
    sign.setStyleSheet(f"color: {theme.color(tone)}; font-weight: 600")
    key = QLabel(name)
    key.setStyleSheet(f"color: {theme.color('muted')}")
    full = _private(value + (f"  ·  {note}" if note else ""))
    text = _wrapping(label(_breakable(full), plain=True), full)
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    grid.addWidget(sign, r, 0, Qt.AlignmentFlag.AlignTop)
    grid.addWidget(key, r, 1, Qt.AlignmentFlag.AlignTop)
    grid.addWidget(text, r, 2)


TONE = {"ok": ("✓", "ok"), "warn": ("!", "warn"), "error": ("✗", "error")}


class SystemCheckDialog(QDialog):
    """What Pythonator runs on: this computer, Pythonator itself, the runtimes and container engines it found,
    and what the sandbox enforces here. Every line is filled; the slow part (versions) arrives a moment later."""

    def __init__(self, data: dict[str, Any], parent: QWidget | None = None, data_folder: Path | None = None,
                 names: dict[str, str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System check")
        self.names = dict(names or {})  # service id → name, for the leftovers
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(760, 640)
        self.data = data
        self.report: list[str] = [diagnostics()]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        info = sysinfo.facts(data_folder)
        computer = dict(info["This computer"])
        headline = QLabel(computer.get("System", ""))
        headline.setStyleSheet("font-size: 15pt; font-weight: 600")
        sub = label(f"{computer.get('Processor', '')}  ·  {computer.get('Cores', '')}  ·  "
                    f"{computer.get('Memory', '')} memory", plain=True, muted=True)
        outer.addWidget(headline)
        outer.addWidget(sub)
        sandbox = [(k, v) for k, v in data.get("sandbox", [])]
        enforced = sum(1 for _k, v in sandbox if v == "enforced")
        problems = [t for level, t in data.get("app", []) if level in ("warn", "error")]
        summary = (f"Sandbox: {enforced} of {len(sandbox)} protections enforced on this computer"
                   + (f"  ·  {len(problems)} thing{'s' if len(problems) != 1 else ''} to look at" if problems else
                      "  ·  nothing to fix"))
        badge = self.badge = label(summary, plain=True)
        self.summary = summary
        self._tone(badge, "warn" if problems or enforced < len(sandbox) else "ok")
        outer.addWidget(badge)
        body = QWidget()
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(0, 0, 6, 0)
        self.column.setSpacing(4)
        area = scroll()
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(body)
        outer.addWidget(area, 1)
        for title, rows in info.items():
            self._facts(title, rows)
        self._pythonator(data)
        self._sandbox(sandbox)
        self.column.addWidget(_section("Runtimes and container engines"))
        self.tools_card, self.tools_grid = _card()
        _row(self.tools_grid, "…", "muted", "Looking", "Finding the runtimes on PATH and their versions…")
        self.column.addWidget(self.tools_card)
        self.column.addWidget(_section("Left behind by services"))
        self.left_card, _grid = _card()
        _row(_grid, "…", "muted", "Looking", "Firewall rules, containers and listening processes of removed services…")
        self.column.addWidget(self.left_card)
        self.left_items: list[leftovers.Leftover] = []
        self._left = _Found(self)
        self._left.done.connect(self._leftovers)
        self._look_left()
        note = label("“Not enforced” protections are explained in the documentation, under Isolation modes, with how "
                     "to turn them on. Host and user names are never shown or copied.", plain=True, muted=True)
        note.setMinimumWidth(1)
        note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.column.addWidget(note)
        self.column.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy report", QDialogButtonBox.ButtonRole.ActionRole)
        if copy is not None:
            copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._found = _Found(self)
        self._found.done.connect(self._tools)
        found = self._found

        def look() -> None:
            result = sysinfo.tools()
            with contextlib.suppress(RuntimeError):  # the window closed meanwhile
                found.done.emit(*result)

        threading.Thread(target=look, daemon=True, name="system-check").start()

    @staticmethod
    def _tone(badge: QLabel, tone: str) -> None:
        badge.setStyleSheet(f"color: {theme.color(tone)}; background: {theme.rgba(tone, 0.10)}; border: 1px solid "
                            f"{theme.rgba(tone, 0.35)}; border-radius: {theme.R_MD}px; padding: 6px 10px;")

    def _facts(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.column.addWidget(_section(title))
        card, grid = _card()
        for name, value in rows:
            _row(grid, "•", "muted", name, value)
            self.report.append(f"{name}: {value}")
        self.column.addWidget(card)

    def _pythonator(self, data: dict[str, Any]) -> None:
        self.column.addWidget(_section("Pythonator"))
        card, grid = _card()
        _row(grid, "✓", "ok", "Version", f"Pythonator {__version__}")
        _row(grid, "✓", "ok", "Built with", f"Qt {QT_VERSION_STR} · PyQt {PYQT_VERSION_STR}")
        for level, text in data.get("app", []):
            mark, tone = TONE.get(level, ("•", "muted"))
            _row(grid, mark, tone, {"ok": "Runs on", "warn": "Note", "error": "Problem"}.get(level, ""), text)
            self.report.append(f"[{level}] {text}")
        self.column.addWidget(card)

    def _sandbox(self, caps: list[tuple[str, str]]) -> None:
        self.column.addWidget(_section("Sandbox on this computer"))
        card, grid = _card()
        if not caps:
            _row(grid, "!", "warn", "Sandbox", "Not reported by the background service")
        for name, status in caps:
            ok = status == "enforced"
            _row(grid, "✓" if ok else "•", "ok" if ok else "warn", name, status)
            self.report.append(f"{name}: {status}")
        self.column.addWidget(card)

    def _tools(self, found: list[sysinfo.Tool], engines: list[sysinfo.Tool]) -> None:
        with contextlib.suppress(RuntimeError):  # closed before the versions came
            card, grid = _card()
            for tool in [*found, *engines]:
                if tool.blocked:
                    _row(grid, "!", "warn", tool.name, tool.version or "found",
                         f"sandboxed services can't run it from {tool.blocked}")
                    share = QPushButton("Let sandboxes use it…")
                    share.setToolTip("One administrator prompt: every sandbox may then read and run the programs in "
                                     "this folder (never change them), as Windows allows for C:\\Program Files")
                    share.clicked.connect(lambda _c=False, f=tool.blocked: self._share(f))
                    grid.addWidget(share, grid.rowCount() - 1, 3)
                elif tool.path:
                    _row(grid, "✓", "ok", tool.name, tool.version or "found", tool.note or tool.path)
                else:
                    _row(grid, "–", "faint", tool.name, "not found on PATH")
                self.report.append(f"{tool.name}: {tool.version or ('found' if tool.path else 'not found')}"
                                   + (f" ({tool.note})" if tool.note else ""))
            self.column.replaceWidget(self.tools_card, card)
            self.tools_card.deleteLater()
            self.tools_card, self.tools_grid = card, grid

    def _services(self) -> dict[str, list[int]]:
        raw = self.data.get("services")
        out: dict[str, list[int]] = {}
        for sid, pids in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(sid, str) and isinstance(pids, list):
                out[sid] = [int(p) for p in pids if isinstance(p, int) and p > 0]
        return out

    def _look_left(self) -> None:
        bridge, services = self._left, self._services()

        def look() -> None:
            found = leftovers.find(services)
            with contextlib.suppress(RuntimeError):  # the window closed meanwhile
                bridge.done.emit(found, None)

        threading.Thread(target=look, daemon=True, name="leftovers").start()

    def _leftovers(self, found: leftovers.Found, _unused: object = None) -> None:
        with contextlib.suppress(RuntimeError):
            card, grid = _card()
            kinds = {"reach": "Make reachable", "container": "Container", "process": "Process"}
            for item in found.items:
                _row(grid, "!", "warn", kinds.get(item.kind, item.kind), item.what, leftovers.origin(item, self.names))
                self.report.append(f"[leftover] {item.what}, {leftovers.origin(item, self.names)}")
            for note in found.notes:
                _row(grid, "–", "faint", "Not checked", note)
                self.report.append(f"[leftover] not checked: {note}")
            if not found.items:
                _row(grid, "✓", "ok", "Nothing", "No firewall rules, containers or processes left listening by a "
                                                   "removed or stopped service")
            else:
                clean = QPushButton("Clean up…")
                clean.setToolTip("Stops the leftover containers and processes and, on Windows, removes the firewall "
                                 "rules (one administrator prompt)")
                clean.clicked.connect(self._clean_left)
                grid.addWidget(clean, grid.rowCount(), 2, Qt.AlignmentFlag.AlignRight)
            self.left_items = list(found.items)
            if found.items:
                n = len(found.items)
                self.badge.setText(f"{self.summary.removesuffix('  ·  nothing to fix')}  ·  {n} thing"
                                   f"{'s' if n != 1 else ''} left behind by services")
                self._tone(self.badge, "warn")
            else:
                self.badge.setText(self.summary)
            self.column.replaceWidget(self.left_card, card)
            self.left_card.deleteLater()
            self.left_card = card

    def _clean_left(self) -> None:
        items = list(self.left_items)
        if not items:
            return
        reach = [i.key for i in items if i.kind == "reach"]
        lines = "\n".join(f"• {i.what}" for i in items[:12]) + (f"\n…and {len(items) - 12} more"
                                                               if len(items) > 12 else "")
        admin = "\n\nWindows asks for administrator rights once, for the firewall rules." if reach else ""
        if ask(self, "Clean up", f"Stop and remove these?\n\n{lines}{admin}") != QMessageBox.StandardButton.Yes:
            return
        bridge = _Found(self)

        def done(ok: object, message: object) -> None:
            (info if ok else warn)(self, "Clean up", str(message))
            self._look_left()  # what is left now

        bridge.done.connect(done)

        def work() -> None:  # waits for the administrator prompt and the engine, off the window's thread
            from ..isolation import windows

            errors = leftovers.clean(items)
            if reach:
                ok, message = windows.unreach(reach)
                if not ok:
                    errors.append(message)
            text = ("Couldn't do all of it:\n" + "\n".join(errors)) if errors else "Done: nothing is left behind."
            with contextlib.suppress(RuntimeError):
                bridge.done.emit(not errors, text)

        threading.Thread(target=work, daemon=True, name="clean-leftovers").start()

    def _share(self, folder: str) -> None:
        from ..isolation import windows

        bridge = _Found(self)
        bridge.done.connect(lambda ok, message: (info if ok else warn)(
            self, "Sandboxes", str(message)))

        def work() -> None:  # waits for the administrator prompt, off the window's thread
            ok, message = windows.share_program_folder(Path(folder))
            with contextlib.suppress(RuntimeError):
                bridge.done.emit(ok, message)

        threading.Thread(target=work, daemon=True, name="share-folder").start()

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(self.report))
