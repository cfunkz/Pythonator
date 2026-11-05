"""Renders plugin panels (the declarative design system of :mod:`pythonator.sdk`) with native widgets.

Plugins send data, never code or HTML: every string is shown as plain text, so a panel can't inject anything.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass
class _Item:
    """A drawn panel node and its widget, kept so the next redraw can reuse it."""

    node: dict[str, Any]
    widget: QWidget
    key: str
    slot: str = ""  # what it can be reused as when its node changes: the same text box, or any row
    children: list[_Item] = field(default_factory=list)  # a row's items


def _key(node: dict[str, Any]) -> str:
    return json.dumps(node, sort_keys=True, ensure_ascii=False, default=str)


def _slot(node: dict[str, Any]) -> str:
    kind = node.get("type")
    return f"input:{node.get('action', '')}" if kind == "input" else "row:" if kind == "row" else ""


def _update_input(widget: QWidget, node: dict[str, Any]) -> None:
    """Same text box (same action), maybe a new placeholder or button label: change those, keep the widget."""
    edit, button = widget.findChild(QPlainTextEdit), widget.findChild(QPushButton)
    if edit is not None:
        edit.setPlaceholderText(str(node.get("placeholder", "")))
    if button is not None:
        button.setText(str(node.get("label") or "Send"))


def reconcile(plugin: str, layout: QLayout, old: list[_Item], nodes: list[Any], on_action: Action,
              drafts: Drafts) -> list[_Item]:
    """Lay out ``nodes`` in ``layout``, reusing the widgets of ``old`` that still fit (like a list view's diff).

    An unchanged node keeps its widget, a text box with the same action is always kept (so a redraw never takes
    the user's typing, cursor or focus) and a row is diffed item by item. Only what changed is drawn again.
    """
    pool = list(old)

    def pick(want: str) -> _Item | None:  # by exact node, or by slot
        for i, item in enumerate(pool):
            if want in (item.key, item.slot):
                return pool.pop(i)
        return None

    out: list[_Item] = []
    for raw in nodes:
        node = raw if isinstance(raw, dict) else {}
        key, kind, slot = _key(node), node.get("type"), _slot(node)
        item = pick(key) or (pick(slot) if slot else None)
        if item is not None and kind == "input" and item.key != key:
            _update_input(item.widget, node)
        if item is None:
            widget = QWidget() if kind == "row" else render(plugin, node, on_action, drafts)
            if kind == "row":
                QHBoxLayout(widget).setContentsMargins(0, 0, 0, 0)
            item = _Item(node, widget, "", slot)
        if kind == "row" and item.key != key and (row := item.widget.layout()) is not None:
            item.children = reconcile(plugin, row, item.children, list(node.get("items", [])), on_action, drafts)
        item.node, item.key = node, key
        out.append(item)
    for item in pool:  # no longer in the panel
        item.widget.hide()
        item.widget.deleteLater()
    while layout.count():  # re-add in the new order; a widget taken out of a layout keeps its focus
        layout.takeAt(0)
    for item in out:
        layout.addWidget(item.widget)
    return out


class PanelDock(QTabWidget):
    """One tab per plugin panel. A redraw changes only what changed, so typing, focus and scroll stay put."""

    def __init__(self, on_action: Action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_action = on_action
        self.pages: dict[str, QScrollArea] = {}
        self.items: dict[str, list[_Item]] = {}
        self.last: dict[str, str] = {}
        self.drafts = Drafts()
        self.setDocumentMode(True)

    def update_panel(self, plugin: str, name: str, panel: dict[str, Any] | None) -> None:
        if panel is None:
            page = self.pages.pop(plugin, None)
            self.items.pop(plugin, None)
            self.last.pop(plugin, None)
            if page is not None:
                self.removeTab(self.indexOf(page))
                page.deleteLater()
            return
        same = _key(panel)
        if self.last.get(plugin) == same:  # plugins redraw on a timer; nothing changed, nothing to do
            return
        self.last[plugin] = same
        page = self.pages.get(plugin)
        if page is None:
            page = scroll()
            page.setFrameShape(QFrame.Shape.NoFrame)
            page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # a panel fits the dock's width
            page.setWidget(QWidget())
            QVBoxLayout(page.widget())
            self.pages[plugin] = page
            self.addTab(page, str(panel.get("title") or name))
        else:
            index = self.indexOf(page)
            title = str(panel.get("title") or name)
            if self.tabText(index) != title:
                self.setTabText(index, title)
        body = page.widget()
        layout = body.layout() if body is not None else None
        if body is None or layout is None:
            return
        bar = page.verticalScrollBar()
        at_end = bar is None or bar.value() >= bar.maximum() - 4
        position = bar.value() if bar is not None else 0
        self.items[plugin] = reconcile(plugin, layout, self.items.get(plugin, []), list(panel.get("items", [])),
                                       self.on_action, self.drafts)
        if isinstance(layout, QVBoxLayout):
            layout.addStretch(1)
        if bar is not None:  # a redraw keeps the place; at the end (a chat), it follows new lines
            body.adjustSize()
            bar.setValue(bar.maximum() if at_end else position)
