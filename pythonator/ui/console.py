"""The live console: a bar (find, wrap, follow, open log, clear), the output with ANSI colours and clickable links,
and a line for the program's input. Appends are batched per frame, so a flood never freezes the window."""

from __future__ import annotations

import re
import time

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.ansi import Parser
from . import theme

# Drawing is budgeted, as in 0.0.9, so a flooding program can't starve the window (and the editor with it).
FRAME_MS = 33  # how often waiting output is drawn
DRAW_BUDGET = 0.010  # seconds of each frame that drawing may take
SLICE = 8_000  # characters drawn between two looks at the clock
BACKLOG = 400_000  # characters waiting to be drawn; older ones are skipped with a note (the log has everything)
URL = re.compile(r"https?://[^\s<>\"'`\x1b]+[^\s<>\"'`\x1b.,;:!?)\]]")


def tool(text: str, tip: str, *, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class ConsoleView(QPlainTextEdit):
    """Read-only output; a click on a link opens it in the browser."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(theme.mono_font(10))
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {theme.color('code_bg')}; border: none;"
                           " border-radius: 0; padding: 4px 6px; }")
        if viewport := self.viewport():
            viewport.setMouseTracking(True)

    def _link(self, e: QMouseEvent) -> str:
        return self.anchorAt(e.position().toPoint())

    def mouseMoveEvent(self, e: QMouseEvent | None) -> None:
        if e is not None and (viewport := self.viewport()):
            viewport.setCursor(Qt.CursorShape.PointingHandCursor if self._link(e) else Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent | None) -> None:
        if e is not None and e.button() == Qt.MouseButton.LeftButton and not self.textCursor().hasSelection() \
                and (url := self._link(e)).startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))
        super().mouseReleaseEvent(e)


class InputLine(QLineEdit):
    """The program's input, with ↑ and ↓ through what was sent before."""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[str] = []
        self._at = 0

    def remember(self, text: str) -> None:
        if text and (not self.history or self.history[-1] != text):
            self.history = [*self.history[-199:], text]
        self._at = len(self.history)

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is not None and e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self.history:
            self._at = max(0, min(len(self.history), self._at + (-1 if e.key() == Qt.Key.Key_Up else 1)))
            self.setText(self.history[self._at] if self._at < len(self.history) else "")
            return
        super().keyPressEvent(e)


class Console(QWidget):
    submitted = pyqtSignal(str)
    clear_requested = pyqtSignal()
    open_log_requested = pyqtSignal()

    def __init__(self, max_lines: int = 20_000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        bar = QWidget()
        bar.setObjectName("PanelBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 4, 6, 4)
        bl.setSpacing(3)
        self.title = QLabel("Console")
        self.title.setProperty("muted", True)
        bl.addWidget(self.title)
        bl.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in console  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(110)
        self.search.setMaximumWidth(240)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.count = QLabel("")
        self.count.setProperty("muted", True)
        prev, nxt = tool("↑", "Previous match (Shift+Enter)"), tool("↓", "Next match (Enter)")
        self.wrap = tool("Wrap", "Wrap long lines", checkable=True)
        self.follow = tool("Follow", "Keep scrolled to the newest output", checkable=True)
        self.follow.setChecked(True)
        open_log = tool("Open log", "Open the full log file")
        clear = tool("Clear", "Clear the console and the log")
        for w in (self.search, self.count, prev, nxt, self.wrap, self.follow, open_log, clear):
            bl.addWidget(w)

        self.view = ConsoleView()
        self.max_lines = max_lines

        inp = QWidget()
        inp.setObjectName("InputBar")
        il = QHBoxLayout(inp)
        il.setContentsMargins(8, 6, 8, 6)
        il.setSpacing(6)
        stdin = QLabel("stdin")
        stdin.setProperty("muted", True)
        stdin.setFont(theme.mono_font(9))
        self.input = InputLine()
        self.input.setFont(theme.mono_font(10))
        self.input.setPlaceholderText("Type a line for the program's input and press Enter")
        send = tool("Send", "Send the line to the program")
        il.addWidget(stdin)
        il.addWidget(self.input, 1)
        il.addWidget(send)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.view, 1)
        layout.addWidget(inp)

        self.input.returnPressed.connect(self._submit)
        send.clicked.connect(self._submit)
        self.search.textChanged.connect(lambda _t: self._highlight())
        self.search.returnPressed.connect(lambda: self.step(backward=False))
        prev.clicked.connect(lambda: self.step(backward=True))
        nxt.clicked.connect(lambda: self.step(backward=False))
        self.wrap.toggled.connect(lambda on: self.view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap))
        self.follow.toggled.connect(lambda on: self._to_end() if on else None)
        open_log.clicked.connect(self.open_log_requested)
        clear.clicked.connect(self.clear_requested)
        QShortcut(QKeySequence.StandardKey.Find, self, self.search.setFocus,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Shift+Return"), self.search, lambda: self.step(backward=True),
                  context=Qt.ShortcutContext.WidgetShortcut)
        self._writing = False
        if sb := self.view.verticalScrollBar():  # scrolling up stops following, back at the end resumes
            sb.valueChanged.connect(lambda v: self.follow.setChecked(v >= sb.maximum() - 4)
                                    if not self._writing else None)
        self._parser = Parser()
        self._queue: list[str] = []
        self._queued = 0  # characters in _queue
        self._skipped = 0  # characters skipped since the last frame
        self._formats: dict[tuple[str, bool, bool, bool, bool, str], QTextCharFormat] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._flush)
        self._find_timer = QTimer(self)  # re-searching 20,000 lines on every flush would cost more than drawing
        self._find_timer.setSingleShot(True)
        self._find_timer.setInterval(400)
        self._find_timer.timeout.connect(self._highlight)

    # -- text -----------------------------------------------------------------------------------------------

    def _submit(self) -> None:
        text = self.input.text()
        self.input.remember(text)
        self.input.clear()
        self.submitted.emit(text)

    def clear(self) -> None:
        self._queue.clear()
        self._queued = self._skipped = 0
        self._parser = Parser()
        self.view.clear()
        self.count.clear()

    def set_text(self, text: str) -> None:
        self.clear()
        self.follow.setChecked(True)
        self.append(text)

    def append(self, text: str) -> None:
        self._queue.append(text)
        self._queued += len(text)
        if self._queued > BACKLOG:  # keep the newest: drop whole pieces from the front, then cut the first one
            while len(self._queue) > 1 and self._queued - len(self._queue[0]) > BACKLOG:
                dropped = self._queue.pop(0)
                self._queued -= len(dropped)
                self._skipped += len(dropped)
            if self._queued > BACKLOG:
                cut = self._queue[0].find("\n", self._queued - BACKLOG) + 1 or self._queued - BACKLOG
                self._queue[0] = self._queue[0][cut:]
                self._queued -= cut
                self._skipped += cut
        if not self._timer.isActive():
            self._timer.start()

    def _format(self, fg: str, bold: bool, dim: bool, italic: bool, underline: bool, href: str = "") -> QTextCharFormat:
        key = (fg, bold, dim, italic, underline, href)
        fmt = self._formats.get(key)
        if fmt is None:
            fmt = QTextCharFormat()
            colour = QColor(theme.color("accent_hi") if href else fg or theme.color("text"))
            if dim:
                colour.setAlphaF(0.6)
            fmt.setForeground(colour)
            if bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setFontItalic(italic)
            fmt.setFontUnderline(underline or bool(href))
            if href:
                fmt.setAnchor(True)
                fmt.setAnchorHref(href)
            if len(self._formats) > 512:
                self._formats.clear()
            self._formats[key] = fmt
        return fmt

    def _flush(self) -> None:
        if not self._queue:
            self._timer.stop()
            return
        text = "".join(self._queue)
        self._queue.clear()
        self._queued = 0
        self._writing = True
        began = time.perf_counter()
        cursor = QTextCursor(self.view.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        if self._skipped:
            cursor.insertText(f"[… {self._skipped:,} characters skipped here to keep up; the log has all of them]\n",
                              self._format(theme.color("muted"), False, True, False, False))
            self._skipped = 0
        pos = 0
        while pos < len(text) and (pos == 0 or time.perf_counter() - began < DRAW_BUDGET):
            end = text.find("\n", pos + SLICE) + 1 or len(text)  # whole lines, so a link is never split
            self._draw(cursor, text[pos:end])
            pos = end
        if pos < len(text):  # the rest waits for the next frame
            self._queue.append(text[pos:])
            self._queued = len(text) - pos
        self._trim()
        cursor.endEditBlock()
        if self.follow.isChecked():
            self._to_end()
        self._writing = False
        if self.search.text() and not self._find_timer.isActive():
            self._find_timer.start()

    def _draw(self, cursor: QTextCursor, text: str) -> None:
        for chunk, s in self._parser.feed(text):
            pos = 0
            for m in URL.finditer(chunk) if "://" in chunk else ():
                if m.start() > pos:
                    cursor.insertText(chunk[pos:m.start()], self._format(s.fg, s.bold, s.dim, s.italic, s.underline))
                cursor.insertText(m.group(), self._format(s.fg, s.bold, s.dim, s.italic, s.underline, m.group()))
                pos = m.end()
            if pos < len(chunk):
                cursor.insertText(chunk[pos:], self._format(s.fg, s.bold, s.dim, s.italic, s.underline))

    def _trim(self) -> None:
        """Drop the oldest lines over the limit in one cut, a tenth at a time. QPlainTextEdit's own limit walks
        block by block and was most of the cost under heavy output."""
        doc = self.view.document()
        if doc is None or doc.blockCount() <= self.max_lines + self.max_lines // 10:
            return
        cut = QTextCursor(doc)
        cut.setPosition(0)
        cut.setPosition(doc.findBlockByNumber(doc.blockCount() - self.max_lines).position(),
                        QTextCursor.MoveMode.KeepAnchor)
        cut.removeSelectedText()

    def _to_end(self) -> None:
        if sb := self.view.verticalScrollBar():
            sb.setValue(sb.maximum())

    # -- find -----------------------------------------------------------------------------------------------

    def _highlight(self) -> None:
        needle = self.search.text()
        doc = self.view.document()
        marks: list[QTextEdit.ExtraSelection] = []
        if needle and doc is not None:
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(theme.color("warn")).darker(250))
            cursor = doc.find(needle)
            while not cursor.isNull() and len(marks) < 5000:
                mark = QTextEdit.ExtraSelection()
                mark.cursor = cursor
                mark.format = fmt
                marks.append(mark)
                cursor = doc.find(needle, cursor)
        self.view.setExtraSelections(marks)
        self.count.setText(("5000+" if len(marks) >= 5000 else str(len(marks))) if needle else "")

    def step(self, *, backward: bool) -> bool:
        needle = self.search.text()
        if not needle:
            return False
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        if not self.view.find(needle, flags):
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
            self.view.setTextCursor(cursor)
            if not self.view.find(needle, flags):
                return False
        self.follow.setChecked(False)
        return True
