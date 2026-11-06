"""Renders plugin panels (the declarative design system of :mod:`pythonator.sdk`) with native widgets.

Plugins send data, never code or HTML: every string is shown as plain text, so a panel can't inject anything.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QKeyEvent, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QBoxLayout,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLayoutItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
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


def markdown_html(text: str) -> str:
    """Markdown as HTML that is safe to show: the text's own HTML is kept as text (MarkdownNoHTML) and images are
    dropped, so nothing is ever loaded; what is left is headings, lists, emphasis, code, tables and links."""
    doc = QTextDocument()
    doc.setMarkdown(text, MARKDOWN)
    images: list[tuple[int, int]] = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and fragment.charFormat().isImageFormat():
                images.append((fragment.position(), fragment.length()))
            it += 1
        block = block.next()
    cursor = QTextCursor(doc)
    for start, length in reversed(images):
        cursor.setPosition(start)
        cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
    return doc.toHtml()


def _open_link(url: QUrl) -> None:
    if url.scheme() in ("http", "https"):
        QDesktopServices.openUrl(url)


class ChatText(QLabel):
    """A message's text, wrapped to the chat's width (a label is always as tall as its text). An answer asked as
    Markdown shows formatted, through :func:`markdown_html`; a link opens in the browser on a click, http(s) only."""

    def __init__(self, *, markdown: bool = False) -> None:
        super().__init__()
        self.markdown = markdown
        self.source = ""
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.RichText if markdown else Qt.TextFormat.PlainText)
        self.setOpenExternalLinks(False)
        self.linkActivated.connect(lambda url: _open_link(QUrl(url)))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard
                                     | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # wraps to the chat's width

    def set_text(self, text: str) -> None:
        if text != self.source:
            self.source = text
            self.setText(markdown_html(text) if self.markdown else text)


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

    def _prose(self, text: str, markdown: bool) -> ChatText:
        w = ChatText(markdown=markdown and self.role == "ai")
        tone = {"note": "muted", "error": "text"}.get(self.role, "text")
        small = " font-size: 8pt;" if self.role == "note" else ""
        w.setStyleSheet(f"color: {theme.color(tone)}; background: transparent; border: none;{small}")
        w.set_text(text)
        return w

    @staticmethod
    def _set(widget: QWidget, text: str) -> None:
        if isinstance(widget, QPlainTextEdit):
            bar = widget.horizontalScrollBar()
            x = bar.value() if bar is not None else 0
            widget.setPlainText(text)
            _fit(widget)
            if bar is not None:
                bar.setValue(x)
        elif isinstance(widget, ChatText):
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


class FlowRow(QLayout):
    """A panel's row: its items side by side, sharing the width as a box layout does, and wrapping onto the next
    line when the panel is too narrow for them, so nothing is ever cut off at the edge of the dock."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item: QLayoutItem | None) -> None:
        if item is not None:
            self.entries.append(item)

    def count(self) -> int:
        return len(self.entries)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self.entries.pop(index) if 0 <= index < len(self.entries) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self) -> QSize:
        hints = [i.sizeHint() for i in self.entries if not i.isEmpty()]
        width = sum(h.width() for h in hints) + self.spacing() * max(0, len(hints) - 1)
        return QSize(width, max((h.height() for h in hints), default=0))

    def minimumSize(self) -> QSize:
        sizes = [i.minimumSize() for i in self.entries if not i.isEmpty()]
        return QSize(max((m.width() for m in sizes), default=0), max((m.height() for m in sizes), default=0))

    def _place(self, rect: QRect, *, move: bool) -> int:
        gap, width = self.spacing(), max(1, rect.width())
        items = [i for i in self.entries if not i.isEmpty()]
        lines: list[list[QLayoutItem]] = []
        used = 0
        for item in items:  # greedy: as many as fit at their preferred width
            need = min(width, item.sizeHint().width())
            if lines and used + gap + need <= width:
                lines[-1].append(item)
                used += gap + need
            else:
                lines.append([item])
                used = need
        y = rect.y()
        for line in lines:
            hints = [min(width, i.sizeHint().width()) for i in line]
            extra = max(0, width - sum(hints) - gap * (len(line) - 1)) // len(line)
            widths = [h + extra for h in hints]
            height = max(i.heightForWidth(w) if i.hasHeightForWidth() else i.sizeHint().height()
                         for i, w in zip(line, widths, strict=True))
            x = rect.x()
            for item, w in zip(line, widths, strict=True):
                if move:
                    item.setGeometry(QRect(QPoint(x, y), QSize(w, height)))
                x += w + gap
            y += height + gap
        return max(0, y - rect.y() - gap)


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
        flow = FlowRow(box)
        for child in node.get("items", []):
            flow.addWidget(render(plugin, child, on_action, drafts))
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
                if hh is not None and hh.sectionSize(c) > 220:  # a long cell elides; its tooltip has it all
                    hh.resizeSection(c, 220)
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
                FlowRow(widget)
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


class PanelDock(QTabWidget):
    """One tab per plugin panel, in the window's Plugins dock."""

    def __init__(self, on_action: Action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_action = on_action
        self.pages: dict[str, PanelPage] = {}
        self.drafts = Drafts()
        self.setDocumentMode(True)

    def update_panel(self, plugin: str, name: str, panel: dict[str, Any] | None) -> None:
        page = self.pages.get(plugin)
        if panel is None:
            if page is not None:
                del self.pages[plugin]
                self.removeTab(self.indexOf(page))
                page.deleteLater()
            return
        if page is None:
            page = self.pages[plugin] = PanelPage(plugin, self.on_action, self.drafts)
            self.addTab(page, plugin)
        page.show_panel(panel, name)
        index = self.indexOf(page)
        if self.tabText(index) != page.title:
            self.setTabText(index, page.title)
