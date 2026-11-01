"""The live console: a fast ANSI-aware log view with find, history search and an input line."""

from __future__ import annotations

import time
from collections.abc import Iterable

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.ansi import AnsiParser, Style
from ..core.locations import find_location
from ..core.logs import LineKind, LogLine
from ..core.urls import URL_RE, clean_url
from . import keys, theme

MAX_RENDER_PER_TICK = 5000
MAX_HIGHLIGHTS = 2000

_KIND_COLORS = {
    LineKind.SYSTEM: QColor("#7aa2f7"),
    LineKind.ERROR: QColor(theme.RED),
    LineKind.INPUT: QColor(theme.YELLOW),
}


def _utf16_len(text: str) -> int:
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


class ConsoleView(QPlainTextEdit):
    """Read-only output view. Appending is O(new lines); old lines fall off the top."""

    open_location = pyqtSignal(str, int)
    open_url = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(theme.monospace_font(10))
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setStyleSheet(f"QPlainTextEdit {{ background: {theme.BG}; border: none; padding: 4px; }}")
        self.setCenterOnScroll(False)
        self._parser = AnsiParser()
        self._formats: dict[tuple[Style, LineKind], QTextCharFormat] = {}
        self._press: tuple[int, int] | None = None
        self._ts_format = QTextCharFormat()
        self._ts_format.setForeground(QColor("#5f6873"))
        self._show_ts = True
        self._ts_second = -1
        self._ts_text = ""
        self._empty = True
        self.setMaximumBlockCount(10_000)
        if viewport := self.viewport():
            viewport.setMouseTracking(True)  # pointer cursor over links

    # -- configuration --------------------------------------------------------------------

    def set_max_lines(self, n: int) -> None:
        self.setMaximumBlockCount(max(100, n))

    def set_show_timestamps(self, show: bool) -> None:
        self._show_ts = show

    def set_wrap(self, wrap: bool) -> None:
        self.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )

    # -- content --------------------------------------------------------------------------

    def at_bottom(self) -> bool:
        sb = self.verticalScrollBar()
        return sb is None or sb.value() >= sb.maximum() - 4

    def scroll_to_bottom(self) -> None:
        if sb := self.verticalScrollBar():
            sb.setValue(sb.maximum())

    def reset(self) -> None:
        self.clear()
        self._parser.reset()
        self._empty = True

    def set_lines(self, lines: list[LogLine]) -> None:
        self.reset()
        self.append_lines(lines, follow=True)

    def append_lines(self, lines: list[LogLine], *, follow: bool | None = None) -> None:
        """Append ``lines``; ``follow=None`` keeps scrolling only if already at the bottom."""
        if not lines:
            return
        limit = min(self.maximumBlockCount() or MAX_RENDER_PER_TICK, MAX_RENDER_PER_TICK)
        if len(lines) > limit:
            skipped = len(lines) - limit
            lines = [
                LogLine(lines[-limit].ts, f"… {skipped:,} lines not rendered (see the log file)", LineKind.SYSTEM),
                *lines[-limit:],
            ]
        if follow is None:
            follow = self.at_bottom()
        sb = self.verticalScrollBar()
        keep = sb.value() if sb is not None and not follow else None
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        try:
            self._write(cursor, lines)
        finally:
            cursor.endEditBlock()
        if follow:
            self.scroll_to_bottom()
        elif sb is not None and keep is not None:
            sb.setValue(min(keep, sb.maximum()))

    def _write(self, cursor: QTextCursor, lines: Iterable[LogLine]) -> None:
        for line in lines:
            if self._empty:
                self._empty = False
            else:
                cursor.insertBlock()
            if self._show_ts:
                cursor.insertText(self._timestamp(line.ts), self._ts_format)
            text = line.text
            begin = cursor.position()
            if line.kind is LineKind.OUT:
                if "\x1b" in text:
                    parts = self._parser.feed(text)
                    for segment, style in parts:
                        cursor.insertText(segment, self._format(style, LineKind.OUT))
                    if "://" in text:
                        self._linkify(begin, "".join(seg for seg, _ in parts))
                    continue
                if text:
                    cursor.insertText(text, self._format(self._parser.style, LineKind.OUT))
            else:
                cursor.insertText(text, self._format(Style(), line.kind))
            if "://" in text:
                self._linkify(begin, text)

    def _linkify(self, begin: int, text: str) -> None:
        """Turn URLs in the line just written (``text``, starting at ``begin``) into links."""
        doc = self.document()
        if doc is None:
            return
        for m in URL_RE.finditer(text):
            url = clean_url(m.group(0))
            if not url:
                continue
            # Qt positions count UTF-16 code units; Python counts code points.
            start = begin + _utf16_len(text[: m.start()])
            sel = QTextCursor(doc)
            sel.setPosition(start)
            sel.setPosition(start + _utf16_len(url), QTextCursor.MoveMode.KeepAnchor)
            sel.mergeCharFormat(self._link_format(url))

    @staticmethod
    def _link_format(url: str) -> QTextCharFormat:
        link = QTextCharFormat()
        link.setAnchor(True)
        link.setAnchorHref(url)
        link.setFontUnderline(True)
        link.setForeground(QColor(theme.BLUE))
        return link

    def _timestamp(self, ts: float) -> str:
        sec = int(ts)
        if sec != self._ts_second:
            self._ts_second = sec
            self._ts_text = time.strftime("%H:%M:%S ", time.localtime(sec))
        return self._ts_text

    def _format(self, style: Style, kind: LineKind) -> QTextCharFormat:
        key = (style, kind)
        fmt = self._formats.get(key)
        if fmt is not None:
            return fmt
        fmt = QTextCharFormat()
        fg = QColor(*style.fg) if style.fg else _KIND_COLORS.get(kind, QColor(theme.TEXT))
        bg = QColor(*style.bg) if style.bg else None
        if style.inverse:
            fg, bg = (bg or QColor(theme.BG)), fg
        if style.dim:
            fg.setAlpha(150)
        fmt.setForeground(fg)
        if bg is not None:
            fmt.setBackground(bg)
        if style.bold or kind is LineKind.ERROR:
            fmt.setFontWeight(QFont.Weight.Bold)
        fmt.setFontItalic(style.italic)
        fmt.setFontUnderline(style.underline)
        fmt.setFontStrikeOut(style.strike)
        if len(self._formats) > 4096:
            self._formats.clear()
        self._formats[key] = fmt
        return fmt

    # -- interaction ----------------------------------------------------------------------

    def mousePressEvent(self, e: QMouseEvent | None) -> None:
        if e is not None and e.button() == Qt.MouseButton.LeftButton:
            p = e.position().toPoint()
            self._press = (p.x(), p.y())
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent | None) -> None:
        super().mouseReleaseEvent(e)
        if e is None or e.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        p = e.position().toPoint()
        moved = abs(p.x() - self._press[0]) + abs(p.y() - self._press[1]) > 4
        self._press = None
        if not moved and not self.textCursor().hasSelection() and (href := self.anchorAt(p)):
            self.open_url.emit(href)

    def mouseMoveEvent(self, e: QMouseEvent | None) -> None:
        super().mouseMoveEvent(e)
        if e is not None and (viewport := self.viewport()):
            over = bool(self.anchorAt(e.position().toPoint()))
            viewport.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.IBeamCursor)

    def mouseDoubleClickEvent(self, e: QMouseEvent | None) -> None:
        if e is not None:
            block = self.cursorForPosition(e.pos()).block().text()
            if found := find_location(block):
                self.open_location.emit(*found)
                return
        super().mouseDoubleClickEvent(e)

    def highlight(self, needle: str) -> int:
        """Highlight every occurrence of ``needle``; returns the number found (capped)."""
        selections: list[QTextEdit.ExtraSelection] = []
        doc = self.document()
        if needle and doc is not None:
            color = QColor(theme.YELLOW)
            color.setAlpha(90)
            cursor = QTextCursor(doc)
            while len(selections) < MAX_HIGHLIGHTS:
                cursor = doc.find(needle, cursor)
                if cursor.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format.setBackground(color)
                selections.append(sel)
        self.setExtraSelections(selections)
        return len(selections)

    def find_step(self, needle: str, backward: bool = False) -> bool:
        if not needle:
            return False
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        if self.find(needle, flags):
            return True
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
        self.setTextCursor(cursor)
        return self.find(needle, flags)


class HistoryLineEdit(QLineEdit):
    """A line edit with shell-like Up/Down history."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: list[str] = []
        self._index = 0

    def remember(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
            del self._history[:-200]
        self._index = len(self._history)

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is not None and self._history and e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = -1 if e.key() == Qt.Key.Key_Up else 1
            self._index = max(0, min(len(self._history), self._index + step))
            self.setText(self._history[self._index] if self._index < len(self._history) else "")
            return
        super().keyPressEvent(e)


def _tool(text: str, tip: str, *, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    return b


class ConsolePanel(QWidget):
    """Console view plus its toolbar and input line."""

    submitted = pyqtSignal(str, str)  # (mode: "stdin" | "shell", text)
    history_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    open_log_requested = pyqtSignal()
    timestamps_toggled = pyqtSignal(bool)
    wrap_toggled = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = theme.named(QWidget(), "PanelBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 4, 6, 4)
        bl.setSpacing(3)
        self.title = QLabel("Console")
        self.title.setObjectName("Dim")
        bl.addWidget(self.title)
        bl.addStretch(1)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText(f"Find in console  ({keys.text(keys.FIND)})")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setFixedWidth(240)
        self.find_count = QLabel("")
        self.find_count.setObjectName("Dim")
        bl.addWidget(self.find_edit)
        bl.addWidget(self.find_count)
        self.btn_prev = _tool("↑", "Previous match (Shift+Enter)")
        self.btn_next = _tool("↓", "Next match (Enter)")
        bl.addWidget(self.btn_prev)
        bl.addWidget(self.btn_next)
        self.btn_time = _tool("Time", "Show timestamps", checkable=True)
        self.btn_wrap = _tool("Wrap", "Wrap long lines", checkable=True)
        self.btn_follow = _tool("Follow", "Keep scrolled to the newest output", checkable=True)
        self.btn_follow.setChecked(True)
        self.btn_history = _tool("History…", "Search the full log history on disk")
        self.btn_open = _tool("Open log", "Open the log file in your editor")
        self.btn_clear = _tool("Clear", "Clear the console (the log file is kept)")
        for b in (self.btn_time, self.btn_wrap, self.btn_follow, self.btn_history, self.btn_open, self.btn_clear):
            bl.addWidget(b)
        layout.addWidget(bar)

        self.view = ConsoleView()
        layout.addWidget(self.view, 1)

        inp = theme.named(QWidget(), "InputBar")
        il = QHBoxLayout(inp)
        il.setContentsMargins(8, 6, 8, 6)
        il.setSpacing(6)
        self.mode = QComboBox()
        self.mode.addItem("stdin", "stdin")
        self.mode.addItem("$ shell", "shell")
        self.mode.setToolTip(
            "stdin: send the line to the running process.\n"
            "shell: run a command in the service's environment (venv on PATH), e.g. pip list."
        )
        self.input = HistoryLineEdit()
        self.input.setFont(theme.monospace_font(10))
        send = QToolButton()
        send.setText("Send")
        il.addWidget(self.mode)
        il.addWidget(self.input, 1)
        il.addWidget(send)
        layout.addWidget(inp)

        self.find_edit.textChanged.connect(self._on_find_changed)
        self.find_edit.returnPressed.connect(lambda: self._find(backward=False))
        self.find_edit.installEventFilter(self)
        self.btn_next.clicked.connect(lambda: self._find(backward=False))
        self.btn_prev.clicked.connect(lambda: self._find(backward=True))
        self.btn_time.toggled.connect(self.timestamps_toggled)
        self.btn_wrap.toggled.connect(self._on_wrap)
        self.btn_follow.toggled.connect(self._on_follow)
        self.btn_history.clicked.connect(self.history_requested)
        self.btn_open.clicked.connect(self.open_log_requested)
        self.btn_clear.clicked.connect(self.clear_requested)
        self.input.returnPressed.connect(self._submit)
        send.clicked.connect(self._submit)
        self.mode.currentIndexChanged.connect(lambda _i: self._update_placeholder())
        if sb := self.view.verticalScrollBar():
            sb.valueChanged.connect(self._on_scrolled)
        QShortcut(QKeySequence.StandardKey.Find, self, self._focus_find,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._running = False
        self._shell_name = ""
        self._question: str | None = None
        self._update_placeholder()

    # -- public ---------------------------------------------------------------------------

    def set_running(self, running: bool) -> None:
        """Pick a sensible default input mode for the selected service."""
        if running != self._running:
            self._running = running
            self.mode.setCurrentIndex(0 if running else 1)
        self._update_placeholder()

    def append(self, lines: list[LogLine]) -> None:
        if not lines:
            return
        self.view.append_lines(lines, follow=self.btn_follow.isChecked())
        if self.find_edit.text():
            self._refresh_highlight()

    def show_lines(self, lines: list[LogLine]) -> None:
        self.view.set_lines(lines)
        self.btn_follow.setChecked(True)
        self._refresh_highlight()

    # -- internals ------------------------------------------------------------------------

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if (
            obj is self.find_edit
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
        ):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                self._find(backward=True)
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.find_edit.clear()
                self.input.setFocus()
                return True
        return super().eventFilter(obj, event)

    def _focus_find(self) -> None:
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def _on_find_changed(self, text: str) -> None:
        self._refresh_highlight()
        if text:
            self.btn_follow.setChecked(False)
            self.view.find_step(text, backward=True)

    def _refresh_highlight(self) -> None:
        needle = self.find_edit.text()
        n = self.view.highlight(needle)
        if not needle:
            self.find_count.setText("")
        else:
            self.find_count.setText(f"{n}{'+' if n >= MAX_HIGHLIGHTS else ''} found" if n else "no matches")

    def _find(self, *, backward: bool) -> None:
        if needle := self.find_edit.text():
            self.btn_follow.setChecked(False)
            self.view.find_step(needle, backward=backward)

    def _on_wrap(self, on: bool) -> None:
        self.view.set_wrap(on)
        self.wrap_toggled.emit(on)

    def _on_follow(self, on: bool) -> None:
        if on:
            self.view.scroll_to_bottom()

    def _on_scrolled(self, _value: int) -> None:
        at_bottom = self.view.at_bottom()
        if at_bottom != self.btn_follow.isChecked():
            self.btn_follow.blockSignals(True)
            self.btn_follow.setChecked(at_bottom)
            self.btn_follow.blockSignals(False)

    def set_question(self, question: str | None) -> None:
        """While Pythonator asks something (y/n), the input line answers it instead."""
        if question == self._question:
            return
        self._question = question
        self.input.setStyleSheet(f"QLineEdit {{ border-color: {theme.YELLOW}; }}" if question else "")
        if question:
            self.input.setFocus()
        self._update_placeholder()

    def _update_placeholder(self) -> None:
        if self._question:
            self.input.setPlaceholderText("Type y or n and press Enter")
        elif self.mode.currentData() == "stdin":
            self.input.setPlaceholderText(
                "Type a line for the process' stdin and press Enter"
                if self._running else "The service is not running; switch to $ shell to run commands"
            )
        else:
            shell = f" with {self._shell_name}" if self._shell_name else ""
            self.input.setPlaceholderText(f"Run a command{shell} in this environment, e.g.  pip install requests")

    def set_shell_name(self, name: str) -> None:
        """The console shell's name, e.g. "PowerShell 7", for the input's placeholder."""
        self._shell_name = name
        self._update_placeholder()

    def _submit(self) -> None:
        text = self.input.text()
        if not text.strip() and self.mode.currentData() == "shell" and not self._question:
            return
        self.input.remember(text)
        self.input.clear()
        self.submitted.emit(str(self.mode.currentData()), text)
