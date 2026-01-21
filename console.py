"""ANSI Console - Terminal emulator with color support (256-color, true color, bold, inverse)."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Optional
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QTextEdit
from config import normalize

@dataclass
class AnsiState:
    bold: bool = False
    inverse: bool = False
    fg: Optional[QColor] = None
    bg: Optional[QColor] = None
    tail: str = ""

class AnsiConsole(QTextEdit):
    PALETTE = ((0,0,0),(205,0,0),(0,205,0),(205,205,0),(0,0,238),(205,0,205),(0,205,205),(229,229,229),
               (127,127,127),(255,0,0),(0,255,0),(255,255,0),(92,92,255),(255,0,255),(0,255,255),(255,255,255))
    DEFAULT_FG, DEFAULT_BG = QColor(221,221,221), QColor(18,18,18)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet("QTextEdit { background: #121212; color: #ddd; border: 1px solid #333; border-radius: 2px; padding: 2px; }")
        font = QFont("Consolas" if os.name == "nt" else "Monospace", 10)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.setFont(font)
        self._state = AnsiState(fg=self.DEFAULT_FG, bg=self.DEFAULT_BG)
        self._fmt = self._build_format()

    def _build_format(self) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if self._state.bold else QFont.Weight.Normal)
        fg, bg = self._state.fg or self.DEFAULT_FG, self._state.bg or self.DEFAULT_BG
        if self._state.inverse: fg, bg = bg, fg
        fmt.setForeground(fg); fmt.setBackground(bg)
        return fmt

    def _color(self, n: int) -> QColor:
        if n < 16: return QColor(*self.PALETTE[n])
        if n < 232: n -= 16; return QColor((n//36)*51, ((n//6)%6)*51, (n%6)*51)
        g = 8 + (n-232)*10; return QColor(g, g, g)

    def _parse_sgr(self, params: str) -> None:
        nums = [int(p) for p in params.split(";") if p.isdigit()] or [0]
        i = 0
        while i < len(nums):
            c = nums[i]
            if c == 0: self._state.bold = self._state.inverse = False; self._state.fg, self._state.bg = self.DEFAULT_FG, self.DEFAULT_BG
            elif c == 1: self._state.bold = True
            elif c == 22: self._state.bold = False
            elif c == 7: self._state.inverse = True
            elif c == 27: self._state.inverse = False
            elif 30 <= c <= 37: self._state.fg = self._color(c-30)
            elif c == 39: self._state.fg = self.DEFAULT_FG
            elif 40 <= c <= 47: self._state.bg = self._color(c-40)
            elif c == 49: self._state.bg = self.DEFAULT_BG
            elif 90 <= c <= 97: self._state.fg = self._color(c-82)
            elif c in (38, 48):
                is_fg = c == 38
                if i+2 < len(nums) and nums[i+1] == 5:
                    col = self._color(nums[i+2]); (setattr(self._state, 'fg', col) if is_fg else setattr(self._state, 'bg', col)); i += 2
                elif i+4 < len(nums) and nums[i+1] == 2:
                    col = QColor(nums[i+2], nums[i+3], nums[i+4]); (setattr(self._state, 'fg', col) if is_fg else setattr(self._state, 'bg', col)); i += 4
            i += 1
        self._fmt = self._build_format()

    def _write(self, text: str, cursor: QTextCursor) -> None:
        if '\x1b[' not in text: cursor.insertText(text, self._fmt); return
        i, length = 0, len(text)
        while i < length:
            esc = text.find("\x1b[", i)
            if esc < 0: cursor.insertText(text[i:], self._fmt); break
            if esc > i: cursor.insertText(text[i:esc], self._fmt)
            j = esc + 2
            while j < length and not ("@" <= text[j] <= "~"): j += 1
            if j >= length: self._state.tail = text[esc:]; break
            if text[j] == "m": self._parse_sgr(text[esc+2:j])
            i = j + 1

    def append_text(self, text: str) -> None:
        if not text: return
        text = normalize(self._state.tail + text); self._state.tail = ""
        cursor = self.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        try: self._write(text, cursor)
        finally: cursor.endEditBlock()
        self.setTextCursor(cursor)

    def prepend_text(self, text: str) -> None:
        if not text: return
        text = normalize(text)
        saved = AnsiState(self._state.bold, self._state.inverse, self._state.fg, self._state.bg, self._state.tail)
        self._reset()
        cursor = self.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.beginEditBlock()
        try: self._write(text, cursor)
        finally: cursor.endEditBlock()
        self._state = saved; self._fmt = self._build_format()

    def set_content(self, text: str) -> None:
        self.clear(); self._reset(); self.append_text(text); self.moveCursor(QTextCursor.MoveOperation.End)

    def _reset(self) -> None:
        self._state = AnsiState(fg=self.DEFAULT_FG, bg=self.DEFAULT_BG); self._fmt = self._build_format()
