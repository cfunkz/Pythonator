"""Service output for the console: ANSI colours (SGR) kept as colours, every other escape sequence dropped, and
control characters shown as visible marks (␇, ␈, ␛…) so output can't hide text, move the cursor or reorder lines.
Pure Python, so it is tested without a display."""

from __future__ import annotations

import re

# CSI … final byte, OSC … BEL/ST, and two-byte escapes. Only CSI … m (SGR) is kept, as colour.
ESCAPE = re.compile(r"\x1b(?:\[([0-9;:?<=>]*)([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
# Control characters as their visible pictures (␀…␟, ␡); C1 controls and bidi overrides or isolates, which can make
# a line read differently from what it is (Trojan Source), as �.
SHOW = {o: chr(0x2400 + o) for o in range(32)} | {9: "    ", 0x7F: "\u2421"} | dict.fromkeys(
    (*range(0x80, 0xA0), *range(0x202A, 0x202F), *range(0x2066, 0x206A)), "\ufffd")
# black red green yellow blue magenta cyan white
BASIC = ("faint", "bad", "ok", "warn", "info", "m_full", "m_wsl", "console_text")
CUBE = (0, 95, 135, 175, 215, 255)

def _xterm(n: int) -> str:
    if n < 8:
        return "tone:" + BASIC[n]
    if n < 16:
        return "tone:" + ("faint" if n == 8 else BASIC[n - 8])
    if n < 232:
        n -= 16
        return f"#{CUBE[n // 36]:02x}{CUBE[n // 6 % 6]:02x}{CUBE[n % 6]:02x}"
    g = 8 + (n - 232) * 10
    return f"#{g:02x}{g:02x}{g:02x}"

def _sgr(params: str, fg: str | None, bold: bool) -> tuple[str | None, bool]:
    nums = [int(x) if x.isdigit() else 0 for x in params.replace(":", ";").split(";")] if params else [0]
    i = 0
    while i < len(nums):
        c = nums[i]
        if c == 0:
            fg, bold = None, False
        elif c == 1:
            bold = True
        elif c == 22:
            bold = False
        elif 30 <= c <= 37:
            fg = "tone:" + BASIC[c - 30]
        elif 90 <= c <= 97:
            fg = "tone:" + ("faint" if c == 90 else BASIC[c - 90])
        elif c == 39:
            fg = None
        elif c in (38, 48) and i + 1 < len(nums):
            if nums[i + 1] == 5 and i + 2 < len(nums):
                if c == 38:
                    fg = _xterm(nums[i + 2] % 256)
                i += 2
            elif nums[i + 1] == 2 and i + 4 < len(nums):
                if c == 38:
                    fg = "#{:02x}{:02x}{:02x}".format(*(min(255, v) for v in nums[i + 2:i + 5]))
                i += 4
        i += 1
    return fg, bold

def segments(text: str) -> list[tuple[str, str | None, bool]]:
    """(text, colour, bold) runs. Colour is None (the console's own), "tone:<name>" or "#rrggbb"."""
    if "\r" in text:  # a progress bar redrawn with carriage returns: what a terminal would show last
        text = text.rstrip("\r").rsplit("\r", 1)[-1]
    out: list[tuple[str, str | None, bool]] = []
    fg: str | None = None
    bold = False
    pos = 0
    for m in ESCAPE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()].translate(SHOW), fg, bold))
        if m.group(2) == "m":
            fg, bold = _sgr(m.group(1) or "", fg, bold)
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:].translate(SHOW), fg, bold))
    return [s for s in out if s[0]]

def plain(text: str) -> str:
    """The text without colours, for copying and searching."""
    return "".join(s for s, _c, _b in segments(text))
