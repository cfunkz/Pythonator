"""ANSI colour codes (SGR) to styled text runs for the console. Other escape sequences are dropped."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ESC = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])"       # CSI: colours (m) are kept, the rest dropped
                  r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: titles, hyperlinks
                  r"|\x1b[()][A-Za-z0-9]|\x1b[=>78]|\r(?!\n)")
_PARTIAL = re.compile(r"\x1b(?:\[[0-9;?]*|\][^\x07\x1b]{0,256}|[()])?$")  # an escape cut off at the end
BASIC = ("#3b3f4a", "#f0716b", "#79c35a", "#e0b45a", "#5aa2f0", "#c58af0", "#4fc4cf", "#d0d4dc",
         "#6b7280", "#ff8f87", "#98e07a", "#ffd27a", "#82bdff", "#dcaaff", "#7fe3ec", "#ffffff")


@dataclass(slots=True)
class Style:
    fg: str = ""
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False

    def copy(self) -> Style:
        return Style(self.fg, self.bold, self.dim, self.italic, self.underline)


def _color256(n: int) -> str:
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        r, g, b = (n // 36) % 6, (n // 6) % 6, n % 6
        return "#{:02x}{:02x}{:02x}".format(*(0 if v == 0 else 55 + v * 40 for v in (r, g, b)))
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _apply(style: Style, params: str) -> Style:
    codes = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else [0]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            style = Style()
        elif c == 1:
            style.bold = True
        elif c == 2:
            style.dim = True
        elif c == 3:
            style.italic = True
        elif c == 4:
            style.underline = True
        elif c == 22:
            style.bold = style.dim = False
        elif c == 23:
            style.italic = False
        elif c == 24:
            style.underline = False
        elif 30 <= c <= 37:
            style.fg = BASIC[c - 30 + (8 if style.bold else 0)]
        elif 90 <= c <= 97:
            style.fg = BASIC[c - 90 + 8]
        elif c == 39:
            style.fg = ""
        elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
            style.fg = _color256(codes[i + 2] % 256)
            i += 2
        elif c == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
            style.fg = "#{:02x}{:02x}{:02x}".format(*(min(255, v) for v in codes[i + 2:i + 5]))
            i += 4
        i += 1
    return style


class Parser:
    """Keeps the style across chunks, like a terminal."""

    def __init__(self) -> None:
        self.style = Style()
        self._rest = ""

    def feed(self, text: str) -> list[tuple[str, Style]]:
        text, self._rest = self._rest + text, ""
        if (cut := _PARTIAL.search(text, max(0, len(text) - 260))) is not None:
            text, self._rest = text[:cut.start()], text[cut.start():]  # finish it with the next chunk
        runs: list[tuple[str, Style]] = []
        pos = 0
        for m in _ESC.finditer(text):
            if m.start() > pos:
                runs.append((text[pos:m.start()], self.style.copy()))
            if m.group(2) == "m":
                self.style = _apply(self.style.copy(), m.group(1))
            pos = m.end()
        if pos < len(text):
            runs.append((text[pos:], self.style.copy()))
        return runs


def strip(text: str) -> str:
    return _ESC.sub("", text)
