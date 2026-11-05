"""Offscreen smoke tests of the window's parts, including the Licensing and Support buttons."""

from __future__ import annotations

import sys
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
    c.show()
    for i in range(5000):
        c.append(f"\x1b[3{i % 8}mline {i}\x1b[0m\n")
    qtbot.waitUntil(lambda: "line 4999" in c.view.toPlainText(), timeout=5000)
    assert c.view.document() is not None and c.view.document().blockCount() <= 1051  # type: ignore[union-attr]
    assert "\x1b" not in c.view.toPlainText()


def test_console_progress_bars_rewrite_one_line(qtbot: Any) -> None:
    c = Console(1000)
    qtbot.addWidget(c)
    c.show()
    c.append("start\n")
    for i in range(2000):
        c.append(f"\r\x1b[32m{i:4d}%\x1b[0m")  # a progress bar: no line feed, only carriage returns
    c.append("\r\ndone\r\n")
    qtbot.waitUntil(lambda: "done" in c.view.toPlainText(), timeout=5000)
    assert c.view.toPlainText().split("\n")[:3] == ["start", "1999%", "done"]


def test_console_hidden_keeps_only_the_newest(qtbot: Any) -> None:
    c = Console(100)
    qtbot.addWidget(c)  # never shown: nothing is drawn, and the waiting output stays bounded
    for i in range(20_000):
        c.append(f"line {i}\n")
    qtbot.wait(200)
    assert c.view.toPlainText() == "" and c._queued <= 100 * 150
    c.show()
    qtbot.waitUntil(lambda: "line 19999" in c.view.toPlainText(), timeout=5000)
    assert c.view.document().blockCount() <= 111  # type: ignore[union-attr]


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
    assert service.own_env and "no virtual environment yet" in dlg.env_line.text()
    dlg.own_env.setChecked(False)
    assert not dlg.build().own_env and "global packages" in dlg.env_line.text()
    dlg.runtime.setCurrentIndex(dlg.runtime.findData("shell"))
    assert not dlg.own_env.isEnabled()
    dlg.runtime.setCurrentIndex(dlg.runtime.findData("python"))
    dlg.own_env.setChecked(True)
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


def test_settings_start_mode_logs_and_sign_in(qtbot: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from pythonator.core.models import Settings
    from pythonator.ui import login
    from pythonator.ui.settings_dialog import ALL, FEW, SettingsDialog

    monkeypatch.setattr(login, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sent: list[dict[str, Any]] = []
    dlg = SettingsDialog(Settings(), lambda msg, *_: sent.append(msg))
    qtbot.addWidget(dlg)
    assert dlg.start_at_once() == 1 and not dlg.at_once.isEnabled()  # one after another by default
    dlg.mode.setCurrentIndex(FEW)
    dlg.at_once.setValue(3)
    dlg.log_mb.setValue(25)
    dlg.log_backups.setValue(7)
    dlg.login.setChecked(True)
    dlg.save()
    saved = sent[-1]["settings"]
    assert (saved["start_at_once"], saved["log_max_mb"], saved["log_backups"]) == (3, 25, 7)
    entry = (tmp_path / "autostart" / "pythonator.desktop").read_text()
    assert login.HIDDEN in entry and login.enabled()
    dlg = SettingsDialog(Settings.model_validate(saved), lambda msg, *_: sent.append(msg))
    qtbot.addWidget(dlg)
    assert dlg.login.isChecked() and dlg.start_at_once() == 3
    dlg.mode.setCurrentIndex(ALL)
    dlg.login.setChecked(False)
    dlg.save()
    assert sent[-1]["settings"]["start_at_once"] == 0 and not login.enabled()


def test_service_list_moves_and_removes_several(qtbot: Any, tmp_path: Path) -> None:
    from PyQt6.QtCore import QItemSelectionModel, Qt

    from pythonator.ui.service_list import ServiceList

    services = [Service(name=n, folder=str(tmp_path), entry="a.py") for n in ("a", "b", "c", "d")]
    view = ServiceList()
    qtbot.addWidget(view)
    view.set_services(services)
    orders: list[list[str]] = []
    removes: list[bool] = []
    view.reordered.connect(orders.append)
    view.remove_requested.connect(lambda: removes.append(True))
    sel = view.selectionModel()
    assert sel is not None
    flag = QItemSelectionModel.SelectionFlag
    sel.setCurrentIndex(view.rows.index(2), flag.ClearAndSelect)
    sel.select(view.rows.index(3), flag.Select)
    assert view.selected_ids() == [services[2].id, services[3].id]
    qtbot.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert orders == [[services[i].id for i in (0, 2, 3, 1)]]
    view.set_services([services[i] for i in (0, 2, 3, 1)])  # the background service's echo keeps the selection
    assert view.selected_ids() == [services[2].id, services[3].id]
    view.shift(-1)
    view.shift(-1)  # already at the top: nothing
    assert orders[-1] == [services[i].id for i in (2, 3, 0, 1)] and len(orders) == 2
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert removes == [True]


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


def test_plugin_panel_redraw_keeps_the_text_box_focus_and_cursor(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QApplication, QLabel, QPlainTextEdit

    from pythonator.sdk import ui

    dock = panels.PanelDock(lambda *a: None)
    qtbot.addWidget(dock)
    dock.show()
    qtbot.waitExposed(dock)
    dock.activateWindow()

    def panel(status: str) -> dict[str, Any]:
        return ui.panel("AI", ui.text(status), ui.row(ui.stat("Tokens", status), ui.input("note", "Note…")),
                        ui.input("ask", "Ask…"))

    dock.update_panel("ai", "AI", panel("idle"))
    edits = [w for w in dock.findChildren(QPlainTextEdit) if not w.isReadOnly()]
    assert len(edits) == 2
    ask = next(w for w in edits if w.placeholderText() == "Ask…")
    nested = next(w for w in edits if w.placeholderText() == "Note…")
    ask.setFocus()
    qtbot.keyClicks(ask, "half a ques")
    cursor = ask.textCursor()
    cursor.setPosition(4)
    ask.setTextCursor(cursor)
    for i in range(20):  # the plugin redraws on every tick and every streamed piece of an answer
        dock.update_panel("ai", "AI", panel(f"thinking {i}"))
    qtbot.wait(10)
    assert QApplication.focusWidget() is ask  # the same widget: never rebuilt, never loses focus
    assert ask.toPlainText() == "half a ques" and ask.textCursor().position() == 4
    assert nested in dock.findChildren(QPlainTextEdit)  # a text box inside a row is kept too
    labels = [w.text() for w in dock.findChildren(QLabel) if w.isVisible()]
    assert "thinking 19" in labels and "idle" not in labels  # but what changed is drawn again

    body = dock.pages["ai"].widget()
    dock.update_panel("ai", "AI", panel("thinking 19"))  # nothing changed: nothing is touched
    assert dock.pages["ai"].widget() is body
    dock.update_panel("ai", "AI", ui.panel("AI", ui.input("ask", "Ask again…", "Go")))
    qtbot.wait(10)
    assert ask.placeholderText() == "Ask again…" and QApplication.focusWidget() is ask


def test_plugins_dialog_refresh_keeps_a_half_typed_setting(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QLineEdit

    dlg = PluginsDialog(lambda *a: None)
    qtbot.addWidget(dlg)
    plugin = {"id": "x", "name": "X", "version": "1", "description": "d", "official": True, "folder": "/x",
              "enabled": True, "running": True, "error": "", "test_mode": False, "permissions": [],
              "granted": [], "settings": [{"key": "k", "label": "K", "kind": "text", "default": "",
                                           "choices": [], "help": ""}], "values": {"k": ""},
              "activity": [], "homepage": "", "sha256": "0" * 64}
    dlg.refresh([plugin])
    fields = [w for w in dlg.detail.findChildren(QLineEdit) if not w.isReadOnly()]
    assert fields
    fields[0].setText("half typed")
    dlg.refresh([{**plugin, "activity": ["a line"]}])  # the plugin logged something: same page
    assert fields[0] in dlg.detail.findChildren(QLineEdit) and fields[0].text() == "half typed"
    dlg.refresh([{**plugin, "running": False}])  # something shown changed: drawn again
    assert fields[0] not in [w for w in dlg.detail.findChildren(QLineEdit) if w.isVisible()]


def test_a_shown_log_appears_at_its_end_at_once(qtbot: Any) -> None:
    """Selecting a service shows its log tail already scrolled to the bottom, never scrolling through it."""
    console = Console(max_lines=1000)
    qtbot.addWidget(console)
    console.resize(600, 300)
    console.show()
    console.set_text("".join(f"line {i}\n" for i in range(5000)))
    assert not console.view.updatesEnabled()  # nothing half-drawn is painted
    qtbot.waitUntil(lambda: console.view.updatesEnabled(), timeout=5000)
    bar = console.view.verticalScrollBar()
    assert bar is not None and bar.value() == bar.maximum()
    assert "line 4999" in console.view.toPlainText() and "line 3000\n" not in console.view.toPlainText()
