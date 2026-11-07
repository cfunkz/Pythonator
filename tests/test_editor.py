"""The editor and the file tree: big files scroll without waiting (styled in idle time, Shift+wheel scrolls a page),
line endings and undo, saves that fail or meet a change on disk, FIFOs and huge files, and a tree that doesn't watch."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from PyQt6.Qsci import QsciScintilla
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

from pythonator.paths import IS_WINDOWS
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


def test_editor_keeps_line_endings_and_undo_cannot_empty_the_file(qtbot: Any, tmp_path: Path) -> None:
    from PyQt6.Qsci import QsciScintilla

    from pythonator.ui.editor import EditorPanel

    crlf = tmp_path / "win.py"
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.open(crlf)
    ed = panel.current()
    assert ed is not None and ed.eolMode() == QsciScintilla.EolMode.EolWindows
    ed.undo()  # nothing to undo yet: opening isn't an edit
    assert ed.text() == "a = 1\r\nb = 2\r\n" and not ed.isModified()
    ed.insertAt("c = 3\r\n", 2, 0)  # an edit, as typing or pasting makes one
    panel.save()
    assert crlf.read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"  # never \r\r\n, on any system
    ed.undo()
    ed.undo()  # one step too many: the file stays, it never empties
    assert ed.text() == "a = 1\r\nb = 2\r\n"
    latin = tmp_path / "latin.txt"
    latin.write_bytes(b"caf\xe9\n")
    panel.open(latin)
    other = panel.current()
    assert other is not None and other.read_only and other.encoding_note


def test_a_failed_save_keeps_the_tab_open(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from pythonator.ui import editor as editor_module
    from pythonator.ui.editor import CodeEditor, EditorPanel

    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.open(path)
    ed = panel.current()
    assert isinstance(ed, CodeEditor)
    ed.setText("x = 2\n")
    warned: list[str] = []
    monkeypatch.setattr(editor_module, "ask", lambda *a, **k: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(editor_module, "warn", lambda _p, _t, text, *a, **k: warned.append(text))

    def refuse() -> bool:
        raise PermissionError("in use")

    monkeypatch.setattr(ed, "save", refuse)
    panel.close_tab(panel.tabs.indexOf(ed))
    assert panel.tabs.indexOf(ed) >= 0 and warned  # still open, and the user was told


def test_saving_over_a_change_on_disk_asks_first(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from pythonator.ui import editor as editor_module
    from pythonator.ui.editor import EditorPanel

    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.open(path)
    ed = panel.current()
    assert ed is not None
    ed.setText("x = 2\n")
    path.write_text("x = 3  # pulled\n")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
    monkeypatch.setattr(editor_module, "ask", lambda *a, **k: QMessageBox.StandardButton.Cancel)
    panel.save()
    assert path.read_text() == "x = 3  # pulled\n"


def test_file_tree_doesnt_list_generated_folders(qtbot: Any, tmp_path: Path) -> None:
    from pythonator.ui.editor import NOT_LISTED, FileTree

    for name in ("node_modules", ".venv", "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.js").write_text("")
    tree = FileTree()
    qtbot.addWidget(tree)
    tree.set_root(str(tmp_path))
    qtbot.waitUntil(lambda: tree.model.rowCount(tree.view.rootIndex()) == 3, timeout=5000)
    model = tree.model
    assert {"node_modules", ".venv"} <= NOT_LISTED
    assert not model.hasChildren(model.index(str(tmp_path / "node_modules")))
    assert not model.canFetchMore(model.index(str(tmp_path / ".venv")))
    assert model.hasChildren(model.index(str(tmp_path / "src")))


def test_a_file_tree_never_outlives_its_icon_provider(qtbot: Any, tmp_path: Path) -> None:
    """Python freed a dropped tree's icon provider before Qt deleted its model, which still pointed at it: the next
    event crashed macOS CI. The provider now lives as long as the program."""
    import gc
    import weakref

    from pythonator.ui.editor import FileTree

    (tmp_path / "src").mkdir()
    order: list[str] = []
    tree = FileTree()
    tree.set_root(str(tmp_path))
    qtbot.wait(100)
    tree.model.destroyed.connect(lambda *_: order.append("model"))
    provider = weakref.ref(tree.icons, lambda _r: order.append("icons"))
    del tree
    gc.collect()
    qtbot.wait(50)
    assert "icons" not in order and provider() is not None


posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX files and process groups")


@posix
def test_opening_a_fifo_doesnt_hang_the_window(qtbot: Any, tmp_path: Path) -> None:
    from pythonator.ui.editor import EditorPanel

    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    panel = EditorPanel()
    qtbot.addWidget(panel)
    said: list[str] = []
    panel.status.connect(said.append)
    started = time.monotonic()
    panel.open(fifo)
    assert time.monotonic() - started < 2 and said and "isn't a regular file" in said[0]


def test_the_file_tree_doesnt_watch_and_refresh_shows_new_files(qtbot: Any, tmp_path: Path) -> None:
    from PyQt6.QtGui import QFileSystemModel

    from pythonator.ui.editor import FileTree

    (tmp_path / "sub").mkdir()
    tree = FileTree()
    qtbot.addWidget(tree)
    tree.show()
    tree.set_root(str(tmp_path))
    assert tree.model.testOption(QFileSystemModel.Option.DontWatchForChanges)
    qtbot.waitUntil(lambda: tree.model.rowCount(tree.view.rootIndex()) == 1, timeout=5000)
    tree.view.expand(tree.model.index(str(tmp_path / "sub")))
    (tmp_path / "new.txt").write_text("x")
    tree.refresh()
    qtbot.waitUntil(lambda: tree.model.rowCount(tree.view.rootIndex()) == 2, timeout=5000)
    qtbot.waitUntil(lambda: tree.view.isExpanded(tree.model.index(str(tmp_path / "sub"))), timeout=5000)
