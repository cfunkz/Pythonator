"""A small, fast ANSI escape-sequence parser.

Only SGR (``ESC [ ... m``) sequences affect styling: 16/256/true-colour foreground and
background, bold, dim, italic, underline, inverse and strike-through. Every other escape
sequence (cursor movement, erase, OSC titles/hyperlinks, ...) is consumed and dropped so it
never shows up as garbage in the console.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import TypeAlias

RGB: TypeAlias = tuple[int, int, int]

# xterm's default 16-colour palette, slightly softened for dark backgrounds.
BASE16: tuple[RGB, ...] = (
    (40, 42, 46), (230, 80, 80), (120, 200, 110), (225, 190, 90),
    (90, 150, 240), (200, 120, 220), (80, 195, 205), (210, 210, 210),
    (120, 125, 135), (255, 110, 110), (150, 230, 130), (255, 220, 120),
    (125, 175, 255), (225, 150, 245), (110, 225, 235), (250, 250, 250),
)


@lru_cache(maxsize=256)
def xterm_color(n: int) -> RGB:
    """Map an xterm 256-colour index to RGB."""
    if n < 16:
        return BASE16[n]
    if n < 232:
        n -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
    level = 8 + (n - 232) * 10
    return level, level, level


@dataclass(frozen=True, slots=True)
class Style:
    fg: RGB | None = None
    bg: RGB | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    inverse: bool = False
    strike: bool = False


PLAIN = Style()

# CSI ... final | OSC ... (BEL | ST) | nF (charset selection, e.g. ESC ( B) | two-byte escapes.
# Groups 1/2 capture the CSI parameters and final byte.
_ESCAPE = re.compile(
    r"\x1b(?:\[([0-?]*)[ -/]*([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]+[0-~]|[0-~])"
)
# Prefix of an escape sequence that has not been completed yet (at end of a chunk).
_INCOMPLETE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*|[ -/]*)$")
_MAX_TAIL = 256


def strip_ansi(text: str) -> str:
    if "\x1b" not in text:
        return text
    return _ESCAPE.sub("", text).replace("\x1b", "")


def _apply_sgr(style: Style, params: str) -> Style:
    codes = [int(p) if p.isdigit() else 0 for p in params.replace(":", ";").split(";")] if params else [0]
    i, n = 0, len(codes)
    while i < n:
        c = codes[i]
        if c == 0:
            style = PLAIN
        elif c == 1:
            style = replace(style, bold=True)
        elif c == 2:
            style = replace(style, dim=True)
        elif c == 3:
            style = replace(style, italic=True)
        elif c == 4:
            style = replace(style, underline=True)
        elif c == 7:
            style = replace(style, inverse=True)
        elif c == 9:
            style = replace(style, strike=True)
        elif c == 22:
            style = replace(style, bold=False, dim=False)
        elif c == 23:
            style = replace(style, italic=False)
        elif c == 24:
            style = replace(style, underline=False)
        elif c == 27:
            style = replace(style, inverse=False)
        elif c == 29:
            style = replace(style, strike=False)
        elif 30 <= c <= 37:
            style = replace(style, fg=BASE16[c - 30])
        elif c == 39:
            style = replace(style, fg=None)
        elif 40 <= c <= 47:
            style = replace(style, bg=BASE16[c - 40])
        elif c == 49:
            style = replace(style, bg=None)
        elif 90 <= c <= 97:
            style = replace(style, fg=BASE16[c - 90 + 8])
        elif 100 <= c <= 107:
            style = replace(style, bg=BASE16[c - 100 + 8])
        elif c in (38, 48) and i + 1 < n:
            color: RGB | None = None
            if codes[i + 1] == 5 and i + 2 < n:
                color = xterm_color(max(0, min(255, codes[i + 2])))
                i += 2
            elif codes[i + 1] == 2 and i + 4 < n:
                r, g, b = (max(0, min(255, v)) for v in codes[i + 2 : i + 5])
                color = (r, g, b)
                i += 4
            if color is not None:
                style = replace(style, fg=color) if c == 38 else replace(style, bg=color)
        i += 1
    return style


class AnsiParser:
    """Stateful parser: styles carry over between calls, as they do in a terminal."""

    __slots__ = ("_tail", "style")

    def __init__(self) -> None:
        self.style = PLAIN
        self._tail = ""

    def reset(self) -> None:
        self.style = PLAIN
        self._tail = ""

    def feed(self, text: str) -> list[tuple[str, Style]]:
        """Split ``text`` into ``(segment, style)`` runs, consuming escape sequences."""
        if self._tail:
            text, self._tail = self._tail + text, ""
        if "\x1b" not in text:
            return [(text, self.style)] if text else []

        if (m := _INCOMPLETE.search(text)) and len(text) - m.start() <= _MAX_TAIL:
            self._tail = text[m.start() :]
            text = text[: m.start()]

        out: list[tuple[str, Style]] = []
        pos = 0
        for m in _ESCAPE.finditer(text):
            if m.start() > pos:
                out.append((text[pos : m.start()], self.style))
            if m.group(2) == "m":
                self.style = _apply_sgr(self.style, m.group(1))
            pos = m.end()
        if pos < len(text):
            rest = text[pos:].replace("\x1b", "")
            if rest:
                out.append((rest, self.style))
        return out
