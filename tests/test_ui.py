from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pythonator.core.logs import LineKind, LogLine
from pythonator.core.models import ServiceConfig
from pythonator.core.paths import DataPaths
from pythonator.supervisor import Status, Supervisor
from pythonator.ui import theme
from pythonator.ui.console import ConsoleView
from pythonator.ui.main_window import MainWindow

from .conftest import wait_until


def test_console_renders_ansi_and_caps_lines(qapp: object) -> None:
    view = ConsoleView()
    view.set_max_lines(100)
    view.set_show_timestamps(False)
    view.append_lines([LogLine(0, "\x1b[31mred\x1b[0m plain"), LogLine(0, "sys", LineKind.SYSTEM)])
    assert view.toPlainText() == "red plain\nsys"
    view.append_lines([LogLine(0, str(i)) for i in range(500)])
    doc = view.document()
    assert doc is not None and doc.blockCount() <= 100
    assert view.toPlainText().endswith("499")


def test_traceback_location_regex(qapp: object) -> None:
    from pythonator.core.locations import find_location

    assert find_location('  File "/srv/bot/main.py", line 42, in <module>') == ("/srv/bot/main.py", 42)


def test_main_window_runs_a_service(tmp_path: Path, qapp: object) -> None:
    theme.apply_theme(qapp)  # type: ignore[arg-type]
    script = tmp_path / "bot.py"
    script.write_text("print('\\x1b[32mready\\x1b[0m', flush=True)\nimport time\ntime.sleep(30)\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="bot", target=str(script), python=sys.executable))
    window = MainWindow(sup)
    window.show()
    window.select(svc.id)
    assert window.current() is svc
    editor = window.editor.current()
    assert editor is not None and editor.path and Path(editor.path).name == "bot.py"
    window.start_service(svc.id)
    assert wait_until(lambda: "ready" in window.console.view.toPlainText(), qt=True)
    assert svc.status is Status.RUNNING
    window.console.mode.setCurrentIndex(1)
    window._on_console_submit("shell", "echo shell-ok")
    assert wait_until(lambda: "shell-ok" in window.console.view.toPlainText(), qt=True)
    window.quit()
    assert wait_until(lambda: not window.isVisible(), qt=True)
    assert wait_until(lambda: svc.status is Status.STOPPED)


def test_dependencies_button_creates_and_edits_requirements(tmp_path: Path, qapp: object) -> None:
    script = tmp_path / "bot.py"
    script.write_text("print('hi')\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    sup.add(ServiceConfig(name="bot", target=str(script), python=sys.executable))
    window = MainWindow(sup)
    try:
        assert window.empty_hint.isHidden()
        assert window.btn_deps.isEnabled()

        def labels() -> list[str]:
            window.deps_menu.aboutToShow.emit()
            return [a.text() for a in window.deps_menu.actions()]

        assert labels()[:3] == ["Create venv", "Install dependencies", "Create requirements.txt"]
        create = window.deps_menu.actions()[2]
        create.trigger()
        assert (tmp_path / "requirements.txt").is_file()
        assert "Edit requirements.txt" in labels()
    finally:
        window.quit()


def test_dependencies_menu_for_node_creates_edits_and_saves_before_install(
    tmp_path: Path, qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pythonator.core.models import Runtime
    from pythonator.ui import main_window

    script = tmp_path / "bot.js"
    script.write_text("console.log('hi')\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="bot", runtime=Runtime.NODE, target=str(script), workdir=str(tmp_path)))
    installs: list[str] = []

    def fake_install(service_id: str) -> bool:
        installs.append(service_id)
        return True

    sup.install_requirements = fake_install  # type: ignore[method-assign]
    answers = [(False, False), (True, True)]  # first "Cancel", then "Install" + "don't ask again"
    asked: list[str] = []

    def fake_confirm(_parent: object, service: str, what: str, _folder: str) -> tuple[bool, bool]:
        asked.append(what)
        return answers.pop(0)

    monkeypatch.setattr(main_window, "confirm_install", fake_confirm)
    window = MainWindow(sup)
    try:
        def actions() -> dict[str, object]:
            window.deps_menu.aboutToShow.emit()
            return {a.text(): a for a in window.deps_menu.actions() if a.text()}

        menu = actions()
        assert "Create venv" not in menu
        menu["Create package.json"].trigger()  # type: ignore[attr-defined]
        manifest = tmp_path / "package.json"
        assert '"start": "node bot.js"' in manifest.read_text()
        tab = window.editor.find_tab(str(manifest))
        assert tab is not None
        cursor = tab.textCursor()
        cursor.select(cursor.SelectionType.Document)
        cursor.insertText(tab.toPlainText().replace('"dependencies": {}', '"dependencies": {"ms": "^2"}'))
        assert tab.is_modified()
        menu = actions()
        assert "Edit package.json" in menu and "Install dependencies" not in menu
        menu["Save and install dependencies"].trigger()  # type: ignore[attr-defined]
        assert asked == ["from package.json"] and installs == []  # declined: nothing runs
        actions()["Save and install dependencies"].trigger()  # type: ignore[attr-defined]
        assert installs == [svc.id] and svc.config.install_confirmed
        assert '"ms": "^2"' in manifest.read_text()  # saved before installing
        actions()["Install dependencies"].trigger()  # type: ignore[attr-defined]
        assert len(asked) == 2 and installs == [svc.id, svc.id]  # not asked again
    finally:
        window.quit()


def test_dependencies_menu_without_packages(tmp_path: Path, qapp: object) -> None:
    from pythonator.core.models import Runtime

    script = tmp_path / "run.sh"
    script.write_text("echo hi\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    sup.add(ServiceConfig(name="sh", runtime=Runtime.SHELL, target=str(script), workdir=str(tmp_path)))
    window = MainWindow(sup)
    try:
        window.deps_menu.aboutToShow.emit()
        menu = {a.text(): a for a in window.deps_menu.actions() if a.text()}
        assert not menu["Install dependencies"].isEnabled()
        assert not any(name.startswith("Create") for name in menu)
    finally:
        window.quit()


def test_stop_button_is_enabled_while_only_a_shell_command_runs(tmp_path: Path, qapp: object) -> None:
    script = tmp_path / "bot.py"
    script.write_text("print('hi')\n")
    long_running = tmp_path / "long.py"
    long_running.write_text("import time\nprint('long', flush=True)\ntime.sleep(60)\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="bot", target=str(script), python=sys.executable))
    window = MainWindow(sup)
    try:
        window._update_header()
        assert not window.btn_stop.isEnabled()
        quote = '"' if sys.platform == "win32" else "'"
        assert sup.run_shell(svc.id, f"{quote}{sys.executable}{quote} -u {quote}{long_running}{quote}")
        assert wait_until(lambda: "long" in "\n".join(ln.text for ln in svc.log.snapshot()), qt=True)
        window._update_header()
        assert window.btn_stop.isEnabled()
        window.btn_stop.click()
        assert wait_until(lambda: svc.task is None, timeout=15, qt=True)
        window._update_header()
        assert not window.btn_stop.isEnabled()
    finally:
        window.quit()


def test_python_tab_hints_once_how_to_enable_jedi(tmp_path: Path, qapp: object, monkeypatch: object) -> None:
    from pythonator.ui import editor_panel

    monkeypatch.setattr(editor_panel, "HAS_JEDI", False)  # type: ignore[attr-defined]
    panel = editor_panel.EditorPanel()
    messages: list[str] = []
    panel.status_message.connect(messages.append)
    for name in ("a.py", "b.py", "c.js"):
        (tmp_path / name).write_text("x = 1\n")
        panel.open_file(str(tmp_path / name))
    hints = [m for m in messages if "jedi" in m]
    assert len(hints) == 1 and "pip install jedi" in hints[0]
