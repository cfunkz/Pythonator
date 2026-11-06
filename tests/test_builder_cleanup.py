"""Removing projects smoothly: one question for the right-click Remove and the Project builder's Delete (both can
delete a made project's folder), a second Remove or Delete does nothing, the builder doesn't flood Pythonator with
requests while services change state, leftover project folders found on Refresh, and one Pythonator at a time."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.supervisor import Supervisor
from pythonator.instance import InstanceLock
from pythonator.paths import DataDirs

# -- leftovers: project folders no service uses ------------------------------------------------------------------


def _drain(sup: Supervisor, until: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        sup.tick()
        time.sleep(0.02)
    assert until()


def test_leftovers_are_found_and_only_ever_deleted_while_still_leftovers(
        dirs: DataDirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.core import templates

    sup = Supervisor(dirs, Store(dirs))
    parent = tmp_path / "projects"
    for name in ("made", "mine", "used", ".hidden"):
        (parent / name).mkdir(parents=True)
    (parent / "made" / "node_modules" / "x").mkdir(parents=True)
    (parent / "notes.txt").write_text("x", encoding="utf-8")
    sup._remember_made(parent / "made")
    sup.add(Service.model_validate({"name": "used", "folder": str(parent / "used"), "runtime": "python",
                                    "entry": "app.py"}))

    # A folder of the user's own choosing: only what the Project builder made there.
    assert [x["name"] for x in sup.leftover_projects(parent)] == ["made"]
    # Pythonator's own projects folder: anything no service uses, never hidden ones or files.
    monkeypatch.setattr(templates, "default_parent", lambda: parent)
    found = sup.leftover_projects(parent)
    assert [(x["name"], x["made"]) for x in found] == [("made", True), ("mine", False)]
    assert sup.leftover_projects(tmp_path / "nowhere") == []

    with pytest.raises(ValueError, match="isn't a leftover"):
        sup.delete_leftovers([str(parent / "used")])  # a service uses it
    with pytest.raises(ValueError, match="isn't a leftover"):
        sup.delete_leftovers([str(parent / "made"), str(tmp_path)])  # one wrong one: nothing at all is deleted
    assert (parent / "made").is_dir()

    events: list[dict[str, Any]] = []
    sup.listeners.append(events.append)
    sup.delete_leftovers([str(parent / "made")])
    assert [x["name"] for x in sup.leftover_projects(parent)] == ["mine"]  # being deleted: not offered again
    _drain(sup, lambda: any(e.get("level") == "ok" for e in events))
    assert not (parent / "made").exists() and (parent / "used").is_dir() and (parent / "mine").is_dir()
    assert sup._made() == set()  # forgotten once gone


def test_a_project_removed_with_its_folder_kept_becomes_a_leftover(dirs: DataDirs, tmp_path: Path) -> None:
    sup = Supervisor(dirs, Store(dirs))
    folder = tmp_path / "projects" / "shop"
    folder.mkdir(parents=True)
    service = Service.model_validate({"name": "shop", "folder": str(folder), "runtime": "python", "entry": "a.py",
                                      "made_by": {"template": "flask", "folder": str(folder)}})
    sup.add(service)
    assert sup.leftover_projects(folder.parent) == []
    sup.remove(service.id)
    assert [x["name"] for x in sup.leftover_projects(folder.parent)] == ["shop"]


def test_removing_twice_does_nothing_the_second_time(harness: Any, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    sid = harness.add(name="sleeper", folder=str(tmp_path), runtime="python", entry="app.py", mode="full",
                      full_access_ok=True)
    harness.ok({"op": "start", "id": sid, "confirmed": True})
    harness.wait(lambda: "running" in harness.states(sid))
    harness.ok({"op": "remove", "id": sid})
    harness.ok({"op": "remove", "id": sid})  # a second click (a second window) while it stops: no error, no kill
    harness.wait(lambda: sid not in harness.daemon.sup.slots)
    assert "Killed" not in "".join(e.get("text", "") for e in harness.events if e.get("ev") == "output")


def test_the_plugin_asks_about_leftovers_in_the_window(harness: Any, tmp_path: Path) -> None:
    from pythonator.plugins import files

    sup, host = harness.daemon.sup, harness.daemon.plugins
    parent = tmp_path / "projects"
    (parent / "old").mkdir(parents=True)
    sup._remember_made(parent / "old")
    builder = host.plugins["builder"]
    granted = {"create_services"}
    found = host._answer(builder, {"op": "leftovers", "folder": str(parent)}, granted)
    assert [x["name"] for x in found["leftovers"]] == ["old"] and found["folder"] == str(parent.resolve())
    with pytest.raises(files.FileRefused, match="leftover"):
        host._answer(builder, {"op": "delete_leftovers", "folder": str(parent), "folders": [str(tmp_path)]},
                     granted)
    asked = host._answer(builder, {"op": "delete_leftovers", "folder": str(parent),
                                   "folders": [found["leftovers"][0]["folder"]]}, granted)
    assert asked == {"asked": 1}
    harness.wait(lambda: any(e.get("ev") == "confirm_leftovers" for e in harness.events))
    assert (parent / "old").is_dir()  # a plugin's request deletes nothing by itself
    harness.ok({"op": "delete_leftovers", "folders": [found["leftovers"][0]["folder"]]})  # the user said yes
    harness.wait(lambda: not (parent / "old").exists())


# -- the Project builder: few requests, the same panel not sent twice --------------------------------------------


def _builder(monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any] | None = None) -> tuple[Any, dict[str, int]]:
    from pythonator import sdk
    from tests.test_official_plugins import plugin_module

    monkeypatch.setattr(sdk, "_send", lambda _m: None)
    builder = plugin_module("builder")
    plugin = builder.Builder(permissions=frozenset({"ui", "create_services"}), settings=dict(settings or {}))
    calls = {"templates": 0, "projects": 0, "leftovers": 0, "shown": 0}
    gate = threading.Event()
    gate.set()

    def templates() -> dict[str, Any]:
        calls["templates"] += 1
        return {"templates": [], "folder": "/p"}

    def projects() -> list[dict[str, Any]]:
        gate.wait(5)
        calls["projects"] += 1
        return [{"id": "abc", "name": "shop", "template": "flask", "folder": "/p/shop", "state": "running"}]

    def leftovers(folder: str = "") -> dict[str, Any]:
        calls["leftovers"] += 1
        return {"folder": "/p", "leftovers": [{"folder": "/p/old", "name": "old", "made": True}]}

    def show(_panel: dict[str, Any]) -> None:
        calls["shown"] += 1

    monkeypatch.setattr(plugin, "project_templates", templates)
    monkeypatch.setattr(plugin, "list_projects", projects)
    monkeypatch.setattr(plugin, "leftover_projects", leftovers)
    monkeypatch.setattr(plugin, "show", show)
    plugin.gate = gate
    return plugin, calls


def _settle(plugin: Any) -> None:
    deadline = time.monotonic() + 5
    while (plugin.loading or plugin.busy) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not plugin.loading


def test_state_events_cost_the_builder_no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator import sdk

    plugin, calls = _builder(monkeypatch)
    plugin.on_start()
    _settle(plugin)
    plugin.services = {"abc": sdk.ServiceInfo("abc", "shop", "python", "sandbox", "running")}
    assert calls["templates"] == 1 and calls["projects"] == 1 and calls["leftovers"] == 0  # the setting is off
    for state in ("stopping", "stopped", "starting", "running", "running", "running"):
        plugin.on_event(sdk.ServiceEvent("started", sdk.ServiceInfo("abc", "shop", "python", "sandbox", state)))
    assert plugin.projects[0]["state"] == "running"
    assert calls["templates"] == 1 and calls["projects"] == 1  # taken from the events themselves
    shown = calls["shown"]
    plugin.render()
    plugin.render()
    assert calls["shown"] == shown  # nothing changed: not sent again
    plugin.on_tick()
    assert calls["projects"] == 1  # no project went away
    plugin.services = {}
    plugin.on_tick()
    _settle(plugin)
    assert calls["projects"] == 2 and calls["templates"] == 1  # a removed project: the list, not the templates


def test_loads_asked_for_while_one_runs_are_one_more_load(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, calls = _builder(monkeypatch)
    plugin.on_start()
    _settle(plugin)
    plugin.gate.clear()
    plugin.load("projects")
    for _ in range(10):
        plugin.load("projects")
    plugin.gate.set()
    _settle(plugin)
    assert calls["projects"] == 3  # the first, the one held up, and one for the ten asked meanwhile


def test_refresh_lists_leftovers_only_when_the_setting_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, calls = _builder(monkeypatch, {"leftovers": True})
    asked: list[Any] = []

    def delete_leftovers(folders: list[str], folder: str = "") -> int:
        asked.append(folders)
        return len(folders)

    monkeypatch.setattr(plugin, "delete_leftovers", delete_leftovers)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(plugin, "show", sent.append)
    plugin.on_start()
    _settle(plugin)
    assert calls["leftovers"] == 1
    assert "1 leftover project folder found" in json.dumps(sent[-1])
    plugin.on_action("page", "Projects")
    text = json.dumps(sent[-1])
    assert "no service uses any more" in text and '"cleanup"' in text and "old" in text
    plugin.on_action("cleanup", "")
    assert asked == [["/p/old"]] and "which of the 1" in plugin.status[0]
    plugin.settings = {"leftovers": False}
    plugin.on_settings()
    assert '"cleanup"' not in json.dumps(sent[-1]) and not plugin.leftovers
    plugin.on_action("refresh", "")
    _settle(plugin)
    assert calls["leftovers"] == 1


# -- the window: one question for both Removes -------------------------------------------------------------------


def _window(qtbot: Any, dirs: DataDirs) -> tuple[Any, list[dict[str, Any]]]:
    """The window, and what it would send to the background service."""
    from pythonator.ui.main_window import MainWindow

    window = MainWindow(dirs)
    qtbot.addWidget(window)
    sent: list[dict[str, Any]] = []
    window.client.request = lambda msg, reply=None: sent.append(msg)  # type: ignore[assignment,method-assign]
    return window, sent


def _box(window: Any) -> Any:
    return window.remove_box


def test_right_click_remove_can_delete_a_made_projects_folder(qtbot: Any, dirs: DataDirs, tmp_path: Path) -> None:
    from PyQt6.QtWidgets import QMessageBox

    made, own = tmp_path / "shop", tmp_path / "own"
    made.mkdir()
    own.mkdir()
    shop = Service.model_validate({"name": "shop", "folder": str(made), "runtime": "python", "entry": "a.py",
                                   "made_by": {"template": "flask", "folder": str(made)}})
    mine = Service.model_validate({"name": "mine", "folder": str(own), "runtime": "python", "entry": "a.py"})
    window, sent = _window(qtbot, dirs)
    try:
        assert window.deletable(shop) and not window.deletable(mine)
        window._ask_remove([shop, mine])
        box = window.remove_box
        assert isinstance(box, QMessageBox)
        assert {"Delete project and folder", "Remove, keep folder"} <= {b.text() for b in box.buttons()}
        assert str(made) in box.informativeText() and str(own) not in box.informativeText()
        window._ask_remove([shop])  # asked again (a second click): the same question, no second one
        window._confirm_delete({"id": shop.id, "name": "shop", "folder": str(made), "plugin": "Project builder"})
        assert window.remove_box is box
        delete = next(b for b in box.buttons() if b.text() == "Delete project and folder")
        delete.click()
        assert sent == [{"op": "remove", "id": shop.id, "files": True},
                               {"op": "remove", "id": mine.id, "files": False}]
        window._ask_remove([shop])  # on its way out: never asked about again
        assert _box(window) is None and len(sent) == 2

        window.removing.clear()
        window._ask_remove([mine])
        box = _box(window)
        assert box is not None and "Delete project and folder" not in [b.text() for b in box.buttons()]
        cancel = box.button(QMessageBox.StandardButton.Cancel)
        assert cancel is not None
        cancel.click()
        assert len(sent) == 2 and mine.id not in window.removing  # cancelled: may be asked again
    finally:
        window.client.close()


def test_the_leftovers_window_deletes_only_the_ticked_folders(qtbot: Any, dirs: DataDirs) -> None:
    from PyQt6.QtCore import Qt

    window, sent = _window(qtbot, dirs)
    try:
        window._confirm_leftovers({"folders": ["/p/a", "/p/b"], "plugin": "Project builder"})
        box = window.leftovers_box
        assert box is not None
        window._confirm_leftovers({"folders": ["/p/a"], "plugin": "Project builder"})
        assert window.leftovers_box is box  # one window
        item = box.list.item(1)
        assert item is not None
        item.setCheckState(Qt.CheckState.Unchecked)
        assert box.delete_button.text() == "Delete 1 folder"
        box.delete_button.click()
        assert sent == [{"op": "delete_leftovers", "folders": ["/p/a"]}]
    finally:
        window.client.close()


# -- one Pythonator at a time --------------------------------------------------------------------------------------


def test_only_one_process_holds_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "window.lock"
    lock = InstanceLock(path)
    assert lock.acquire() and lock.acquire()  # held: asking again is fine
    other = [sys.executable, "-c", "import sys; from pathlib import Path; from pythonator.instance import "
             "InstanceLock; sys.exit(0 if InstanceLock(Path(sys.argv[1])).acquire() else 3)", str(path)]
    assert subprocess.run(other, check=False, timeout=60).returncode == 3  # a second Pythonator stops
    lock.release()
    assert subprocess.run(other, check=False, timeout=60).returncode == 0  # once the first is gone, it starts
