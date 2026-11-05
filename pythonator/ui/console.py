"""The live console: a bar (find, wrap, follow, open log, clear), the output with ANSI colours and clickable links,
and a line for the program's input.

Output is drawn on a timer, a few milliseconds a frame, and not while the user types, clicks or scrolls, so a flooding
program never makes the editor or the rest of the window wait. When it prints faster than that, every frame shows the
newest output (with a note a second; the log has everything), and while it can't be seen it only keeps the newest
lines. A carriage return rewrites the line, as in
a terminal, so a progress bar stays one short line instead of growing without end."""

from __future__ import annotations

import re
import time

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, pyqtSignal
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
    QApplication,
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

FRAME_MS = 33  # how often waiting output is drawn
DRAW_BUDGET = 0.003  # seconds of each frame that drawing may take: a key press waits at most about this long
SLICE = 2_000  # characters drawn between two looks at the clock
LONG_LINE = 8_000  # longer lines are broken: one huge line is slow to lay out on every change
QUIET = 0.15  # seconds after a key, click or scroll anywhere in the window during which the console waits
MAX_WAIT = 2.0  # but it draws at least this often
LAG = 0.3  # seconds of drawing allowed to wait; a burst within that shows in full, a flood is thinned out
BACKLOG = 4_000_000  # characters kept waiting at most, however slow the drawing
SGR = re.compile(r"\x1b\[[0-9;]*m")
URL = re.compile(r"https?://[^\s<>\"'`\x1b]+[^\s<>\"'`\x1b.,;:!?)\]]")


class _Input(QObject):
    """When the user last typed, clicked or scrolled: the console then waits a moment, so the editor gets the
    whole frame."""

    KINDS = frozenset({QEvent.Type.KeyPress, QEvent.Type.Wheel, QEvent.Type.MouseButtonPress,
                       QEvent.Type.MouseButtonDblClick, QEvent.Type.ShortcutOverride})

    def __init__(self) -> None:
        super().__init__()
        self.at = 0.0

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if event is not None and event.type() in self.KINDS:
            self.at = time.perf_counter()
        return False


_input: _Input | None = None


def _watch_input() -> _Input:
    global _input
    if _input is None:
        _input = _Input()
        if (app := QApplication.instance()) is not None:
            app.installEventFilter(_input)
    return _input


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
        self._skipped = 0  # lines skipped and not yet noted
        self._cr = False  # a carriage return held back: a line feed may follow in the next piece
        self._rate = 400_000.0  # characters drawn per second of drawing, measured
        self._drawn = 0.0  # when output was last drawn
        self._noted = 0.0  # when the last "skipped" note was drawn
        self._input = _watch_input()
        self._fast = 0  # characters of a shown log tail, drawn with a bigger budget: nobody types while it loads
        self._formats: dict[tuple[str, bool, bool, bool, bool, str], QTextCharFormat] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._flush)
        self._find_timer = QTimer(self)  # re-searching 20,000 lines on every flush would cost more than drawing
        self._find_timer.setSingleShot(True)
        self._find_timer.setInterval(1000)
        self._find_timer.timeout.connect(self._highlight)

    # -- text -----------------------------------------------------------------------------------------------

    def _submit(self) -> None:
        text = self.input.text()
        self.input.remember(text)
        self.input.clear()
        self.submitted.emit(text)

    def clear(self) -> None:
        self._queue.clear()
        self._queued = self._skipped = self._fast = 0
        self._cr = False
        self._parser = Parser()
        self.view.clear()
        self.count.clear()

    def set_text(self, text: str) -> None:
        self.clear()
        self.follow.setChecked(True)
        self.append(text)
        self._fast = len(text)

    def append(self, text: str) -> None:
        """Queue output; it's drawn by the next frames. Only appending happens here, so it's cheap however often."""
        if not text:
            return
        self._queue.append(text)
        self._queued += len(text)
        if self._queued > BACKLOG:
            self._skip(self._queued - BACKLOG // 2)
        if not self._timer.isActive():
            self._timer.start()

    def _skip(self, chars: int) -> None:
        """Drop about the oldest ``chars`` waiting characters, at a line end, and count the lines for the note."""
        skipped = 0
        while self._queue and len(self._queue[0]) <= chars:  # whole pieces first, then into the next one
            piece = self._queue.pop(0)
            chars -= len(piece)
            self._queued -= len(piece)
            skipped += piece.count("\n")
        if self._queue and chars > 0:
            first = self._queue[0]
            cut = first.find("\n", chars) + 1 or len(first)
            skipped += first.count("\n", 0, cut)
            self._queue[0] = first[cut:]
            self._queued -= cut
        self._skipped += skipped  # a progress bar's skipped redraws (no line ends) need no note
        self._parser = Parser()  # the colour set in what was skipped no longer applies
        self._cr = False

    def _showing(self) -> bool:
        window = self.window()
        return self.view.isVisible() and self.view.height() > 4 and not (window is not None and window.isMinimized())

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
        if not self._showing():  # nobody sees it: keep about what the console would hold, draw nothing
            if self._queued > self.max_lines * 150:
                self._skip(self._queued - self.max_lines * 100)
            return
        began = time.perf_counter()
        if began - self._input.at < QUIET and began - self._drawn < MAX_WAIT:
            return  # the user is typing or scrolling: that goes first
        self._drawn = began
        keep = max(48_000, int(self._rate * DRAW_BUDGET * 1000 / FRAME_MS * LAG))  # what LAG seconds can draw
        if not self._fast and self._queued > keep:  # behind: drop the oldest a little every frame, never a jump
            self._skip(self._queued - keep)
        text = "".join(self._queue)
        self._queue.clear()
        self._queued = 0
        if self._cr:
            text = "\r" + text
        if "\r" in text:
            text = text.replace("\r\n", "\n")
            self._cr = text.endswith("\r")
            if self._cr:
                text = text[:-1]
        self._writing = True
        cursor = QTextCursor(self.view.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        if self._skipped and began - self._noted >= 1.0:  # one note a second at most
            self._noted = began
            if cursor.block().length() > 1:
                cursor.insertText("\n")
            lines = f"{self._skipped:,} line" + ("s" if self._skipped != 1 else "")
            cursor.insertText(f"[… {lines} skipped here to keep up; the log has all of them]\n",
                              self._format(theme.color("muted"), False, True, False, False))
            self._skipped = 0
        budget = DRAW_BUDGET * 4 if self._fast else DRAW_BUDGET
        pos, last, now = 0, 0.0, time.perf_counter()
        drawing = now
        while pos < len(text) and (pos == 0 or now + last - began < budget):  # stop before it runs over
            end = text.rfind("\n", pos, pos + SLICE) + 1 or min(len(text), pos + SLICE)  # whole lines when it can
            self._draw(cursor, text[pos:end])
            pos = end
            last, now = time.perf_counter() - now, time.perf_counter()
        self._fast = max(0, self._fast - pos)
        if pos < len(text):  # the rest waits for the next frame, with its held-back carriage return
            self._queue.append(text[pos:] + ("\r" if self._cr else ""))
            self._queued = len(self._queue[0])
            self._cr = False
        self._trim()
        cursor.endEditBlock()
        if self.follow.isChecked():
            self._to_end()
        self._writing = False
        spent = now - drawing
        if spent > 0.0005:
            self._rate = 0.8 * self._rate + 0.2 * (pos / spent)
        if self.search.text() and not self._find_timer.isActive():
            self._find_timer.start()

    def _draw(self, cursor: QTextCursor, text: str) -> None:
        if "\r" in text:
            text = self._rewrite(cursor, text)
        first = text.find("\n")
        if cursor.block().length() + (len(text) if first < 0 else first) > LONG_LINE:
            cursor.insertText("\n")
        for chunk, s in self._parser.feed(text):
            pos = 0
            for m in URL.finditer(chunk) if "://" in chunk else ():
                if m.start() > pos:
                    cursor.insertText(chunk[pos:m.start()], self._format(s.fg, s.bold, s.dim, s.italic, s.underline))
                cursor.insertText(m.group(), self._format(s.fg, s.bold, s.dim, s.italic, s.underline, m.group()))
                pos = m.end()
            if pos < len(chunk):
                cursor.insertText(chunk[pos:], self._format(s.fg, s.bold, s.dim, s.italic, s.underline))

    @staticmethod
    def _rewrite(cursor: QTextCursor, text: str) -> str:
        """A carriage return starts the line again: keep what follows the last one (and the colours before it).
        In the first line that means wiping what's already drawn of the line too."""
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if (at := line.rfind("\r")) >= 0:
                lines[i] = "".join(SGR.findall(line, 0, at)) + line[at + 1:]
                if i == 0 and cursor.block().length() > 1:
                    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
        return "\n".join(lines)

    def _trim(self) -> None:
        """Drop the oldest lines over the limit in one cut, a twentieth at a time. QPlainTextEdit's own limit walks
        block by block and was most of the cost under heavy output."""
        doc = self.view.document()
        if doc is None or doc.blockCount() <= self.max_lines + self.max_lines // 20:
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
