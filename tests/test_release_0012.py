"""0.0.12: bot and app templates, deleting a project (only after the user says so), Windows program folders the
sandbox can't use, plugin panels that wrap instead of cutting off, and Pythonator's share of the meter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pythonator import launcher
from pythonator.core import runtimes, templates
from pythonator.core.models import Service
from pythonator.isolation import windows

# -- templates -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [k for k, t in templates.TEMPLATES.items() if not t.command])
def test_every_written_template_is_valid_code(key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    plan = templates.plan(key, "demo", tmp_path, set())
    t = templates.TEMPLATES[key]
    Service.model_validate(plan.service)  # a valid service as it is
    for name, text in plan.files.items():
        assert "{port}" not in text, name
        if name.endswith(".py"):
            compile(text, name, "exec")
    ports = plan.service["network"]["ports"]
    assert (ports == []) == (t.port == 0) and plan.service["mode"] == "sandbox"
    assert plan.service["network"]["expose"] == "local" and set(plan.service["env"]) == set(t.env)
    if t.needs in ("npm", "bun"):
        assert plan.steps == [] and '"private": true' in plan.files["package.json"]  # npm install on first Start
    elif t.needs == "python":
        assert plan.steps[0][1:3] == ["-m", "venv"] and "requirements.txt" in plan.files
    else:  # Go, Rust, Deno, Java, PHP, Ruby: their own files, and their own tool fetches what they use
        assert ".gitignore" in plan.files and all(step[0] == f"/bin/{step[0].rsplit('/', 1)[-1]}"
                                                  for step in plan.steps)


@pytest.mark.skipif(not runtimes.which("node"), reason="Node.js isn't installed")
@pytest.mark.parametrize("key", [k for k, t in templates.TEMPLATES.items() if t.needs == "npm" and not t.command])
def test_node_templates_parse(key: str, tmp_path: Path) -> None:
    plan = templates.plan(key, "demo", tmp_path, set())
    for name, text in plan.files.items():
        if name.endswith(".js"):
            (tmp_path / name).write_text(text, encoding="utf-8")
            done = subprocess.run([runtimes.which("node") or "node", "--check", str(tmp_path / name)],
                                  capture_output=True, text=True, check=False)
            assert done.returncode == 0, done.stderr


def test_bots_wait_for_their_token(tmp_path: Path) -> None:
    """Without its token a bot says where to put it and exits 0, so it isn't restarted over and over."""
    plan = templates.plan("worker-py", "w", tmp_path, set())
    assert "SIGTERM" in plan.files["worker.py"]
    for key, var in (("discord-py", "DISCORD_TOKEN"), ("telegram-py", "TELEGRAM_TOKEN")):
        text = templates.plan(key, "b", tmp_path, set()).files["bot.py"]
        assert f"No {var} yet" in text and "sys.exit(0)" in text
    webhook = templates.plan("whatsapp-py", "b", tmp_path, set()).files["app.py"]
    assert "compare_digest" in webhook and "X-Hub-Signature-256" in webhook and 'host="127.0.0.1"' in webhook


# -- deleting a project --------------------------------------------------------------------------------------


def test_only_a_made_project_is_deleted_and_only_when_the_user_confirms(dirs: Any, qtbot: Any,
                                                                      tmp_path: Path) -> None:
    from tests.conftest import DaemonHarness

    h = DaemonHarness(dirs, qtbot)
    try:
        sup, host = h.daemon.sup, h.daemon.plugins
        parent = tmp_path / "projects"
        made = sup.create_project("worker-py", "job", parent)
        folder = parent / "job"
        h.wait(lambda: sup.slots[made.id].state.value in ("stopped", "failed"), timeout=240_000)
        assert sup.slots[made.id].state.value == "stopped", sup.tail(made.id)
        assert sup.slots[made.id].service.made_by is not None
        mine = tmp_path / "mine"
        mine.mkdir()
        own = h.add(name="mine", folder=str(mine), runtime="python", entry="x.py",
                    made_by={"template": "flask", "folder": str(mine)})  # the window can't claim it was made
        assert sup.slots[own].service.made_by is None
        with pytest.raises(ValueError, match="wasn't made by the Project builder"):
            sup.project_folder(own)

        builder = host.plugins["builder"]
        listed = host._answer(builder, {"op": "projects"}, {"create_services"})["projects"]
        assert [p["name"] for p in listed] == ["job"]
        assert host._answer(builder, {"op": "delete", "id": made.id}, {"create_services"}) == {"asked": True}
        h.wait(lambda: any(e.get("ev") == "confirm_delete" and e["id"] == made.id for e in h.events))
        assert folder.is_dir() and made.id in sup.slots  # a plugin's request deletes nothing by itself

        edited = sup.slots[made.id].service.model_copy(update={"made_by": None, "name": "job2"})
        h.ok({"op": "update", "service": edited.model_dump(mode="json")})
        assert sup.slots[made.id].service.made_by is not None  # an edit keeps the mark

        h.ok({"op": "remove", "id": made.id, "files": True})  # the user said yes in the window
        h.wait(lambda: not folder.exists() and made.id not in sup.slots, timeout=20_000)
        assert parent.is_dir() and mine.is_dir()
        ok, _data, err = h.call({"op": "remove", "id": own, "files": True})
        assert not ok and "wasn't made" in err and mine.is_dir()
    finally:
        h.close()


def test_a_moved_project_keeps_its_folder(dirs: Any, qtbot: Any, tmp_path: Path) -> None:
    from tests.conftest import DaemonHarness

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    h = DaemonHarness(dirs, qtbot)
    try:
        service = Service.model_validate({"name": "p", "folder": str(elsewhere), "runtime": "python",
                                          "entry": "x.py", "made_by": {"template": "flask",
                                                                       "folder": str(tmp_path / "p")}})
        h.daemon.sup.add(service)
        with pytest.raises(ValueError, match="moved to another folder"):
            h.daemon.sup.project_folder(service.id)
    finally:
        h.close()


# -- Windows: programs installed for all users -----------------------------------------------------------------


def ace(kind: int, sid: str, mask: int = 0x001200A9, flags: int = 0x10) -> launcher.Ace:
    return launcher.Ace(kind, flags, mask, sid)


def test_a_program_folder_is_shared_with_sandboxes_only_with_read_and_run_for_all() -> None:
    allp = launcher.ALL_APP_PACKAGES
    assert launcher.shared_with_sandboxes([ace(0, allp)])  # as C:\Program Files is
    assert not launcher.shared_with_sandboxes([ace(0, "S-1-5-32-545")])  # Users only: AppContainers can't read
    assert not launcher.shared_with_sandboxes([ace(0, allp, mask=0x00120089)])  # read, but not run
    assert not launcher.shared_with_sandboxes([ace(0, allp, flags=0x08)])  # only for what is made inside later
    assert not launcher.shared_with_sandboxes([ace(0, allp), ace(1, allp, mask=0x20)])  # a deny wins


def test_the_share_script_only_takes_a_real_folder(tmp_path: Path) -> None:
    script = windows.share_script(tmp_path)
    assert f"'{tmp_path.resolve()}'" in script and "(OI)(CI)(RX)" in script and "*S-1-15-2-1" in script
    for bad in (tmp_path / "missing", tmp_path / "it's"):
        if bad.name == "it's":
            bad.mkdir()
        with pytest.raises(ValueError):
            windows.share_script(bad)
    assert not windows.blocked_program_folder(tmp_path)  # not Windows, or a folder of yours: never blocked


@pytest.mark.skipif(sys.platform != "win32", reason="Windows permissions")
def test_the_start_says_how_to_fix_a_program_folder_sandboxes_cant_use(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "windows_shared_with_sandboxes", lambda path: False)
    from pythonator.isolation import IsolationError

    assert windows.can_change_permissions(tmp_path)  # your own folder: the launcher shares it itself
    assert not windows.blocked_program_folder(tmp_path)
    monkeypatch.setattr(windows, "can_change_permissions", lambda folder: False)  # as C:\Program Files\...
    with pytest.raises(IsolationError, match="Let sandboxes use it"):
        windows.check_program_folders([tmp_path])


# -- panels and the meter --------------------------------------------------------------------------------------


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


def test_the_background_service_measures_itself_and_its_plugins(dirs: Any) -> None:
    from pythonator.daemon.supervisor import Stats, _own_stats

    own = _own_stats({}, set(), [Stats(cpu=12.5, memory=1000)])
    assert own["cpu"] >= 12.5 and own["memory"] > 1000  # this process counts too
