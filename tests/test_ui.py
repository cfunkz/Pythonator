"""Offscreen smoke tests of the window's parts, including the Licensing and Support buttons."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QPushButton

from pythonator import errors
from pythonator.core.models import Service
from pythonator.paths import DataDirs
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

    dock = panels.PanelPage("ai", lambda *a: None)
    qtbot.addWidget(dock)
    dock.show()
    qtbot.waitExposed(dock)
    dock.activateWindow()

    def panel(status: str) -> dict[str, Any]:
        return ui.panel("AI", ui.text(status), ui.row(ui.stat("Tokens", status), ui.input("note", "Note…")),
                        ui.input("ask", "Ask…"))

    dock.show_panel(panel("idle"))
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
        dock.show_panel(panel(f"thinking {i}"))
    qtbot.wait(10)
    assert QApplication.focusWidget() is ask  # the same widget: never rebuilt, never loses focus
    assert ask.toPlainText() == "half a ques" and ask.textCursor().position() == 4
    assert nested in dock.findChildren(QPlainTextEdit)  # a text box inside a row is kept too
    labels = [w.text() for w in dock.findChildren(QLabel) if w.isVisible()]
    assert "thinking 19" in labels and "idle" not in labels  # but what changed is drawn again

    body = dock.widget()
    dock.show_panel(panel("thinking 19"))  # nothing changed: nothing is touched
    assert dock.widget() is body
    dock.show_panel(ui.panel("AI", ui.input("ask", "Ask again…", "Go")))
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


def test_find_bar_counts_steps_and_replaces(qtbot: Any, tmp_path: Path) -> None:
    from PyQt6.QtCore import Qt

    path = tmp_path / "a.py"
    path.write_text("x = 1  # é\nfoo(x)\nprint(x, xx)\n", encoding="utf-8")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.show()
    panel.open(path)
    bar, ed = panel.findbar, panel.current()
    assert ed is not None
    bar.open()
    bar.find_edit.setText("x")
    bar.refresh()
    assert len(bar.matches) == 5 and bar.count.text() == "5 found"
    bar.word.setChecked(True)  # "xx" no longer counts
    assert len(bar.matches) == 3
    assert bar.step() and bar.count.text().startswith("1 of 3") and ed.selectedText() == "x"
    assert bar.step() and ed.getSelection()[0] == 1 and ed.selectedText() == "x"  # after the é: UTF-8 offsets
    qtbot.keyClick(bar.find_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)  # back
    qtbot.keyClick(bar.find_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)  # and wraps to the last
    assert bar.count.text() == "3 of 3"
    bar.regex.setChecked(True)
    bar.find_edit.setText(r"print\((\w+)")
    bar.refresh()
    bar.replace_edit.setText(r"show(\1")
    bar.replace_all()
    assert "show(x, xx)" in ed.text() and "é" in ed.text()  # UTF-8 offsets: the é before it didn't shift it
    bar.find_edit.setText("(")
    bar.refresh()
    assert bar.count.text() == "Invalid"
    qtbot.keyClick(bar.find_edit, Qt.Key.Key_Escape)
    assert not bar.isVisible()


def test_plugin_chat_updates_in_place_and_follows_the_end(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QPlainTextEdit

    from pythonator.sdk import ui

    dock = panels.PanelPage("ai", lambda *a: None)
    qtbot.addWidget(dock)
    dock.resize(380, 500)
    dock.show()
    qtbot.waitExposed(dock)

    def panel(answer: str, *more: dict[str, Any]) -> dict[str, Any]:
        return ui.panel("AI", ui.text("head"), ui.chat(ui.message("you", "Why?"), ui.message("ai", answer),
                                                       ui.message("ai", "x = 1", code=True), *more))

    dock.show_panel(panel("Because"))
    chat = dock.findChild(panels.ChatView)
    assert chat is not None and len(chat.rows) == 2  # the answer's text and code share one bubble
    first = chat.rows[1][1].widgets[0]
    assert isinstance(first, panels.ChatText) and isinstance(chat.rows[1][1].widgets[1], QPlainTextEdit)
    dock.show_panel(panel("Because the port is taken"))  # a streamed piece
    assert dock.findChild(panels.ChatView) is chat and chat.rows[1][1].widgets[0] is first
    assert first.text() == "Because the port is taken"
    for i in range(30):
        dock.show_panel(panel("Because", *[ui.message("note", f"line {n}") for n in range(i)]))
    qtbot.wait(20)
    bar = chat.verticalScrollBar()
    assert bar is not None and bar.maximum() > 0 and bar.value() == bar.maximum()  # follows the end
    dock.show_panel(panel("Again", ui.message("error", "404: not found")))
    assert [b.role for _, b in chat.rows] == ["you", "ai", "error"]


def test_plugin_select_shows_what_the_plugin_chose(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QComboBox

    picked: list[str] = []
    widget = panels.render("p", {"type": "select", "action": "quick", "options": ["Pick…", "A", "B"],
                                 "value": "Pick…"}, lambda _p, _a, v: picked.append(v))
    qtbot.addWidget(widget)
    combo = widget.findChild(QComboBox)
    assert combo is not None
    combo.activated.emit(2)
    assert picked == ["B"] and combo.currentText() == "Pick…"  # a one-shot pick goes back to the placeholder


def test_plugin_settings_labels_are_never_cut_off(qtbot: Any) -> None:
    """A long setting label wraps onto as many lines as it needs. Notify's settings in a QFormLayout gave a
    three-line label the height of its check box or number box, so its top and bottom were cut off."""
    import tomllib

    from PyQt6.QtWidgets import QCheckBox, QScrollArea

    manifest = tomllib.loads((Path(__file__).parents[1] / "plugins" / "notify" / "plugin.toml").read_text("utf-8"))
    settings = [{"choices": [], "help": "", "default": "", **s} for s in manifest["plugin"]["settings"]]
    dlg = PluginsDialog(lambda *a: None)
    qtbot.addWidget(dlg)
    box = dlg._settings({"id": "notify", "settings": settings, "values": {}})
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(box)
    qtbot.addWidget(area)
    for width in (340, 470, 900):
        area.resize(width, 600)  # shorter than the settings: they scroll, and must not be squeezed
        area.show()
        qtbot.wait(20)
        for w in box.findChildren(QLabel):
            if w.isVisible() and w.wordWrap() and w.text():
                assert w.height() >= w.heightForWidth(w.width()), (width, w.text(), w.height())
    bools = [s for s in settings if s["kind"] == "bool"]
    checks = box.findChildren(QCheckBox)
    assert len(checks) == len(bools) and [c.isChecked() for c in checks] == [bool(s["default"]) for s in bools]


def test_review_window_for_several_files(qtbot: Any) -> None:
    from pythonator.ui.proposal_dialog import ProposalDialog

    dlg = ProposalDialog({"id": "0" * 16, "plugin": "pythonator-ai", "plugin_name": "AI assistant", "path": "main.py",
                          "paths": ["main.py", "static/index.html"], "new_files": ["static/index.html"],
                          "service_name": "web", "folder": "/x", "new_file": False, "warnings": [],
                          "diff": "--- a/main.py\n+++ b/main.py\n+x\n\n--- /dev/null\n+++ b/static/index.html\n+y"},
                         lambda *a: None)
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "Review a change: main.py and 1 more"
    assert dlg.apply_button.text() == "Apply all 2 files"
    text = " ".join(w.text() for w in dlg.findChildren(QLabel))
    assert "changes to 2 files" in text and "new  static/index.html" in text and "changed  main.py" in text


def test_env_table_adds_deletes_and_imports(qtbot: Any, tmp_path: Path) -> None:
    from pythonator.ui.service_dialog import ServiceDialog

    (tmp_path / ".env").write_text("A=1\nTOKEN=x\n")
    dlg = ServiceDialog(None, [], preset={"name": "s", "folder": str(tmp_path), "runtime": "python",
                                          "entry": "x.py", "env": {"A": "keep"}})
    qtbot.addWidget(dlg)
    dlg.env.add("B", "2")
    dlg.env.add("C", "3")
    dlg.env.selectRow(1)
    dlg.env.delete_selected()
    assert dlg.env.pairs() == [("A", "keep"), ("C", "3")]
    dlg.env_file.setText(".env")
    dlg._import_env_file()
    assert dlg.env.pairs() == [("A", "keep"), ("C", "3"), ("TOKEN", "x")] and "1 variable" in dlg.env_note.text()


def test_system_check_says_what_the_computer_is(qtbot: Any, tmp_path: Path) -> None:
    from PyQt6.QtWidgets import QLabel

    from pythonator.core import sysinfo
    from pythonator.ui.about import SystemCheckDialog

    facts = sysinfo.facts(tmp_path)
    computer = dict(facts["This computer"])
    assert computer["System"] and computer["Processor"] and "GB" in computer["Memory"]
    dlg = SystemCheckDialog({"app": [["ok", "Python 3.13"]], "sandbox": [["Network off", "enforced"],
                                                                      ["Only listed ports", "not enforced"]]},
                            data_folder=tmp_path)
    qtbot.addWidget(dlg)
    text = " ".join(w.text() for w in dlg.findChildren(QLabel))
    assert "1 of 2 protections enforced" in text and "Network off" in text and computer["System"] in text
    qtbot.waitUntil(lambda: any(line.startswith("Python:") for line in dlg.report), timeout=30_000)
    assert sys.version.split()[0] in "\n".join(dlg.report) or "Python" in "\n".join(dlg.report)


def test_markdown_shows_no_html_and_loads_nothing(qtbot: Any) -> None:
    from pythonator.ui.panels import ChatText, markdown_html

    html = markdown_html("# Fix\n\n**bold** <b>raw</b> ![x](file:///etc/passwd) [docs](https://example.com)")
    assert "<img" not in html and "file:///etc/passwd" not in html  # images are dropped: nothing is loaded
    assert "&lt;b&gt;raw&lt;/b&gt;" in html and "https://example.com" in html  # HTML stays text, links stay
    text = ChatText(markdown=True)
    qtbot.addWidget(text)
    text.set_text("**bold**")
    assert "bold" in text.text() and "**" not in text.text()
    plain = ChatText()
    qtbot.addWidget(plain)
    plain.set_text("**not markdown** <b>x</b>")
    assert plain.text() == "**not markdown** <b>x</b>" and plain.textFormat() == Qt.TextFormat.PlainText


def test_tables_are_updated_in_place(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QTableWidget

    from pythonator.sdk import ui
    from pythonator.ui.panels import PanelPage

    page = PanelPage("n", lambda *a: None)
    qtbot.addWidget(page)
    page.show_panel(ui.panel("N", ui.table(["Service", "State"], [["api", "running"]], ["ok"])))
    table = page.findChild(QTableWidget)
    assert table is not None
    page.show_panel(ui.panel("N", ui.table(["Service", "State"], [["api", "failed"], ["web", "running"]])))
    assert page.findChild(QTableWidget) is table and table.rowCount() == 2
    item = table.item(0, 1)
    assert item is not None and item.text() == "failed"


def test_plugin_panels_are_tabs(qtbot: Any) -> None:
    from pythonator.sdk import ui
    from pythonator.ui.panels import PanelDock

    dock = PanelDock(lambda *a: None)
    qtbot.addWidget(dock)
    dock.update_panel("notify", "Notify", ui.panel("Notify", ui.text("ok")))
    dock.update_panel("ai", "AI", ui.panel("AI assistant", ui.text("hi")))
    assert dock.count() == 2 and [dock.tabText(i) for i in range(2)] == ["Notify", "AI assistant"]
    dock.update_panel("ai", "", None)
    assert dock.count() == 1 and "ai" not in dock.pages


def test_a_row_wraps_instead_of_cutting_off(qtbot: Any) -> None:
    from pythonator.sdk import ui
    from pythonator.ui.panels import FlowRow, PanelPage

    page = PanelPage("notify", lambda *a: None)
    qtbot.addWidget(page)
    page.show_panel(ui.panel("Notify", ui.row(ui.stat("Running", "8 / 17"), ui.stat("Problems, 24 h", "151"),
                                              ui.stat("Health checks", "0 / 1"))))
    from PyQt6.QtWidgets import QWidget

    flow = next(f for w in page.findChildren(QWidget) if isinstance(f := w.layout(), FlowRow))
    assert isinstance(flow, FlowRow) and flow.count() == 3
    wide, narrow = flow.heightForWidth(900), flow.heightForWidth(260)
    assert narrow > wide  # two lines, not a row wider than the panel
    assert flow.minimumSize().width() < 260


def test_the_meter_shows_pythonators_share(qtbot: Any) -> None:
    from pythonator.ui.system_meter import SystemMeter

    meter = SystemMeter()
    qtbot.addWidget(meter)
    meter.set_own(cpu=200.0, memory=300 * 1024**2)
    meter.sample()
    assert 0 <= meter.own_cpu <= (meter.cpu or 0) + 0.01 and 300 * 1024**2 <= meter.own_mem <= meter.used
    assert "Pythonator" in meter.toolTip()
    meter.grab()  # paints both shades


def _inside(page: Any) -> list[str]:
    """Widgets of the page that reach past its right edge."""
    from PyQt6.QtWidgets import QWidget

    body, edge = page.widget(), page.viewport().width()
    out = [f"the page is {body.width()} px in a {edge} px dock"] if body.width() > edge else []
    for w in body.findChildren(QWidget):
        if not w.isVisibleTo(body):
            continue
        right = w.mapTo(body, w.rect().topRight()).x()
        if right > edge + 1:
            out.append(f"{type(w).__name__} {w.objectName()} reaches {right} > {edge}")
    return out


@pytest.mark.parametrize("width", [240, 300, 420])
def test_no_panel_reaches_past_the_dock(qtbot: Any, width: int) -> None:
    from pythonator.sdk import ui
    from pythonator.ui.panels import PANEL_MIN_WIDTH, PanelPage

    page = PanelPage("p", lambda *a: None)
    qtbot.addWidget(page)
    long = "C:\\Users\\someone\\Pythonator Projects\\a-very-long-project-name\\node_modules\\vite\\bin\\vite.js"
    page.show_panel(ui.panel(
        "P", ui.tabs("page", ["New project", "Projects (12)"], "New project"),
        ui.row(ui.select("t", ["React (Vite) with a very long option name indeed"], label="Template"),
               ui.button("Refresh", "r", icon="refresh")),
        ui.text(long), ui.input("create", long[:100], "Create"),
        ui.row(ui.button("Create my-react-application-with-a-long-name", "c"), ui.button("Delete…", "d")),
        ui.row(*(ui.stat(f"Statistic {i}", "123 / 456") for i in range(4))),
        ui.table(["Project", "Template", "State", "Folder"], [["my-react", "react", "failed", long]] * 3)))
    page.show()
    page.resize(width, 700)
    body, port = page.widget(), page.viewport()
    assert body is not None and port is not None
    qtbot.waitUntil(lambda: body.width() == port.width())
    assert page.minimumWidth() == PANEL_MIN_WIDTH
    assert _inside(page) == []


def test_a_button_ends_in_an_ellipsis_instead_of_widening_the_panel(qtbot: Any) -> None:
    from pythonator.ui.panels import FitButton, breakable

    b = FitButton("Create my-react-application-with-a-long-name")
    qtbot.addWidget(b)
    assert b.minimumSizeHint().width() <= 56 < b.sizeHint().width()
    b.resize(80, 30)
    b.show()
    assert b.text().endswith("…") and b.toolTip() == b.full
    assert "\u200b" in breakable("C:\\Users\\someone\\Pythonator Projects\\my-react\\node_modules")
    assert breakable("short words only") == "short words only"


def test_the_host_checks_tabs_and_icons() -> None:
    from pydantic import ValidationError

    from pythonator.plugins.host import Panel
    from pythonator.sdk import ICONS, ui

    Panel.model_validate(ui.panel("ok", ui.tabs("p", ["A", "B"], "A"), ui.button("Refresh", "r", icon="refresh")))
    for bad in (ui.tabs("p", ["only one"], "x"), ui.tabs("p", [str(i) for i in range(7)], "0"),
                ui.button("x", "x", icon="https://example.com/a.png")):
        with pytest.raises(ValidationError):
            Panel.model_validate(ui.panel("bad", bad))
    from pythonator.ui import icons

    assert set(ICONS) <= set(icons.SHAPES)


def test_tabs_switch_views(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QPushButton

    from pythonator.sdk import ui
    from pythonator.ui.panels import PanelPage

    got: list[tuple[str, str]] = []
    page = PanelPage("p", lambda _p, a, v: got.append((a, v)))
    qtbot.addWidget(page)
    page.show_panel(ui.panel("P", ui.tabs("page", ["Services", "Events"], "Services")))
    buttons = [b for b in page.findChildren(QPushButton) if b.property("segment")]
    assert [b.isChecked() for b in buttons] == [True, False]
    buttons[1].click()
    assert got == [("page", "Events")]


def test_service_details_are_in_a_menu(qtbot: Any, dirs: DataDirs, tmp_path: Path) -> None:
    from PyQt6.QtWidgets import QMenu

    from pythonator.ui.main_window import MainWindow

    window = MainWindow(dirs)
    qtbot.addWidget(window)
    try:
        service = Service.model_validate({"name": "web", "folder": str(tmp_path), "runtime": "python",
                                          "entry": "app.py"})
        window.services[service.id] = service
        window.current = service.id
        window.states[service.id] = {"state": "running", "pid": 4242}
        window.stats[service.id] = {"cpu": 3.5, "memory": 50 * 2**20, "ports": [8000]}
        rows = dict(window.details(service))
        assert rows["Process"] == "pid 4242" and rows["Listening on"] == "8000" and rows["Folder"] == str(tmp_path)
        menu = window._details_menu(QMenu(window))
        texts = [a.text() for a in menu.actions()]
        assert any(t.startswith("Process\tpid 4242") for t in texts) and "Copy all details" in texts
        window._update_header()
        assert "pid" not in window.h_info.full and "pid 4242" in window.h_info.toolTip()
        assert all(not b.text() or b.text() == "" for b in window.buttons)  # icons, the words in tooltips
    finally:
        window.client.close()


def _panel(n: int, text: str = "word ") -> dict[str, Any]:
    return {"title": f"P{n}", "items": [{"type": "text", "text": text * 40} for _ in range(10)]}


def test_redraws_are_coalesced_and_hidden_tabs_wait(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.ui.panels import REDRAW_MS, PanelDock

    dock = PanelDock(lambda *a: None)
    qtbot.addWidget(dock)
    dock.show()
    drawn: list[str] = []
    dock.update_panel("a", "A", _panel(0))
    dock.update_panel("b", "B", _panel(0))
    original = PanelDock._draw

    def counted(self: PanelDock, page: Any, name: str, panel: dict[str, Any]) -> None:
        drawn.append(page.plugin)
        original(self, page, name, panel)

    monkeypatch.setattr(PanelDock, "_draw", counted)
    for i in range(1, 20):  # a streamed answer: twenty updates
        dock.update_panel("a", "A", _panel(i))
        dock.update_panel("b", "B", _panel(i))
    assert drawn == [] and dock.tabText(dock.indexOf(dock.pages["a"])) == "P19"
    qtbot.wait(REDRAW_MS * 3)
    assert drawn == ["a"] and dock.pages["a"].title == "P19"  # once, the latest; b is hidden
    assert "b" in dock.pending
    dock.setCurrentWidget(dock.pages["b"])
    assert drawn == ["a", "b"] and not dock.pending
    dock.update_panel("a", "A", None)
    assert "a" not in dock.pages


def test_a_resize_lays_the_panel_out_once_it_rests(qtbot: Any) -> None:
    from pythonator.ui.panels import REFLOW_MS, PanelPage

    page = PanelPage("p", lambda *a: None)
    qtbot.addWidget(page)
    page.resize(400, 300)
    page.show()
    page.show_panel(_panel(1))
    body, port = page.widget(), page.viewport()
    assert body is not None and port is not None
    qtbot.waitUntil(lambda: body.width() == port.width())
    first = body.width()
    for width in range(400, 600, 10):  # a drag
        page.resize(width, 300)
        qtbot.wait(5)
    assert body.width() == first  # not at every step
    qtbot.wait(REFLOW_MS * 3)
    assert body.width() == port.width() != first


def test_a_label_remembers_its_height_per_width(qtbot: Any) -> None:
    from pythonator.ui.panels import WrapLabel

    label = WrapLabel("word " * 200)
    qtbot.addWidget(label)
    h1 = label.heightForWidth(200)
    assert label.heightForWidth(200) == h1 and len(label._heights) == 1
    assert label.heightForWidth(400) < h1 and len(label._heights) == 2
    label.setText("short")
    assert label._heights == {} and label.heightForWidth(200) < h1
    for w in range(100, 140):
        label.heightForWidth(w)
    assert len(label._heights) <= 33
    assert label.textFormat().name == "PlainText"  # a plugin's text is never HTML


def test_labels_and_tooltips_never_read_html(qtbot: Any) -> None:
    from PyQt6.QtCore import Qt

    from pythonator.ui.widgets import label, tip

    assert label("<img src='/dev/zero'>").textFormat() == Qt.TextFormat.PlainText
    assert tip("plain name") == "plain name"
    assert tip("<img src='/dev/zero'>") == "<qt>&lt;img src=&#x27;/dev/zero&#x27;&gt;</qt>"


ROOT = Path(__file__).resolve().parent.parent


def test_a_worker_thread_can_report_after_the_window_closed(qtbot: Any) -> None:
    from PyQt6 import sip

    from pythonator.ui.service_dialog import _Bridge

    bridge = _Bridge()
    got: list[str] = []
    bridge.text.connect(got.append)
    bridge.send("one")
    sip.delete(bridge)  # the window, its parent, closed while the thread still ran
    bridge.send("two")  # RuntimeError before: "wrapped C/C++ object of type _Bridge has been deleted"
    assert got == ["one"]
    source = (ROOT / "pythonator" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "bridge.text.emit(" not in source


def test_an_unexpected_error_is_logged_and_the_program_goes_on(monkeypatch: pytest.MonkeyPatch,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    said: list[str] = []
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    errors.keep_running(said.append)
    try:
        raise ZeroDivisionError("boom")
    except ZeroDivisionError:
        sys.excepthook(*sys.exc_info())
    assert said and "ZeroDivisionError" in said[0] and "nothing was closed" in said[0]
    assert "Unexpected error" in caplog.text
    errors._notify.clear()


def test_a_slot_that_raises_doesnt_end_the_window() -> None:
    code = textwrap.dedent("""
        from PyQt6.QtCore import QCoreApplication, QTimer
        from pythonator import errors
        app = QCoreApplication([])
        errors.keep_running()
        QTimer.singleShot(50, lambda: 1 / 0)
        QTimer.singleShot(300, lambda: (print("ALIVE", flush=True), app.quit()))
        app.exec()
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert "ALIVE" in done.stdout, done.stderr[-2000:]


def test_system_check_wraps_long_paths_and_hides_the_home_folder() -> None:
    from pythonator.ui import about

    home = str(Path.home())
    path = home + r"\AppData\Local\Microsoft\WinGet\Packages\DenoLand.Deno_8wekyb3d8bbwe\deno.exe"
    shown = about._private(path)
    assert shown.startswith("~") and home not in shown
    assert about._breakable(r"C:\a\b") == "C:\\\u200ba\\\u200bb"
    assert about._breakable("a / b") == "a / b"  # only inside a path
    if shutil.which("true"):
        assert about._private("nothing to hide") == "nothing to hide"


def test_an_error_on_a_worker_thread_reaches_the_status_bar_on_the_window_thread(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QWidget

    from pythonator.ui.app import _Notice

    seen: list[tuple[str, bool]] = []

    class Window(QWidget):
        def _status(self, text: str, _ms: int) -> None:
            seen.append((text, threading.current_thread() is threading.main_thread()))

    window = Window()
    qtbot.addWidget(window)
    notice = _Notice(window)  # type: ignore[arg-type]
    worker = threading.Thread(target=notice.said.emit, args=("boom",))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: bool(seen), timeout=2000)
    assert seen == [("boom", True)]


def test_a_worker_answer_that_arrives_after_the_window_closed_is_dropped(qtbot: Any) -> None:
    """Removing a project on Windows undoes Make reachable on a worker thread, and the window may close before the
    answer arrives. The answer is dropped instead of reaching a deleted object, which raised in the event loop."""
    import threading

    from PyQt6 import sip
    from PyQt6.QtWidgets import QWidget

    from pythonator.ui.service_dialog import _Bridge

    got: list[str] = []
    for close_first in (False, True):
        window = QWidget()
        bridge = _Bridge(window)

        def step(text: str, bridge: _Bridge = bridge) -> None:
            got.append(text)
            bridge.deleteLater()

        bridge.deliver(step)
        worker = threading.Thread(target=bridge.send, args=(f"done:{close_first}",))
        worker.start()
        worker.join()
        if close_first:
            sip.delete(window)
        qtbot.wait(50)
        if not close_first:
            window.deleteLater()
    assert got == ["done:False"]


def test_editing_the_folder_and_file_to_run_shows_at_once(qtbot: Any, dirs: DataDirs, tmp_path: Path) -> None:
    """The service dialog's new folder, name and file to run reach the file tree and the editor as soon as the
    daemon sends the edited service back, not only after selecting another service and back."""
    from pythonator.ui.main_window import MainWindow

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "app.py").write_text("print('old')\n", encoding="utf-8")
    (new / "bot.py").write_text("print('new')\n", encoding="utf-8")
    (new / "main.py").write_text("print('main')\n", encoding="utf-8")
    window = MainWindow(dirs)
    qtbot.addWidget(window)
    try:
        data = Service.model_validate({"name": "web", "folder": str(old), "runtime": "python",
                                       "entry": "app.py"}).model_dump(mode="json")
        window._set_services([data])
        window._select(data["id"])
        files, editor = window.editor.files, window.editor
        assert files.root == str(old) and editor.current().path == old / "app.py"  # type: ignore[union-attr]
        window._set_services([{**data, "name": "bot", "folder": str(new), "entry": "bot.py"}])
        assert files.root == str(new) and files.title.text() == "BOT"
        assert editor.current().path == new / "bot.py"  # type: ignore[union-attr]
        assert window.console.title.text() == "Console · bot"
        window._set_services([{**data, "name": "bot", "folder": str(new), "entry": "main.py"}])
        assert files.root == str(new) and editor.current().path == new / "main.py"  # type: ignore[union-attr]
        editor.open(new / "bot.py")
        window._set_services([{**data, "name": "bot", "folder": str(new), "entry": "main.py", "args": ["-v"]}])
        assert editor.current().path == new / "bot.py"  # type: ignore[union-attr]  # an unrelated edit keeps the tab
    finally:
        window.client.close()
