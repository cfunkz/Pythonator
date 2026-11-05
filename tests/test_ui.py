"""Offscreen smoke tests of the window's parts, including the Licensing and Support buttons."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QLabel, QPushButton

from pythonator.core.models import Service
from pythonator.ui import panels, theme
from pythonator.ui.about import SUPPORT, AboutDialog, LicensingDialog, SupportDialog, SystemCheckDialog
from pythonator.ui.console import Console
from pythonator.ui.editor import EditorPanel
from pythonator.ui.plugins_view import PluginsDialog
from pythonator.ui.service_dialog import ServiceDialog


def test_licensing_and_support(qtbot: Any) -> None:
    lic = LicensingDialog()
    qtbot.addWidget(lic)
    assert any("GNU General Public License" in label.text() for label in lic.findChildren(QLabel))
    sup = SupportDialog()
    qtbot.addWidget(sup)
    labels = {b.text() for b in sup.findChildren(QPushButton)}
    assert {title for title, _url, _ in SUPPORT} <= labels
    assert all(url.startswith("https://") for _t, url, _ in SUPPORT)
    about = AboutDialog()
    qtbot.addWidget(about)
    text = " ".join(label.text() for label in about.findChildren(QLabel))
    assert "Made by <b>cfunkz</b>" in text and "Pythonator 0.0" in text


def test_system_check(qtbot: Any) -> None:
    dlg = SystemCheckDialog({"app": [["ok", "Python 3.12"]], "sandbox": [["Network off", "enforced"]]})
    qtbot.addWidget(dlg)


def test_console_handles_floods_and_colours(qtbot: Any) -> None:
    c = Console(1000)
    qtbot.addWidget(c)
    for i in range(5000):
        c.append(f"\x1b[3{i % 8}mline {i}\x1b[0m\n")
    qtbot.waitUntil(lambda: "line 4999" in c.view.toPlainText(), timeout=5000)
    assert c.view.document() is not None and c.view.document().blockCount() <= 1001  # type: ignore[union-attr]
    assert "\x1b" not in c.view.toPlainText()


def test_editor_opens_and_saves(qtbot: Any, tmp_path: Path) -> None:
    (tmp_path / "main.go").write_text("package main\n")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.set_root(str(tmp_path))
    panel.open(tmp_path / "main.go")
    editor = panel.current()
    assert editor is not None
    editor.setText("package main\n\nfunc main() {}\n")
    assert panel.unsaved()
    panel.save()
    assert "func main" in (tmp_path / "main.go").read_text()
    assert not panel.unsaved()


def test_editor_zoom_is_one_size_for_every_file(qtbot: Any, tmp_path: Path) -> None:
    """Ctrl+ / Ctrl- (the editor's own keys), Ctrl+= and Ctrl+0 all change one zoom, shared by every open file,
    with the line numbers measured at that size."""
    from PyQt6.Qsci import QsciScintilla
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from pythonator.ui.editor import BASE_SIZE, CodeEditor

    for name in ("a.py", "b.txt"):
        (tmp_path / name).write_text("print(1)\n" * 50)
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.show()
    panel.open(tmp_path / "a.py")
    panel.open(tmp_path / "b.txt")
    first, second = (panel.tabs.widget(i) for i in range(2))
    assert isinstance(first, CodeEditor) and isinstance(second, CodeEditor)
    width = first.marginWidth(0)
    for _ in range(3):
        QTest.keyClick(second, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)  # type: ignore[call-overload]
    panel.zoom(1)  # what Ctrl+= does
    assert panel.font_size == BASE_SIZE + 4
    for ed in (first, second):
        assert ed.SendScintilla(QsciScintilla.SCI_GETZOOM) == 4 and ed.font_size == panel.font_size
    assert first.marginWidth(0) > width
    for _ in range(20):
        QTest.keyClick(second, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)  # type: ignore[call-overload]
    assert panel.font_size >= 6
    panel.set_font_size(BASE_SIZE)  # Ctrl+0
    assert first.SendScintilla(QsciScintilla.SCI_GETZOOM) == 0 and first.marginWidth(0) == width


def test_service_dialog_builds_a_valid_service(qtbot: Any, tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text("")
    dlg = ServiceDialog(None, [("Network off", "enforced")], preset={"name": "bot", "folder": str(tmp_path),
                                                                    "entry": "bot.py", "runtime": "python"})
    qtbot.addWidget(dlg)
    service = dlg.build()
    assert isinstance(service, Service) and service.name == "bot" and service.mode.value == "sandbox"
    assert service.memory_mb == 0 and service.cpu_percent == 0
    dlg.memory.setText("1.5G")
    dlg.cpu.setText("50%")
    service = dlg.build()
    assert (service.memory_mb, service.cpu_percent) == (1536, 50)
    assert "1.5 GB" in dlg.memory_note.text() and "0.5 cores" in dlg.cpu_note.text()
    dlg.memory.setText("lots")
    assert "512M" in dlg.memory_note.text()
    with pytest.raises(ValueError, match="Memory limit"):
        dlg.build()


def test_plugin_panels_render_text_as_plain_text(qtbot: Any) -> None:
    widget = panels.render("p", {"type": "text", "text": "<img src=x onerror=alert(1)>"}, lambda *a: None)
    qtbot.addWidget(widget)
    assert widget.textFormat().name == "PlainText"  # type: ignore[attr-defined]


def test_plugins_dialog(qtbot: Any) -> None:
    sent: list[Any] = []
    dlg = PluginsDialog(lambda *a: sent.append(a))
    qtbot.addWidget(dlg)
    dlg.refresh([{"id": "x", "name": "X", "version": "1", "description": "d", "official": False, "folder": "/x",
                  "enabled": False, "running": False, "error": "", "test_mode": True, "permissions": ["network"],
                  "granted": [], "settings": [{"key": "k", "label": "K", "kind": "secret", "default": "",
                                               "choices": [], "help": ""}], "values": {"k": "••••••"},
                  "activity": [], "homepage": "", "sha256": "0" * 64}])
    assert dlg.list.count() == 1


def test_both_themes_apply(qapp: Any) -> None:
    for name in ("light", "dark"):
        theme.apply(qapp, name)
        assert theme.color("bg")
