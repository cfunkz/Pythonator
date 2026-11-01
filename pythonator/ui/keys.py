"""Keyboard shortcuts that follow each platform's conventions.

Qt already maps ``Ctrl`` to ⌘ on macOS, so most shortcuts are written once. The exceptions are
keys macOS reserves (⌘Space is Spotlight, ⌘H hides the app, ⌘G is "find next") and the F keys,
which on a Mac keyboard need Fn, so the usual Mac equivalents are added there (⌘R run, ⌘. stop).
Labels in tooltips use :func:`text`, so a Mac shows ⌘S where Windows and Linux show Ctrl+S.
"""

from __future__ import annotations

import sys

from PyQt6.QtGui import QKeySequence

IS_MAC = sys.platform == "darwin"

START = ("Ctrl+R", "F5") if IS_MAC else ("F5",)  # the first one is shown in tooltips
STOP = ("Ctrl+.", "Shift+F5") if IS_MAC else ("Shift+F5",)
NEW_SERVICE = "Ctrl+Shift+N"
CONFIGURE = "Ctrl+E"
QUIT = "Ctrl+Q"
SAVE = "Ctrl+S"
SAVE_AS = "Ctrl+Shift+S"
OPEN = "Ctrl+O"
CLOSE_TAB = "Ctrl+W"
FIND = "Ctrl+F"
REPLACE = ("Alt+Ctrl+F",) if IS_MAC else ("Ctrl+H",)  # ⌘H hides the app on macOS
GO_TO_LINE = "Ctrl+L" if IS_MAC else "Ctrl+G"  # ⌘G is "find next" on macOS
# Meta is the Control key on macOS (⌃Space, as in Xcode and VS Code); ⌘Space opens Spotlight.
COMPLETE = "Meta+Space" if IS_MAC else "Ctrl+Space"
COMMENT = "Ctrl+/"
DUPLICATE = "Ctrl+D"


def text(seq: str | tuple[str, ...]) -> str:
    """How the platform writes a shortcut: "Ctrl+S" on Windows / Linux, "⌘S" on macOS."""
    first = seq[0] if isinstance(seq, tuple) else seq
    return QKeySequence(first).toString(QKeySequence.SequenceFormat.NativeText)


def sequences(seq: str | tuple[str, ...]) -> list[QKeySequence]:
    return [QKeySequence(s) for s in ((seq,) if isinstance(seq, str) else seq)]


def unique(*seqs: QKeySequence) -> list[QKeySequence]:
    """``seqs`` without empty or repeated keys."""
    seen: set[str] = set()
    out: list[QKeySequence] = []
    for seq in seqs:
        key = seq.toString()
        if key and key not in seen:
            seen.add(key)
            out.append(seq)
    return out
