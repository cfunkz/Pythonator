"""Find and replace for the code editor: a compact bar of icon buttons, every match highlighted, "3 of 12".

Matching is Python's ``re`` on the editor's text (one engine for finding, counting and replacing, so the three
always agree); positions are converted to Scintilla's UTF-8 byte offsets once per search.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from PyQt6.Qsci import QsciScintilla
from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtGui import QColor, QKeyEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import QGridLayout, QLabel, QLineEdit, QToolButton, QWidget

from . import theme
from .icons import icon

MAX_MATCHES = 10_000  # counted; past this "10000+"
MAX_MARKED = 5_000  # highlighted
INDICATOR = 8  # a Scintilla indicator the lexers don't use
Editor = Callable[[], QsciScintilla | None]
Match = tuple[int, int, "re.Match[str]"]  # UTF-8 start, end, the match (for \1 in replacements)


def _button(name: str, tip: str, *, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(name))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    b.setProperty("flat", True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the caret stays in the find box
    return b


class _Keys(QObject):
    """Enter / Shift+Enter in the find box, Enter / Ctrl+Alt+Enter in the replace box, Esc in both."""

    def __init__(self, bar: FindBar) -> None:
        super().__init__(bar)
        self.bar = bar

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if not isinstance(event, QKeyEvent) or event.type() != QEvent.Type.KeyPress:
            return False
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key.Key_Escape:
            self.bar.close_bar()
            return True
        if key not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return False
        if obj is self.bar.replace_edit:
            if mods & Qt.KeyboardModifier.ControlModifier and mods & Qt.KeyboardModifier.AltModifier:
                self.bar.replace_all()
            else:
                self.bar.replace_one()
        else:
            self.bar.step(forward=not mods & Qt.KeyboardModifier.ShiftModifier)
        return True


class FindBar(QWidget):
    def __init__(self, editor: Editor, status: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FindBar")
        self.editor, self.status = editor, status
        self.matches: list[Match] = []
        self.more = False  # past MAX_MATCHES
        self._for: tuple[int, str, int] | None = None  # what self.matches were found for
        self._marked: QsciScintilla | None = None

        self.toggle = _button("more", "Show replace (Ctrl+H)")
        self.toggle.clicked.connect(lambda: self.show_replace(not self.replace_edit.isVisible()))
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.setClearButtonEnabled(True)
        self.count = QLabel("")
        self.count.setProperty("muted", True)
        self.count.setMinimumWidth(78)
        self.count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.case = _button("case", "Match case (Alt+C)", checkable=True)
        self.word = _button("word", "Whole word (Alt+W)", checkable=True)
        self.regex = _button("regex", "Regular expression (Alt+R)", checkable=True)
        prev = _button("up", "Previous match (Shift+Enter, Shift+F3)")
        nxt = _button("down", "Next match (Enter, F3)")
        close = _button("close", "Close (Esc)")
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace")
        self.one = _button("replace", "Replace (Enter in the replace box)")
        self.all = _button("replace_all", "Replace all (Ctrl+Alt+Enter)")

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(4)
        grid.addWidget(self.toggle, 0, 0)
        grid.addWidget(self.find_edit, 0, 1)
        for col, w in enumerate((self.count, self.case, self.word, self.regex, prev, nxt, close), start=2):
            grid.addWidget(w, 0, col)
        grid.addWidget(self.replace_edit, 1, 1)
        grid.addWidget(self.one, 1, 5)
        grid.addWidget(self.all, 1, 6)
        grid.setColumnStretch(1, 1)
        self.find_edit.setMaximumWidth(520)
        self.replace_edit.setMaximumWidth(520)
        self.show_replace(False)

        keys = _Keys(self)
        self.find_edit.installEventFilter(keys)
        self.replace_edit.installEventFilter(keys)
        self._timer = QTimer(self)  # typing: search once it pauses, not on every key
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.refresh)
        self.find_edit.textChanged.connect(lambda _t: self._timer.start())
        for b in (self.case, self.word, self.regex):
            b.toggled.connect(lambda _on: self.refresh())
        prev.clicked.connect(lambda: self.step(forward=False))
        nxt.clicked.connect(lambda: self.step(forward=True))
        close.clicked.connect(self.close_bar)
        self.one.clicked.connect(self.replace_one)
        self.all.clicked.connect(self.replace_all)
        for keys_, slot in (("Alt+C", self.case.toggle), ("Alt+W", self.word.toggle), ("Alt+R", self.regex.toggle)):
            QShortcut(QKeySequence(keys_), self, slot, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # -- showing --

    def show_replace(self, on: bool) -> None:
        for w in (self.replace_edit, self.one, self.all):
            w.setVisible(on)
        self.toggle.setIcon(icon("less" if on else "more"))
        self.toggle.setToolTip("Hide replace" if on else "Show replace (Ctrl+H)")

    def open(self, *, replace: bool = False) -> None:
        ed = self.editor()
        self.show()
        if replace:
            self.show_replace(True)
        if ed is not None and ed.hasSelectedText() and "\n" not in ed.selectedText():
            self.find_edit.setText(ed.selectedText())
        target = self.replace_edit if replace and self.find_edit.text() else self.find_edit
        target.setFocus()
        target.selectAll()
        self.refresh()

    def close_bar(self) -> None:
        self.hide()
        self._unmark()
        if (ed := self.editor()) is not None:
            ed.setFocus()

    def text_changed(self) -> None:
        """The editor's text changed: count again soon (only while the bar is open)."""
        self._for = None
        if self.isVisible():
            self._timer.start()

    # -- searching --

    def pattern(self) -> re.Pattern[str] | None:
        """The search as a regular expression; None (and the reason in the counter) if it isn't valid."""
        text = self.find_edit.text()
        if not text:
            return None
        source = text if self.regex.isChecked() else re.escape(text)
        if self.word.isChecked():
            source = rf"\b(?:{source})\b"
        try:
            return re.compile(source, 0 if self.case.isChecked() else re.IGNORECASE)
        except re.error as exc:
            self._show_count("Invalid", "error", f"Not a valid regular expression: {exc}")
            return None

    def refresh(self) -> None:
        """Find every match in the current file, highlight them and update "3 of 12"."""
        self._timer.stop()
        ed = self.editor()
        rx = self.pattern()
        if ed is None or rx is None:
            self.matches = []
            self._unmark()
            if not self.find_edit.text():
                self._show_count("", "", "")
            return
        key = (id(ed), rx.pattern, rx.flags)  # an edit of the text resets it (text_changed)
        if key != self._for or self._marked is not ed:
            self.matches, self.more = _find_all(rx, ed.text())
            self._for = key
            self._mark(ed)
        self._update_count(ed)

    def _mark(self, ed: QsciScintilla) -> None:
        self._unmark()
        ed.indicatorDefine(QsciScintilla.IndicatorStyle.StraightBoxIndicator, INDICATOR)
        colour = QColor(theme.color("warn"))
        ed.setIndicatorForegroundColor(colour, INDICATOR)
        ed.SendScintilla(QsciScintilla.SCI_INDICSETALPHA, INDICATOR, 70)
        ed.SendScintilla(QsciScintilla.SCI_INDICSETOUTLINEALPHA, INDICATOR, 160)
        ed.SendScintilla(QsciScintilla.SCI_INDICSETUNDER, INDICATOR, True)
        ed.SendScintilla(QsciScintilla.SCI_SETINDICATORCURRENT, INDICATOR)
        for start, end, _m in self.matches[:MAX_MARKED]:
            ed.SendScintilla(QsciScintilla.SCI_INDICATORFILLRANGE, start, max(1, end - start))
        self._marked = ed

    def _unmark(self) -> None:
        ed, self._marked = self._marked, None
        if ed is not None:
            try:
                ed.SendScintilla(QsciScintilla.SCI_SETINDICATORCURRENT, INDICATOR)
                ed.SendScintilla(QsciScintilla.SCI_INDICATORCLEARRANGE, 0, ed.length())
            except RuntimeError:  # its tab was closed
                pass

    def _current(self, ed: QsciScintilla) -> int:
        """The match that is selected right now, or -1."""
        start = ed.SendScintilla(QsciScintilla.SCI_GETSELECTIONSTART)
        end = ed.SendScintilla(QsciScintilla.SCI_GETSELECTIONEND)
        return next((i for i, (s, e, _m) in enumerate(self.matches) if s == start and e == end), -1)

    def _update_count(self, ed: QsciScintilla) -> None:
        if not self.matches:
            self._show_count("No results", "error", "")
            return
        total = f"{len(self.matches):,}{'+' if self.more else ''}"
        at = self._current(ed)
        self._show_count(f"{at + 1} of {total}" if at >= 0 else f"{total} found", "", "")

    def _show_count(self, text: str, tone: str, tip: str) -> None:
        self.count.setText(text)
        self.count.setToolTip(tip)
        self.count.setStyleSheet(f"color: {theme.color('error')}" if tone == "error" else "")

    def step(self, *, forward: bool = True) -> bool:
        """Select the next (or previous) match after the caret, wrapping around the file."""
        ed = self.editor()
        if ed is None:
            return False
        self.refresh()
        if not self.matches:
            self.status("Not found")
            return False
        start = ed.SendScintilla(QsciScintilla.SCI_GETSELECTIONSTART)
        end = ed.SendScintilla(QsciScintilla.SCI_GETSELECTIONEND)
        if forward:
            i = next((i for i, (s, _e, _m) in enumerate(self.matches) if s >= end and (s, _e) != (start, end)), 0)
        else:
            before = [i for i, (s, _e, _m) in enumerate(self.matches) if s < start]
            i = before[-1] if before else len(self.matches) - 1
        s, e, _m = self.matches[i]
        ed.SendScintilla(QsciScintilla.SCI_SETSEL, s, e)
        ed.SendScintilla(QsciScintilla.SCI_SCROLLRANGE, e, s)
        self._update_count(ed)
        return True

    # -- replacing --

    def _replacement(self, match: re.Match[str]) -> str:
        repl = self.replace_edit.text()
        if not self.regex.isChecked():
            return repl
        try:
            return match.expand(repl)  # \1, \g<name>
        except (re.error, IndexError) as exc:
            self.status(f"Replacement: {exc}")
            return repl

    def replace_one(self) -> None:
        """Replace the selected match, then go to the next one (or just find one, if none is selected)."""
        ed = self.editor()
        if ed is None or ed.isReadOnly():
            return
        self.refresh()
        at = self._current(ed)
        if at < 0:
            self.step()
            return
        ed.replaceSelectedText(self._replacement(self.matches[at][2]))
        self._for = None
        self.step()

    def replace_all(self) -> None:
        ed = self.editor()
        rx = self.pattern()
        if ed is None or rx is None or ed.isReadOnly():
            return
        text = ed.text()
        literal = self.replace_edit.text()
        try:
            new, n = rx.subn(self._replacement if self.regex.isChecked() else lambda _m: literal, text)
        except re.error as exc:
            self.status(f"Replacement: {exc}")
            return
        if n:
            first = ed.SendScintilla(QsciScintilla.SCI_GETFIRSTVISIBLELINE)
            line, col = ed.getCursorPosition()
            ed.beginUndoAction()  # one Ctrl+Z undoes them all
            ed.selectAll()
            ed.replaceSelectedText(new)
            ed.endUndoAction()
            ed.setCursorPosition(min(line, ed.lines() - 1), col)
            ed.SendScintilla(QsciScintilla.SCI_SETFIRSTVISIBLELINE, first)
        self._for = None
        self.refresh()
        self.status(f"Replaced {n:,}" if n else "Nothing to replace")


def _find_all(rx: re.Pattern[str], text: str) -> tuple[list[Match], bool]:
    """Every match as UTF-8 offsets (what Scintilla counts in). Empty matches are skipped."""
    out: list[Match] = []
    char = byte = 0
    for m in rx.finditer(text):
        if m.end() == m.start():
            continue
        if len(out) >= MAX_MATCHES:
            return out, True
        byte += len(text[char:m.start()].encode("utf-8", "surrogatepass"))
        start = byte
        byte += len(m.group().encode("utf-8", "surrogatepass"))
        char = m.end()
        out.append((start, byte, m))
    return out, False
