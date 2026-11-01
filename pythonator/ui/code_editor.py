"""A lightweight Python code editor widget.

Line numbers, current-line highlight, syntax highlighting, smart indentation, comment toggling
and (optionally, if ``jedi`` is installed) completion computed off the GUI thread using the
service's own interpreter/venv.
"""

from __future__ import annotations

import keyword
import re
import sys
from typing import Any

from PyQt6.QtCore import (
    QObject,
    QRect,
    QRunnable,
    QSize,
    QStringListModel,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShortcut,
    QSyntaxHighlighter,
    QTextCursor,
    QTextFormat,
)
from PyQt6.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QWidget

from . import keys, theme
from .highlighter import JS_BUILTINS, JS_KEYWORDS, PythonHighlighter, highlighter_for
from .languages import spec_for

try:
    import jedi
except ImportError:  # optional dependency
    jedi = None

HAS_JEDI = jedi is not None


def jedi_hint() -> str:
    """How to turn on smart Python completion, for the Python that runs Pythonator."""
    if getattr(sys, "frozen", False):  # a packaged build: pip can't add to it
        return ("Smart Python completion is off in this build (no jedi). The editor completes words from the "
                "file instead.")
    return (f'Smart Python completion is off: run  "{sys.executable}" -m pip install jedi  and restart '
            "Pythonator. Until then the editor completes words from the file.")

INDENT = "    "
_WORD = re.compile(r"[A-Za-z_]\w{2,}")
_JEDI_MAX_CHARS = 400_000
_jedi_envs: dict[str, Any] = {}


def _complete(
    language: str, code: str, line: int, col: int, path: str | None, python: str | None
) -> list[str]:
    if language != "python":
        if language == "javascript":
            return _fallback(code, JS_KEYWORDS | JS_BUILTINS)
        spec = spec_for(language)
        return _fallback(code, spec.keywords | spec.builtins if spec else frozenset())
    if jedi is None or len(code) > _JEDI_MAX_CHARS:
        return _fallback(code)
    try:
        env = None
        if python:
            env = _jedi_envs.get(python)
            if env is None:
                env = _jedi_envs[python] = jedi.create_environment(python, safe=False)
        script = jedi.Script(code=code, path=path, environment=env)
        return [c.name for c in script.complete(line, col)[:400]]
    except Exception:
        return _fallback(code)


def _fallback(code: str, words: frozenset[str] = frozenset(keyword.kwlist)) -> list[str]:
    return sorted(set(_WORD.findall(code)) | words)


class _CompletionSignals(QObject):
    done = pyqtSignal(int, list)


class _CompletionJob(QRunnable):
    def __init__(self, request: int, args: tuple[str, str, int, int, str | None, str | None]) -> None:
        super().__init__()
        self.request = request
        self.args = args
        self.signals = _CompletionSignals()

    def run(self) -> None:
        self.signals.done.emit(self.request, _complete(*self.args))


_POOL: QThreadPool | None = None


def _pool() -> QThreadPool:
    global _POOL
    if _POOL is None:
        _POOL = QThreadPool()
        _POOL.setMaxThreadCount(1)  # jedi isn't designed for concurrent use
    return _POOL


class _Gutter(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.gutter_width(), 0)

    def paintEvent(self, event: QPaintEvent | None) -> None:
        if event is not None:
            self._editor.paint_gutter(event)


class CodeEditor(QPlainTextEdit):
    cursor_moved = pyqtSignal(int, int)  # line, column (1-based)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path: str | None = None
        self.python: str | None = None
        self.setFont(theme.monospace_font(11))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {theme.BG}; border: none; }}")
        self.language = "python"
        self._highlighter: QSyntaxHighlighter | None = PythonHighlighter(self.document())
        self._gutter = _Gutter(self)
        self.blockCountChanged.connect(lambda _n: self._update_margins())
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._on_cursor)
        self._update_margins()
        self._on_cursor()

        self._model = QStringListModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.activated.connect(self._insert_completion)
        self._request = 0
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self.request_completion)
        ctx = Qt.ShortcutContext.WidgetShortcut
        QShortcut(QKeySequence(keys.COMPLETE), self, self.request_completion, context=ctx)
        QShortcut(QKeySequence(keys.COMMENT), self, self.toggle_comment, context=ctx)
        QShortcut(QKeySequence(keys.DUPLICATE), self, self.duplicate_line, context=ctx)

    @property
    def comment_prefix(self) -> str:
        if self.language == "javascript":
            return "//"
        if spec := spec_for(self.language):
            return spec.line_comment[0].strip() if spec.line_comment else "#"
        return "#"

    def set_language(self, language: str) -> None:
        """Switch highlighting / comments / completion: 'python', 'javascript' or 'text'."""
        if language == self.language:
            return
        self.language = language
        if self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter.deleteLater()
        self._highlighter = highlighter_for(language, self.document())

    # -- gutter ---------------------------------------------------------------------------

    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 20 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margins(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if (vp := self.viewport()) is not None and rect.contains(vp.rect()):
            self._update_margins()

    def resizeEvent(self, e: QResizeEvent | None) -> None:
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event: QPaintEvent) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor(theme.BG))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        height = self.fontMetrics().height()
        width = self._gutter.width() - 10
        dim, bright = QColor(theme.TEXT_FAINT), QColor(theme.TEXT)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(bright if number == current else dim)
                painter.drawText(0, top, width, height, Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

    def _on_cursor(self) -> None:
        sel = QTextEdit.ExtraSelection()
        color = QColor(theme.ACCENT)
        color.setAlpha(22)
        sel.format.setBackground(color)
        sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])
        self._gutter.update()
        c = self.textCursor()
        self.cursor_moved.emit(c.blockNumber() + 1, c.positionInBlock() + 1)

    # -- editing --------------------------------------------------------------------------

    def go_to_line(self, line: int) -> None:
        doc = self.document()
        if doc is None:
            return
        block = doc.findBlockByNumber(max(0, line - 1))
        if block.isValid():
            cursor = QTextCursor(block)
            self.setTextCursor(cursor)
            self.centerCursor()
            self.setFocus()

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is None:
            return
        popup = self._completer.popup()
        if popup is not None and popup.isVisible() and e.key() in (
            Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab
        ):
            e.ignore()
            return
        key, mods = e.key(), e.modifiers()
        if key == Qt.Key.Key_Tab and not mods & Qt.KeyboardModifier.ControlModifier:
            if self.textCursor().hasSelection():
                self._shift_selection(+1)
            else:
                cursor = self.textCursor()
                col = cursor.positionInBlock()
                cursor.insertText(" " * (4 - col % 4))
            return
        if key == Qt.Key.Key_Backtab:
            self._shift_selection(-1)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not mods & Qt.KeyboardModifier.ShiftModifier:
            self._newline_with_indent()
            return
        if key == Qt.Key.Key_Backspace and self._smart_backspace():
            return
        super().keyPressEvent(e)
        text = e.text()
        if text == ".":
            self._debounce.start(0)
        elif text.isidentifier() or (text.isalnum() and self._word_before_cursor()):
            if len(self._word_before_cursor()) >= 3:
                self._debounce.start()
        elif text and popup is not None:
            popup.hide()  # space, punctuation, ... ends the word being completed

    def _newline_with_indent(self) -> None:
        cursor = self.textCursor()
        line = cursor.block().text()[: cursor.positionInBlock()]
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.rstrip()
        if stripped.endswith((":", "(", "[", "{")):
            indent += INDENT
        elif self.language == "python" and stripped.split(maxsplit=1)[:1] in (
            ["return"], ["pass"], ["break"], ["continue"], ["raise"]
        ):
            indent = indent[:-4] if indent.endswith(INDENT) else indent
        cursor.beginEditBlock()
        cursor.insertText("\n" + indent)
        cursor.endEditBlock()
        self.ensureCursorVisible()

    def _smart_backspace(self) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        before = cursor.block().text()[: cursor.positionInBlock()]
        if before and not before.strip(" ") and len(before) % 4 == 0:
            for _ in range(4):
                cursor.deletePreviousChar()
            return True
        return False

    def _selected_blocks(self) -> tuple[QTextCursor, int, int]:
        cursor = self.textCursor()
        doc = self.document()
        assert doc is not None
        start = doc.findBlock(cursor.selectionStart()).blockNumber()
        end_block = doc.findBlock(cursor.selectionEnd())
        end = end_block.blockNumber()
        if cursor.hasSelection() and cursor.selectionEnd() == end_block.position() and end > start:
            end -= 1
        return cursor, start, end

    def _shift_selection(self, direction: int) -> None:
        cursor, start, end = self._selected_blocks()
        doc = self.document()
        assert doc is not None
        cursor.beginEditBlock()
        for n in range(start, end + 1):
            block = doc.findBlockByNumber(n)
            c = QTextCursor(block)
            if direction > 0:
                c.insertText(INDENT)
            else:
                text = block.text()
                remove = len(text) - len(text.lstrip(" "))
                c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, min(4, remove))
                c.removeSelectedText()
        cursor.endEditBlock()

    def toggle_comment(self) -> None:
        cursor, start, end = self._selected_blocks()
        doc = self.document()
        assert doc is not None
        blocks = [doc.findBlockByNumber(n) for n in range(start, end + 1)]
        code = [b for b in blocks if b.text().strip()]
        if not code:
            return
        mark = self.comment_prefix
        uncomment = all(b.text().lstrip().startswith(mark) for b in code)
        col = min(len(b.text()) - len(b.text().lstrip()) for b in code)
        cursor.beginEditBlock()
        for b in code:
            c = QTextCursor(b)
            text = b.text()
            if uncomment:
                i = text.index(mark)
                n = len(mark) + (1 if text[i + len(mark) : i + len(mark) + 1] == " " else 0)
                c.setPosition(b.position() + i)
                c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, n)
                c.removeSelectedText()
            else:
                c.setPosition(b.position() + col)
                c.insertText(mark + " ")
        cursor.endEditBlock()

    def duplicate_line(self) -> None:
        cursor = self.textCursor()
        text = cursor.block().text()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertText("\n" + text)
        cursor.endEditBlock()
        self.setTextCursor(cursor)

    # -- completion -----------------------------------------------------------------------

    def _word_before_cursor(self) -> str:
        cursor = self.textCursor()
        text = cursor.block().text()[: cursor.positionInBlock()]
        m = re.search(r"[A-Za-z_]\w*$", text)
        return m.group() if m else ""

    def request_completion(self) -> None:
        cursor = self.textCursor()
        self._request += 1
        job = _CompletionJob(
            self._request,
            (self.language, self.toPlainText(), cursor.blockNumber() + 1, cursor.positionInBlock(), self.path,
             self.python),
        )
        job.signals.done.connect(self._show_completions)
        _pool().start(job)

    def _show_completions(self, request: int, items: list[str]) -> None:
        if request != self._request or not self.hasFocus():
            return
        prefix = self._word_before_cursor()
        items = [i for i in dict.fromkeys(items) if i != prefix]
        if not items:
            if popup := self._completer.popup():
                popup.hide()
            return
        self._model.setStringList(items[:300])
        self._completer.setCompletionPrefix(prefix)
        popup = self._completer.popup()
        if popup is None:
            return
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))  # type: ignore[union-attr]
        rect = self.cursorRect()
        rect.setWidth(popup.sizeHintForColumn(0) + 40)
        self._completer.complete(rect)

    def _insert_completion(self, text: str) -> None:
        cursor = self.textCursor()
        prefix = self._word_before_cursor()
        if prefix:
            cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(prefix))
        cursor.insertText(text)
        self.setTextCursor(cursor)
