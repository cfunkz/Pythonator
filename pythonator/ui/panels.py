"""Renders plugin panels (the declarative design system of :mod:`pythonator.sdk`) with native widgets.

Plugins send data, never code or HTML: every string is shown as plain text, so a panel can't inject anything.
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QKeyEvent, QPalette, QResizeEvent, QTextDocument
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QBoxLayout,
    QComboBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .widgets import label

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


def _code_box(text: str) -> QPlainTextEdit:
    code = QPlainTextEdit(text)
    code.setReadOnly(True)
    code.setFont(theme.mono_font(9))
    code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    code.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
    _fit(code)
    return code


def _fit(code: QPlainTextEdit) -> None:
    lines = code.toPlainText().count("\n") + 1
    code.setFixedHeight(min(320, 24 + code.fontMetrics().lineSpacing() * lines))


Part = tuple[str, bool, bool]  # text, is code, is Markdown
Group = tuple[str, list[Part]]  # a bubble: role, its parts
CAPTIONS = {"you": "You", "ai": "Assistant", "error": "Error"}


MARKDOWN = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML


class Prose(QTextBrowser):
    """A message's text: wraps to the chat's width and is exactly as tall as its text, with no scroll bar of its
    own, so a long answer is laid out once per width instead of on every layout pass (as a word-wrapped QLabel is).

    With ``markdown`` it shows Markdown (headings, lists, bold, ``code``, tables): never HTML (shown as text),
    never images or files (nothing is loaded), and a link opens only when clicked, only http(s)."""

    def __init__(self, text: str = "", *, markdown: bool = False, tone: str = "text", small: bool = False) -> None:
        super().__init__()
        self.markdown = markdown
        self.shown: str | None = None
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(_open_link)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        doc = self.document()
        if doc is not None:
            doc.setDocumentMargin(1)
            if layout := doc.documentLayout():
                layout.documentSizeChanged.connect(lambda _s: self._fit())
        size = " font-size: 8pt;" if small else ""
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; border: none; color: {theme.color(tone)};"
                           f"{size} }}")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Link, QColor(theme.color("accent_hi")))
        self.setPalette(palette)
        if viewport := self.viewport():
            viewport.setAutoFillBackground(False)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        if text == self.shown:
            return
        self.shown = text
        doc = self.document()
        if self.markdown and doc is not None:
            doc.setMarkdown(text, MARKDOWN)
        else:
            self.setPlainText(text)
        self._fit()

    def text(self) -> str:
        return self.toPlainText()

    def loadResource(self, kind: int, name: QUrl) -> Any:
        return None  # never an image, a file or anything else the text names

    def resizeEvent(self, e: QResizeEvent | None) -> None:
        super().resizeEvent(e)
        self._fit()

    def _fit(self) -> None:
        doc = self.document()
        if doc is None:
            return
        viewport = self.viewport()
        width = max(40, viewport.width() if viewport is not None else self.width())
        if doc.textWidth() != width:
            doc.setTextWidth(width)
        height = math.ceil(doc.size().height()) + 2
        if height != self.height():
            self.setFixedHeight(height)


def _open_link(url: QUrl) -> None:
    if url.scheme() in ("http", "https"):
        QDesktopServices.openUrl(url)


class _Bubble(QFrame):
    """Consecutive messages of one role: a caption, then text and code blocks in turn."""

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role
        self.parts: list[Part] = []
        self.widgets: list[QWidget] = []
        self.setObjectName("bubble")
        tint = {"you": "accent", "error": "error"}.get(role)
        if role == "note":
            self.setStyleSheet("QFrame#bubble { background: transparent; border: none; }")
        else:
            fill = theme.rgba(tint, 0.13) if tint else theme.color("raised")
            edge = theme.rgba(tint, 0.4) if tint else theme.color("border")
            self.setStyleSheet(f"QFrame#bubble {{ background: {fill}; border: 1px solid {edge}; border-radius: 6px; }}")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*((2, 0, 2, 0) if role == "note" else (10, 6, 10, 8)))
        self.body.setSpacing(4)
        if role in CAPTIONS:
            caption = _plain(CAPTIONS[role])
            caption.setStyleSheet(f"color: {theme.color('error' if role == 'error' else 'muted')}; "
                                  "font-size: 8pt; font-weight: 600; background: transparent; border: none;")
            self.body.addWidget(caption)

    def show_parts(self, parts: list[Part]) -> None:
        """Update in place: unchanged parts keep their widget (and the user's text selection)."""
        keep = 0
        while keep < min(len(parts), len(self.parts)) and parts[keep][1:] == self.parts[keep][1:]:
            if parts[keep][0] != self.parts[keep][0]:
                self._set(self.widgets[keep], parts[keep][0])
            keep += 1
        for widget in self.widgets[keep:]:
            widget.hide()
            widget.deleteLater()
        self.widgets = self.widgets[:keep]
        for text, code, markdown in parts[keep:]:
            widget = _code_box(text) if code else self._prose(text, markdown)
            self.body.addWidget(widget)
            self.widgets.append(widget)
        self.parts = list(parts)

    def _prose(self, text: str, markdown: bool) -> Prose:
        tone = {"note": "muted", "error": "text"}.get(self.role, "text")
        return Prose(text, markdown=markdown and self.role == "ai", tone=tone, small=self.role == "note")

    @staticmethod
    def _set(widget: QWidget, text: str) -> None:
        if isinstance(widget, QPlainTextEdit):
            bar = widget.horizontalScrollBar()
            x = bar.value() if bar is not None else 0
            widget.setPlainText(text)
            _fit(widget)
            if bar is not None:
                bar.setValue(x)
        elif isinstance(widget, Prose):
            widget.set_text(text)


class ChatView(QScrollArea):
    """A chat: the user's bubbles on the right, the answers full width, notes small. It scrolls on its own and
    follows new text while scrolled to the end; scrolled up, it stays where the user is reading."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setObjectName("chat")
        self.setStyleSheet(f"QScrollArea#chat {{ border: 1px solid {theme.color('border')}; border-radius: 3px; "
                           f"background: {theme.color('bg')}; }}")
        inner = QWidget()
        inner.setObjectName("chatBody")
        inner.setStyleSheet(f"QWidget#chatBody {{ background: {theme.color('bg')}; }}")
        self.column = QVBoxLayout(inner)
        self.column.setContentsMargins(8, 8, 8, 8)
        self.column.setSpacing(8)
        self.column.addStretch(1)
        self.setWidget(inner)
        self.rows: list[tuple[QWidget, _Bubble]] = []
        self.follow = True
        bar = self.verticalScrollBar()
        if bar is not None:
            bar.valueChanged.connect(lambda v: setattr(self, "follow", v >= bar.maximum() - 4))
            bar.rangeChanged.connect(lambda _lo, hi: bar.setValue(hi) if self.follow else None)

    def set_messages(self, messages: list[Any]) -> None:
        groups: list[Group] = []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ("you", "ai", "note", "error"):
                continue
            part = (str(m.get("text", "")), bool(m.get("code")), bool(m.get("markdown")))
            if groups and groups[-1][0] == m["role"] and m["role"] != "note":
                groups[-1][1].append(part)
            else:
                groups.append((str(m["role"]), [part]))
        for i, (role, parts) in enumerate(groups):
            if i < len(self.rows) and self.rows[i][1].role != role:
                for row, _ in self.rows[i:]:
                    row.hide()
                    row.deleteLater()
                del self.rows[i:]
            if i >= len(self.rows):
                self.rows.append(self._row(role))
                self.column.insertWidget(self.column.count() - 1, self.rows[-1][0])
            if self.rows[i][1].parts != parts:
                self.rows[i][1].show_parts(parts)
        for row, _ in self.rows[len(groups):]:
            row.hide()
            row.deleteLater()
        del self.rows[len(groups):]

    @staticmethod
    def _row(role: str) -> tuple[QWidget, _Bubble]:
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(48 if role == "you" else 0, 0, 0, 0)  # the user's side
        bubble = _Bubble(role)
        line.addWidget(bubble)
        return row, bubble


def render(plugin: str, node: dict[str, Any], on_action: Action, drafts: Drafts | None = None) -> QWidget:
    kind = node.get("type")
    if kind == "text":
        text = _plain(str(node.get("text", "")))
        text.setStyleSheet(f"color: {_tone(str(node.get('tone', '')))}")
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return text
    if kind == "code":
        return _code_box(str(node.get("text", "")))
    if kind == "chat":
        chat = ChatView()
        chat.set_messages(list(node.get("messages", [])))
        return chat
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
        action, shown = str(node.get("action", "")), str(node.get("value", ""))

        def chosen(i: int) -> None:
            if 0 <= i < len(options):
                on_action(plugin, action, options[i])
            if shown in options:  # it shows what the plugin chose: its next panel says what that is now
                combo.setCurrentText(shown)

        combo.activated.connect(chosen)
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
        table = QTableWidget()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.ContiguousSelection)
        table.setWordWrap(False)
        vh = table.verticalHeader()
        if vh is not None:
            vh.hide()
            vh.setDefaultSectionSize(26)
            vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        hh = table.horizontalHeader()
        if hh is not None:
            # Interactive, sized once per change: ResizeToContents measures every cell on every resize step, which
            # made dragging a splitter or a window edge stutter with a long table in a panel.
            hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            hh.setResizeContentsPrecision(60)
            hh.setStretchLastSection(True)
        fill_table(table, node)
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


def fill_table(table: QTableWidget, node: dict[str, Any]) -> None:
    """Put a table node's rows into ``table``, reusing its cells: a panel that refreshes a status table every few
    seconds changes some text, not a whole new widget."""
    cols = [str(c) for c in node.get("columns", [])]
    rows = node.get("rows", [])
    tones = node.get("tones", [])
    table.setUpdatesEnabled(False)
    try:
        fresh = table.columnCount() != len(cols) or table.rowCount() == 0
        if table.columnCount() != len(cols):
            table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setRowCount(len(rows))
        default = QColor(theme.color("text"))
        for r, row in enumerate(rows):
            color = QColor(_tone(str(tones[r]))) if r < len(tones) else default
            cells = [str(c) for c in row[:len(cols)]] + [""] * max(0, len(cols) - len(row))
            for c, cell in enumerate(cells):
                item = table.item(r, c)
                if item is None:
                    item = QTableWidgetItem(cell)
                    table.setItem(r, c, item)
                elif item.text() != cell:
                    item.setText(cell)
                if item.foreground().color() != color:
                    item.setForeground(color)
                if len(cell) > 60:
                    item.setToolTip(cell[:2000])
        if fresh and rows:
            table.resizeColumnsToContents()
            hh = table.horizontalHeader()
            for c in range(max(0, len(cols) - 1)):
                if hh is not None and hh.sectionSize(c) > 320:
                    hh.resizeSection(c, 320)
        table.setFixedHeight(min(420, 32 + 26 * max(1, len(rows))))
    finally:
        table.setUpdatesEnabled(True)


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
    if kind == "input":
        return f"input:{node.get('action', '')}"
    if kind == "table":
        return "table:" + "\x1f".join(str(c) for c in node.get("columns", []))
    return f"{kind}:" if kind in ("row", "chat") else ""


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
        if item is not None and isinstance(item.widget, ChatView) and item.key != key:
            item.widget.set_messages(list(node.get("messages", [])))
        if item is not None and isinstance(item.widget, QTableWidget) and item.key != key:
            fill_table(item.widget, node)
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
        if isinstance(layout, QBoxLayout):
            layout.addWidget(item.widget, 1 if isinstance(item.widget, ChatView) else 0)  # a chat takes the height
        else:
            layout.addWidget(item.widget)
    return out


class PanelPage(QScrollArea):
    """One plugin's panel. A redraw changes only what changed, so typing, focus and scroll stay put."""

    def __init__(self, plugin: str, on_action: Action, drafts: Drafts | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plugin = plugin
        self.on_action = on_action
        self.drafts = drafts or Drafts()
        self.items: list[_Item] = []
        self.last = ""
        self.title = plugin
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # a panel fits its dock's width
        body = QWidget()
        body.setObjectName("PanelBody")
        QVBoxLayout(body).setContentsMargins(10, 8, 10, 10)
        self.setWidget(body)

    def show_panel(self, panel: dict[str, Any], name: str = "") -> bool:
        """Draw ``panel``; False when nothing changed (plugins redraw on a timer)."""
        same = _key(panel)
        self.title = str(panel.get("title") or name or self.plugin)
        if self.last == same:
            return False
        self.last = same
        body = self.widget()
        layout = body.layout() if body is not None else None
        if body is None or layout is None:
            return False
        bar = self.verticalScrollBar()
        at_end = bar is None or bar.value() >= bar.maximum() - 4
        position = bar.value() if bar is not None else 0
        body.setUpdatesEnabled(False)  # one repaint for the whole redraw, not one per changed widget
        try:
            self.items = reconcile(self.plugin, layout, self.items, list(panel.get("items", [])), self.on_action,
                                   self.drafts)
            if isinstance(layout, QVBoxLayout) and not any(isinstance(i.widget, ChatView) for i in self.items):
                layout.addStretch(1)
        finally:
            body.setUpdatesEnabled(True)
        if bar is not None:  # a redraw keeps the place; at the end (a chat), it follows new lines
            body.adjustSize()
            bar.setValue(bar.maximum() if at_end else position)
        return True


class _TitleBar(QWidget):
    """A panel dock's title: its name, ⧉ to open it in a window of its own (⇲ puts it back), ✕ to hide it.
    Dragging it moves the panel, to another side, into tabs with the others, or out on its own."""

    def __init__(self, dock: QDockWidget, docks: PanelDocks) -> None:
        super().__init__(dock)
        self.dock = dock
        self.setObjectName("PanelTitle")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 4, 3)
        row.setSpacing(2)
        self.name = QLabel(dock.windowTitle())
        self.name.setProperty("section", True)
        row.addWidget(self.name, 1)
        self.pop = QToolButton()
        self.pop.setAutoRaise(True)
        self.pop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pop.clicked.connect(lambda: docks.toggle_window(dock))
        close = QToolButton()
        close.setAutoRaise(True)
        close.setText("✕")
        close.setToolTip("Hide this panel (Plugins ▾ shows it again)")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(dock.close)
        row.addWidget(self.pop)
        row.addWidget(close)
        dock.windowTitleChanged.connect(self.name.setText)
        self.sync(locked=False)

    def sync(self, *, locked: bool) -> None:
        floating = self.dock.isFloating()
        self.pop.setText("⇲" if floating else "⧉")
        self.pop.setToolTip("Put it back in the main window" if floating else "Open in a window of its own")
        self.pop.setVisible(floating or not locked)


class PanelDocks(QObject):
    """Every plugin panel in a dock of its own. They start as tabs on the right of the main window; each can be
    dragged to another side, next to or into the others, or opened in a window of its own, and the layout is kept
    with the window's. Locked, they stay where they are (still tabs, still hidden and shown)."""

    changed = pyqtSignal()  # a panel came or went

    def __init__(self, window: QMainWindow, on_action: Action) -> None:
        super().__init__(window)
        self.window = window
        self.on_action = on_action
        self.drafts = Drafts()
        self.docks: dict[str, QDockWidget] = {}
        self.pages: dict[str, PanelPage] = {}
        self.locked = False
        window.setDockOptions(QMainWindow.DockOption.AllowTabbedDocks | QMainWindow.DockOption.AllowNestedDocks
                              | QMainWindow.DockOption.GroupedDragging | QMainWindow.DockOption.AnimatedDocks)
        window.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)

    def count(self) -> int:
        return len(self.docks)

    def update_panel(self, plugin: str, name: str, panel: dict[str, Any] | None) -> None:
        if panel is None:
            self.pages.pop(plugin, None)
            dock = self.docks.pop(plugin, None)
            if dock is not None:
                self.window.removeDockWidget(dock)
                dock.deleteLater()
                self.changed.emit()
            return
        page = self.pages.get(plugin)
        dock = self.docks.get(plugin)
        if page is None or dock is None:
            page = PanelPage(plugin, self.on_action, self.drafts)
            self.pages[plugin] = page
            dock = self._dock(plugin, page)
        if page.show_panel(panel, name) and dock.windowTitle() != page.title:
            dock.setWindowTitle(page.title)
        if plugin not in self.docks:
            self.docks[plugin] = dock
            self._place(dock)
            self.changed.emit()

    def _dock(self, plugin: str, page: PanelPage) -> QDockWidget:
        dock = QDockWidget(page.title, self.window)
        dock.setObjectName(f"panel-{plugin}")  # what saveState and restoreState know it by
        dock.setWidget(page)
        dock.setMinimumWidth(260)
        bar = _TitleBar(dock, self)
        dock.setTitleBarWidget(bar)
        dock.topLevelChanged.connect(lambda floating, d=dock: self._floated(d, floating))
        self._features(dock)
        return dock

    def _place(self, dock: QDockWidget) -> None:
        if self.window.restoreDockWidget(dock):  # where the user left it last time
            self._sync(dock)
            return
        others = [d for d in self.docks.values() if d is not dock and not d.isFloating()
                  and self.window.dockWidgetArea(d) == Qt.DockWidgetArea.RightDockWidgetArea]
        self.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        if others:
            self.window.tabifyDockWidget(others[0], dock)
        dock.hide()  # out of the way until asked for: the editor and console keep the width

    def _features(self, dock: QDockWidget) -> None:
        f = QDockWidget.DockWidgetFeature
        dock.setFeatures(f.DockWidgetClosable if self.locked else
                         f.DockWidgetClosable | f.DockWidgetMovable | f.DockWidgetFloatable)
        self._sync(dock)

    def _sync(self, dock: QDockWidget) -> None:
        bar = dock.titleBarWidget()
        if isinstance(bar, _TitleBar):
            bar.sync(locked=self.locked)

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        for dock in self.docks.values():
            self._features(dock)

    def show_all(self) -> None:
        """Every panel back in the main window, as tabs, the first one in front."""
        first: QDockWidget | None = None
        for dock in self.docks.values():
            if dock.isFloating():
                dock.setFloating(False)
            dock.show()
            if first is None:
                first = dock
            elif self.window.dockWidgetArea(dock) == self.window.dockWidgetArea(first):
                self.window.tabifyDockWidget(first, dock)
        if first is not None:
            first.raise_()

    def show(self, plugin: str) -> None:
        dock = self.docks.get(plugin)
        if dock is not None:
            dock.show()
            dock.raise_()
            if dock.isFloating():
                dock.activateWindow()

    def toggle_window(self, dock: QDockWidget) -> None:
        if dock.isFloating():
            dock.setFloating(False)
            dock.show()
            dock.raise_()
            return
        size = dock.size()
        dock.setFloating(True)
        dock.resize(max(size.width(), 480), max(size.height(), 560))
        dock.show()

    def _floated(self, dock: QDockWidget, floating: bool) -> None:
        if floating:
            QTimer.singleShot(0, lambda: self._as_window(dock))
        else:
            dock.setProperty("own_window", False)
        self._sync(dock)

    def _as_window(self, dock: QDockWidget) -> None:
        """A floating panel becomes a real window (its own taskbar entry, minimise and maximise), once the drag
        that floated it has ended: changing a window's kind during a drag would drop it."""
        with contextlib.suppress(RuntimeError):  # removed meanwhile
            if not dock.isFloating() or dock.property("own_window"):
                return
            if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
                QTimer.singleShot(120, lambda: self._as_window(dock))
                return
            geometry = dock.geometry()
            dock.setProperty("own_window", True)
            dock.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowTitleHint
                                | Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
            dock.setGeometry(geometry)
            dock.show()
            self._sync(dock)

    def floating(self) -> list[QDockWidget]:
        return [d for d in self.docks.values() if d.isFloating() and d.isVisible()]
