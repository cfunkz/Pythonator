"""0.0.11: Docker Compose projects (and their safety check), the Project builder's templates, the editor's line
endings and undo, the env table's buttons, the system check, and plugin panels in docks and windows."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pythonator.core import runtimes, templates
from pythonator.core.models import Service
from pythonator.isolation import IsolationError, compose

HAS_COMPOSE = bool(shutil.which("docker")) and subprocess.run(
    ["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


def compose_service(folder: Path, **kw: Any) -> Service:
    return Service.model_validate({"id": "abc123", "name": "stack", "folder": str(folder), "runtime": "compose",
                                   "entry": "compose.yaml", "mode": "container", **kw})


# -- Docker Compose ------------------------------------------------------------------------------------------


def test_compose_runs_in_container_mode_only(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    assert runtimes.detect(tmp_path)[0] is runtimes.RUNTIMES["compose"]
    assert runtimes.detect(tmp_path)[1] == "compose.yaml"
    with pytest.raises(ValueError, match="Container mode"):
        Service.model_validate({"name": "s", "folder": str(tmp_path), "runtime": "compose", "mode": "sandbox"})
    assert compose_service(tmp_path).mode.value == "container"


def test_compose_audit_refuses_what_a_container_may_not_have(tmp_path: Path) -> None:
    folder = tmp_path.resolve()
    config = {"services": {
        "ok": {"image": "nginx", "ports": [{"target": 80, "published": "8080", "host_ip": "127.0.0.1"}],
               "volumes": [{"type": "bind", "source": str(folder / "data"), "target": "/data"},
                           {"type": "volume", "source": "db", "target": "/db"}],
               "build": {"context": str(folder)}, "cap_add": ["NET_BIND_SERVICE"], "network_mode": "service:x"},
        "bad": {"privileged": True, "network_mode": "host", "pid": "container:other", "cap_add": ["SYS_ADMIN"],
                "devices": ["/dev/sda"], "security_opt": ["seccomp=unconfined"],
                "volumes": [{"type": "bind", "source": "/etc", "target": "/x"},
                            {"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}],
                "build": {"context": str(folder.parent)}, "env_file": [{"path": str(folder.parent / "x.env")}],
                "ports": [{"target": 80, "published": "8000"}]},
    }, "secrets": {"pw": {"file": str(folder.parent / "pw.txt")}}}
    problems = compose.audit(config, folder, lan=False)
    assert not [p for p in problems if p.startswith("ok:")], problems
    joined = "\n".join(problems)
    for expected in ("privileged", "network_mode: host", "pid: container:other", "SYS_ADMIN", "devices",
                     "seccomp=unconfined", "mounts /etc", "container engine", "builds from", "env file",
                     "publishes port 8000 to every network", "secrets pw"):
        assert expected in joined, expected
    assert not [p for p in compose.audit(config, folder, lan=True) if "publishes" in p]  # chosen: other computers
    assert compose.published(config) == [8080, 8000]


def test_compose_shell_and_unknown_steps_are_refused(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    service = compose_service(tmp_path)
    with pytest.raises(IsolationError, match="shell"):
        compose.wrap(service, "/usr/bin/docker", task=["/bin/sh", "-c", "id"])
    with pytest.raises(IsolationError, match="shell"):
        compose.wrap(service, "/usr/bin/docker", task=[*compose.base("/usr/bin/docker", service), "exec", "x"])
    down = compose.steps("/usr/bin/docker", service, "down")[0]
    cmd = compose.wrap(service, "/usr/bin/docker", task=down)
    assert cmd.argv[-2:] == ["down", "--remove-orphans"] and "-p" in cmd.argv and "pythonator-abc123" in cmd.argv
    with pytest.raises(IsolationError, match="network on"):
        compose.check("/usr/bin/docker", compose_service(tmp_path, network={"enabled": False}))


def test_compose_file_must_be_in_the_folder(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    service = compose_service(tmp_path, entry="../compose.yaml")
    with pytest.raises(IsolationError, match="isn't a file in the service's folder"):
        compose.compose_file(service)


@pytest.mark.skipif(not HAS_COMPOSE, reason="docker compose isn't installed")
def test_compose_check_reads_the_file_as_the_engine_does(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n  web:\n    image: nginx\n    ports: ['127.0.0.1:${WEB_PORT:-8080}:80']\n"
        "  db:\n    image: postgres\n    volumes: ['/:/host']\n")
    service = compose_service(tmp_path, env={"WEB_PORT": "9123"})
    config = compose.read_config(shutil.which("docker") or "docker", service)
    assert compose.published(config) == [9123]  # the service's variables fill the file's ${…}
    with pytest.raises(IsolationError, match="mounts /, outside the project folder"):
        compose.check(shutil.which("docker") or "docker", service)


# -- the Project builder's templates ---------------------------------------------------------------------------


def test_templates_plan_new_folders_with_official_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    plan = templates.plan("react", "shop", tmp_path, set())
    assert plan.steps == [["/bin/npm", "create", "vite@9", "shop", "--", "--template", "react", "--no-interactive"]]
    assert plan.flatten == "shop" and plan.service["mode"] == "sandbox"
    assert plan.service["network"] == {"enabled": True, "ports": [plan.service["network"]["ports"][0]],
                                       "expose": "local"}
    django = templates.plan("django", "my-site", tmp_path, set())
    assert django.steps[-1][-4:] == ["django", "startproject", "my_site", "."]
    assert "Django" in django.files["requirements.txt"]
    flask = templates.plan("flask", "api", tmp_path, {5050})
    assert flask.service["network"]["ports"] != [5050]  # another service has that port
    compile(flask.files["app.py"], "app.py", "exec")
    compile(templates.plan("fastapi", "x", tmp_path, set()).files["main.py"], "main.py", "exec")
    for bad in ("Shop", "1x", "a/b", "../x", "x" * 41, ""):
        with pytest.raises(ValueError, match="lowercase"):
            templates.plan("react", bad, tmp_path, set())
    (tmp_path / "taken").mkdir()
    with pytest.raises(ValueError, match="exists already"):
        templates.plan("flask", "taken", tmp_path, set())
    with pytest.raises(ValueError, match="Unknown"):
        templates.plan("rails", "x", tmp_path, set())


def test_flatten_moves_the_tool_output_up(tmp_path: Path) -> None:
    (tmp_path / "shop" / "src").mkdir(parents=True)
    (tmp_path / "shop" / "package.json").write_text("{}")
    templates.flatten(tmp_path, "shop")
    assert (tmp_path / "package.json").is_file() and (tmp_path / "src").is_dir() and not (tmp_path / "shop").exists()


def test_builder_makes_a_flask_service_in_its_sandbox(dirs: Any, qtbot: Any, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The real thing: a new folder, the venv and pip run in the new service's sandbox (no packages, so it works
    offline)."""
    import dataclasses

    from tests.conftest import DaemonHarness

    offline = dataclasses.replace(templates.TEMPLATES["flask"], requirements="")
    monkeypatch.setitem(templates.TEMPLATES, "flask", offline)
    h = DaemonHarness(dirs, qtbot)
    try:
        sup = h.daemon.sup
        parent = tmp_path / "projects"
        service = sup.create_project("flask", "hello", parent)
        assert (parent / "hello" / "app.py").is_file() and service.mode.value == "sandbox"
        with pytest.raises(ValueError, match="exists already"):
            sup.create_project("flask", "hello", parent)
        with pytest.raises(ValueError, match="can't go in"):
            sup.create_project("flask", "x", dirs.root)
        h.wait(lambda: sup.slots[service.id].state.value in ("stopped", "failed"), timeout=240_000)
        log = sup.tail(service.id)
        assert sup.slots[service.id].state.value == "stopped", log
        assert "Project ready" in log and runtimes.project_venv(parent / "hello") is not None
    finally:
        h.close()


# -- the editor ------------------------------------------------------------------------------------------------


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


# -- the service dialog's tables -------------------------------------------------------------------------------


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


# -- the system check ------------------------------------------------------------------------------------------


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


# -- plugin panels ---------------------------------------------------------------------------------------------


def test_markdown_shows_no_html_and_loads_nothing(qtbot: Any) -> None:
    from PyQt6.QtCore import QUrl

    from pythonator.ui.panels import Prose

    prose = Prose("# Fix\n\n**bold** <b>raw</b> ![x](file:///etc/passwd)", markdown=True)
    qtbot.addWidget(prose)
    text = prose.toPlainText()
    assert "Fix" in text and "bold" in text and "**" not in text and "<b>raw</b>" in text  # HTML stays text
    assert prose.loadResource(2, QUrl("file:///etc/passwd")) is None
    plain = Prose("**not markdown**")
    qtbot.addWidget(plain)
    assert plain.toPlainText() == "**not markdown**"


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


def test_each_plugin_panel_is_a_dock_that_can_be_a_window(qtbot: Any) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QDockWidget, QMainWindow

    from pythonator.sdk import ui
    from pythonator.ui.panels import PanelDocks

    window = QMainWindow()
    qtbot.addWidget(window)
    window.show()
    docks = PanelDocks(window, lambda *a: None)
    docks.update_panel("notify", "Notify", ui.panel("Notify", ui.text("ok")))
    docks.update_panel("ai", "AI", ui.panel("AI assistant", ui.text("hi")))
    assert docks.count() == 2 and {d.windowTitle() for d in docks.docks.values()} == {"Notify", "AI assistant"}
    docks.show_all()
    ai = docks.docks["ai"]
    docks.toggle_window(ai)  # ⧉: its own window
    qtbot.waitUntil(lambda: bool(ai.property("own_window")), timeout=3000)
    assert ai.isFloating() and ai.windowFlags() & Qt.WindowType.WindowMinMaxButtonsHint
    docks.toggle_window(ai)  # ⇲: back
    assert not ai.isFloating()
    docks.set_locked(True)
    assert not ai.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable
    docks.update_panel("ai", "", None)
    assert docks.count() == 1
