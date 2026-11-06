"""0.0.13: Node.js for Windows sandboxes without administrator rights, AI edits that find their file, plugin
panels that can't reach past the dock (the sizing contract, tabs and icon buttons), and service details in a
menu instead of the header."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pythonator.core.models import Service
from pythonator.isolation import private_node, program_folders, windows
from pythonator.paths import DataDirs

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "pythonator-ai"
sys.path.insert(0, str(PLUGIN))

from pythonator_ai import context  # noqa: E402

# -- a private copy of Node for sandboxes ----------------------------------------------------------------------


def fake_node(folder: Path, version: str = "22") -> Path:
    (folder / "node_modules" / "npm" / "bin").mkdir(parents=True)
    (folder / private_node.EXE).write_text(f"node {version}", encoding="utf-8")
    (folder / "npm.cmd").write_text("@echo off", encoding="utf-8")
    (folder / "node_modules" / "npm" / "package.json").write_text('{"version": "10"}', encoding="utf-8")
    (folder / "node_modules" / "npm" / "bin" / "npm-cli.js").write_text("// npm", encoding="utf-8")
    return folder


def test_node_npm_and_npx_are_found_and_run_without_a_batch_file(tmp_path: Path) -> None:
    node = fake_node(tmp_path / "nodejs")
    private = tmp_path / "copy"
    assert private_node.node_folder(str(node / private_node.EXE)) == node
    assert private_node.node_folder(str(node / "npm.cmd")) == node
    assert private_node.node_folder(str(tmp_path / "python.exe")) is None
    assert private_node.node_folder(str(tmp_path / "elsewhere" / "npm.cmd")) is None  # no node beside it
    npm = private_node.swap([str(node / "npm.cmd"), "create", "vite@9"], node, private)
    assert npm == [str(private / private_node.EXE), str(private / "node_modules" / "npm" / "bin" / "npm-cli.js"),
                   "create", "vite@9"]
    npx = private_node.swap([str(node / "npx"), "x"], node, private)
    assert npx[1].endswith("npx-cli.js") and npx[2] == "x"
    assert private_node.swap([str(node / "node.exe"), "app.js"], node, private) == [
        str(private / private_node.EXE), "app.js"]


def test_the_copy_is_made_once_per_version(tmp_path: Path) -> None:
    dirs = DataDirs(tmp_path / "data")
    node = fake_node(tmp_path / "nodejs")
    first = private_node.private_copy(node, dirs)
    assert first.is_relative_to(dirs.root / "runtimes") and (first / "node_modules" / "npm" / "bin").is_dir()
    assert private_node.private_copy(node, dirs) == first  # reused
    (node / private_node.EXE).write_text("node 24, a bigger file", encoding="utf-8")  # Node was updated
    second = private_node.private_copy(node, dirs)
    assert second != first and not first.exists() and (second / private_node.EXE).read_text().startswith("node 24")
    assert not list((dirs.root / "runtimes").glob("*.partial-*"))


def test_off_windows_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = fake_node(tmp_path / "nodejs")
    argv, env = [str(node / "npm.cmd"), "install"], {"PATH": str(node)}
    if sys.platform != "win32":
        assert private_node.for_sandbox(argv, env, DataDirs(tmp_path / "d")) == (argv, env, None)
    assert windows.can_change_permissions(tmp_path) or sys.platform == "win32"


def test_a_blocked_node_is_swapped_for_the_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What Windows does when C:\\Program Files\\nodejs doesn't let sandboxes in (simulated on every OS)."""
    node = fake_node(tmp_path / "nodejs")
    monkeypatch.setattr(private_node, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "blocked_program_folder", lambda folder: folder == node)
    dirs = DataDirs(tmp_path / "d")
    env = {"PATH": f"{node}{__import__('os').pathsep}/usr/bin"}
    argv, new_env, private = private_node.for_sandbox([str(node / "npm.cmd"), "install"], env, dirs)
    assert private is not None and argv[0] == str(private / private_node.EXE) and argv[1].endswith("npm-cli.js")
    assert new_env["PATH"].split(__import__("os").pathsep)[0] == str(private)
    assert str(node) not in new_env["PATH"].split(__import__("os").pathsep)
    # a Node service's shell finds the copy first on PATH, its command line stays as typed
    monkeypatch.setattr("pythonator.core.runtimes.which", lambda name: str(node / private_node.EXE))
    shell, shell_env, again = private_node.for_sandbox(["cmd.exe", "/c", "npm -v"], env, dirs, node_service=True)
    assert shell == ["cmd.exe", "/c", "npm -v"] and again == private and shell_env["PATH"].startswith(str(private))


def test_the_installed_node_is_left_out_when_the_copy_is_used(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    node = fake_node(tmp_path / "nodejs")
    monkeypatch.setattr("pythonator.core.runtimes.which", lambda name: str(node / private_node.EXE))
    service = Service.model_validate({"name": "n", "folder": str(tmp_path), "runtime": "node", "entry": ""})
    dirs = DataDirs(tmp_path / "d")
    shell = tmp_path / "tools" / "cmd.exe"
    shell.parent.mkdir()
    shell.write_text("", encoding="utf-8")
    assert node in program_folders(service, [str(shell)], dirs)
    assert node not in program_folders(service, [str(shell)], dirs, with_node=False)


# -- AI edits find their file ----------------------------------------------------------------------------------

STRESS = 'p = sub.add_parser("brute")\np.add_argument("--lines", type=int, default=1_000_000)\n'


def test_a_placeholder_path_goes_to_the_file_that_holds_the_text() -> None:
    answer = ("<<<<<<< EDIT path/to/file.py\n" + STRESS + "=======\n"
              + STRESS.replace("1_000_000", "50_000") + ">>>>>>> END\n")
    edits, notes = context.resolve_paths(context.parse_edits(answer), ["stress.py", "main.py"],
                                         {"stress.py": "import x\n" + STRESS, "main.py": "print()\n"})
    assert list(edits) == ["stress.py"] and "is for stress.py" in notes[0]


def test_a_wrong_folder_or_a_placeholder_that_matches_nothing() -> None:
    known = ["src/app.py", "README.md"]
    shown = {"src/app.py": "PORT = 80\n"}
    edits, _ = context.resolve_paths(context.parse_edits("<<<<<<< EDIT app.py\nPORT = 80\n=======\nPORT = 1\n"
                                                         ">>>>>>> END\n"), known, shown)
    assert list(edits) == ["src/app.py"]  # the one file of that name
    edits, notes = context.resolve_paths(context.parse_edits("<<<<<<< EDIT path/to/file.py\nnothing like it\n"
                                                             "=======\nx\n>>>>>>> END\n"), known, shown)
    assert edits == {} and "left out" in notes[0]  # never "create path/to/file.py"
    edits, _ = context.resolve_paths(context.parse_edits("<<<<<<< EDIT docs/NEW.md\n=======\n# New\n"
                                                         ">>>>>>> END\n"), known, shown)
    assert list(edits) == ["docs/NEW.md"]  # a new file stays a new file


def test_trailing_spaces_and_line_ends_dont_stop_an_edit() -> None:
    text = "a = 1\nPORT = 80\nb = 2\n"
    assert context.loose_span(text, "PORT = 80   \n") == (6, 15)
    assert context.loose_span("x\nx\n", "x") is None  # twice: not a unique place
    edits, _ = context.resolve_paths(context.parse_edits("<<<<<<< EDIT app.py\nPORT = 80  \n=======\nPORT = 1\n"
                                                         ">>>>>>> END\n"), ["app.py"], {"app.py": text})
    fixed = edits["app.py"][0]
    assert fixed.old == "PORT = 80\n" and text.count(fixed.old) == 1  # what Pythonator replaces, exactly


def test_the_prompt_asks_for_real_paths() -> None:
    assert "path/to/file.py" in context.SYSTEM and "never a placeholder" in context.SYSTEM
    assert "<<<<<<< EDIT main.py" in context.SYSTEM


# -- the sizing contract ---------------------------------------------------------------------------------------


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


# -- the bundled plugins use views and icons -------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from pythonator import sdk

    out: list[dict[str, Any]] = []
    monkeypatch.setattr(sdk, "_send", out.append)
    return out


def test_notify_has_two_views(sent: list[dict[str, Any]], tmp_path: Path) -> None:
    from tests.test_official_plugins import notify_plugin

    plugin = notify_plugin(tmp_path)
    first = json.dumps(plugin.panel([]))
    assert '"type": "tabs"' in first and "Problems 24 h" in first and "Detail" not in first
    plugin.on_action("page", "Events")
    second = json.dumps(plugin.panel([]))
    assert "Nothing has happened yet" in second and "Problems 24 h" not in second
    assert '"icon": "send"' in second and '"icon": "delete"' in second


def test_builder_has_two_views(sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_official_plugins import plugin_module

    builder = plugin_module("builder")
    plugin = builder.Builder(permissions=frozenset({"ui", "create_services"}))
    shown: list[dict[str, Any]] = []
    monkeypatch.setattr(plugin, "project_templates", lambda: {"templates": [], "folder": "/p"})
    monkeypatch.setattr(plugin, "list_projects", list)
    monkeypatch.setattr(plugin, "show", shown.append)
    plugin.on_start()
    plugin.loading = False
    plugin.on_action("page", "Projects")
    assert '"type": "tabs"' in json.dumps(shown[-1]) and "No projects made here yet" in json.dumps(shown[-1])


# -- the window: details in a menu, a lower editor ---------------------------------------------------------------


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
