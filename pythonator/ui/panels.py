"""Renders plugin panels (the declarative design system of :mod:`pythonator.sdk`) with native widgets.

Plugins send data, never code or HTML: every string is shown as plain text, so a panel can't inject anything.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QColor, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .widgets import label, scroll

Action = Callable[[str, str, str], None]  # plugin, action, value
MAX_INPUT = 16_000


class Drafts:
    """What the user typed in panels' text boxes, kept while the plugin redraws its panel."""

    def __init__(self) -> None:
        self.text: dict[tuple[str, str], str] = {}
        self.focused: tuple[str, str] | None = None


class _EnterSends(QObject):
    """Enter sends, Shift+Enter is a new line."""

    def __init__(self, box: QPlainTextEdit, send: Callable[[], None]) -> None:
        super().__init__(box)
        self.send = send

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress \
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.send()
            return True
        return False


def _tone(tone: str) -> str:
    return {"ok": theme.color("ok"), "warn": theme.color("warn"), "error": theme.color("error"),
            "muted": theme.color("muted")}.get(tone, theme.color("text"))


def _plain(text: str) -> QLabel:
    return label(text, plain=True)


def render(plugin: str, node: dict[str, Any], on_action: Action, drafts: Drafts | None = None) -> QWidget:
    kind = node.get("type")
    if kind == "text":
        text = _plain(str(node.get("text", "")))
        text.setStyleSheet(f"color: {_tone(str(node.get('tone', '')))}")
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return text
    if kind == "code":
        code = QPlainTextEdit(str(node.get("text", "")))
        code.setReadOnly(True)
        code.setFont(theme.mono_font(9))
        code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lines = str(node.get("text", "")).count("\n") + 1
        code.setFixedHeight(min(320, 24 + code.fontMetrics().lineSpacing() * lines))
        return code
    if kind == "input":
        return _input(plugin, node, on_action, drafts or Drafts())
    if kind == "select":
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        if node.get("label"):
            layout.addWidget(_plain(str(node["label"])))
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)  # long options don't widen the panel
        options = [str(o) for o in node.get("options", [])]
        combo.addItems(options)
        if str(node.get("value", "")) in options:
            combo.setCurrentText(str(node.get("value", "")))
        action = str(node.get("action", ""))
        combo.activated.connect(lambda i: on_action(plugin, action, options[i]) if 0 <= i < len(options) else None)
        layout.addWidget(combo, 1)
        return box
    if kind == "stat":
        card = QFrame()
        card.setProperty("card", True)
        stack = QVBoxLayout(card)
        name = _plain(str(node.get("label", "")))
        name.setProperty("muted", True)
        value = _plain(str(node.get("value", "")))
        value.setStyleSheet(f"font-size: 15pt; font-weight: 600; color: {_tone(str(node.get('tone', '')))}")
        stack.addWidget(name)
        stack.addWidget(value)
        return card
    if kind == "table":
        cols = [str(c) for c in node.get("columns", [])]
        rows = node.get("rows", [])
        tones = node.get("tones", [])
        table = QTableWidget(len(rows), len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        vh = table.verticalHeader()
        if vh is not None:
            vh.hide()
        hh = table.horizontalHeader()
        if hh is not None:
            hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            hh.setStretchLastSection(True)
        for r, row in enumerate(rows):
            color = QColor(_tone(str(tones[r]))) if r < len(tones) else None
            for c, cell in enumerate(row[:len(cols)]):
                item = QTableWidgetItem(str(cell))
                if color is not None:
                    item.setForeground(color)
                table.setItem(r, c, item)
        table.setMinimumHeight(min(400, 36 + 28 * len(rows)))
        return table
    if kind == "button":
        button = QPushButton(str(node.get("label", "")))
        action, arg = str(node.get("action", "")), str(node.get("value", ""))
        button.clicked.connect(lambda: on_action(plugin, action, arg))
        return button
    if kind == "row":
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for child in node.get("items", []):
            layout.addWidget(render(plugin, child, on_action, drafts))
        return box
    return _plain("")


def _input(plugin: str, node: dict[str, Any], on_action: Action, drafts: Drafts) -> QWidget:
    action = str(node.get("action", ""))
    key = (plugin, action)
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    edit = QPlainTextEdit()
    edit.setPlaceholderText(str(node.get("placeholder", "")))
    edit.setFixedHeight(edit.fontMetrics().lineSpacing() * 3 + 16)
    edit.setPlainText(drafts.text.get(key, ""))
    edit.moveCursor(edit.textCursor().MoveOperation.End)
    button = QPushButton(str(node.get("label") or "Send"))
    button.setProperty("primary", True)

    def changed() -> None:
        drafts.text[key] = edit.toPlainText()

    def send() -> None:
        text = edit.toPlainText().strip()[:MAX_INPUT]
        if text:
            drafts.text.pop(key, None)
            edit.clear()
            on_action(plugin, action, text)

    def focus(_old: QWidget | None, new: QWidget | None) -> None:
        if new is edit:
            drafts.focused = key
        elif drafts.focused == key and new is not None:
            drafts.focused = None

    edit.textChanged.connect(changed)
    edit.installEventFilter(_EnterSends(edit, send))
    button.clicked.connect(send)
    app = QApplication.instance()
    if isinstance(app, QApplication):
        app.focusChanged.connect(focus)
        edit.destroyed.connect(lambda *_: _disconnect(app, focus))
    if drafts.focused == key:
        edit.setFocus()
    layout.addWidget(edit, 1)
    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignBottom)
    return box


def _disconnect(app: QApplication, slot: Callable[..., None]) -> None:
    with contextlib.suppress(TypeError, RuntimeError):
        app.focusChanged.disconnect(slot)


class PanelDock(QTabWidget):
    """One tab per plugin panel."""

    def __init__(self, on_action: Action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_action = on_action
        self.pages: dict[str, QScrollArea] = {}
        self.drafts = Drafts()
        self.setDocumentMode(True)

    def update_panel(self, plugin: str, name: str, panel: dict[str, Any] | None) -> None:
        if panel is None:
            page = self.pages.pop(plugin, None)
            if page is not None:
                self.removeTab(self.indexOf(page))
                page.deleteLater()
            return
        page = self.pages.get(plugin)
        if page is None:
            page = scroll()
            page.setFrameShape(QFrame.Shape.NoFrame)
            page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # a panel fits the dock's width
            self.pages[plugin] = page
            self.addTab(page, str(panel.get("title") or name))
        bar = page.verticalScrollBar()
        at_end = bar is None or bar.value() >= bar.maximum() - 4
        position = bar.value() if bar is not None else 0
        body = QWidget()
        layout: QLayout = QVBoxLayout(body)
        for node in panel.get("items", []):
            layout.addWidget(render(plugin, node, self.on_action, self.drafts))
        if isinstance(layout, QVBoxLayout):
            layout.addStretch(1)
        old = page.takeWidget()
        page.setWidget(body)
        if old is not None:
            old.deleteLater()
        if bar is not None:  # a redraw keeps the place; at the end (a chat), it follows new lines
            body.adjustSize()
            bar.setValue(bar.maximum() if at_end else position)
