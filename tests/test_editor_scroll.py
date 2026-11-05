"""The editor scrolls without waiting: big files are styled in idle time, and Shift+wheel scrolls a page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.Qsci import QsciScintilla
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

from pythonator.ui.editor import EditorPanel


def test_editor_scrolls_without_waiting(qtbot: Any, tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("def f(x):\n    return x  # c\n\n" * 20_000)
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.resize(900, 700)
    panel.show()
    panel.open(tmp_path / "big.py")
    ed = panel.current()
    assert ed is not None
    assert ed.SendScintilla(QsciScintilla.SCI_GETIDLESTYLING) == QsciScintilla.SC_IDLESTYLING_ALL
    # The whole file gets styled in the background, so Ctrl+End or a scrollbar drag never waits on the lexer.
    qtbot.waitUntil(lambda: ed.SendScintilla(QsciScintilla.SCI_GETENDSTYLED) == ed.length(), timeout=10_000)

    def wheel(mods: Qt.KeyboardModifier) -> int:
        ed.setFirstVisibleLine(0)
        QApplication.sendEvent(ed.viewport(), QWheelEvent(
            QPointF(50, 50), QPointF(50, 50), QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton, mods,
            Qt.ScrollPhase.NoScrollPhase, False))
        return ed.firstVisibleLine()

    assert wheel(Qt.KeyboardModifier.NoModifier) == QApplication.wheelScrollLines()  # the system's lines per notch
    assert wheel(Qt.KeyboardModifier.ShiftModifier) >= ed.SendScintilla(QsciScintilla.SCI_LINESONSCREEN) - 1
