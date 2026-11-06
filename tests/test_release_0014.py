"""0.0.14: Node.js in the Windows sandbox gets past "EPERM: lstat 'C:\\'", removing and deleting a project never
holds up the window or the other services (and the folder really goes), and the Project builder has one Create."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation, launcher
from pythonator.core.models import Service
from pythonator.isolation import base, windows
from pythonator.paths import DataDirs

# -- Node.js in the Windows sandbox ----------------------------------------------------------------------------

# What an AppContainer does to Node.js, on any OS: every folder above the sandbox's own refuses a stat with EPERM,
# and Node's JavaScript realpath (which stats each of them, from C:\ down) fails. So does Windows' own realpath
# (fs.realpath.native and fs.promises.realpath: it names the drive), as CI showed with Vite in 0.0.15.
APPCONTAINER = r"""
const fs = require('fs'); const path = require('path');
const roots = process.env.PYTHONATOR_NODE_ROOTS.split(path.delimiter).map((r) => path.resolve(r));
const above = (p) => { try { const s = path.resolve(String(p)); const pre = s.endsWith(path.sep) ? s : s + path.sep;
  return roots.some((r) => r !== s && r.startsWith(pre)); } catch { return false; } };
const eperm = (p) => Object.assign(new Error(`EPERM: operation not permitted, lstat '${p}'`),
  { code: 'EPERM', syscall: 'lstat', path: String(p) });
for (const n of ['lstatSync', 'statSync']) { const o = fs[n];
  fs[n] = function (p, ...a) { if (above(p)) throw eperm(p); return o.call(this, p, ...a); }; }
for (const n of ['lstat', 'stat']) { const o = fs[n];
  fs[n] = function (p, opt, cb) { if (typeof opt === 'function') { cb = opt; opt = undefined; }
    if (above(p)) return process.nextTick(cb, eperm(p)); return o.call(this, p, opt, cb); };
  const po = fs.promises[n];
  fs.promises[n] = function (p, ...a) { return above(p) ? Promise.reject(eperm(p)) : po.call(this, p, ...a); }; }
const refused = (p) => Object.assign(new Error(`EPERM: operation not permitted, realpath '${p}'`),
  { code: 'EPERM', syscall: 'realpath', path: String(p) });
fs.realpathSync = function (p) { throw eperm(path.parse(path.resolve(String(p))).root); };
fs.realpathSync.native = function (p) { throw refused(p); };
fs.realpath = function (p, o, cb) { if (typeof o === 'function') cb = o; process.nextTick(cb, eperm('/')); };
fs.realpath.native = function (p, o, cb) { if (typeof o === 'function') cb = o; process.nextTick(cb, refused(p)); };
fs.promises.realpath = (p) => Promise.reject(refused(p));
"""

MAIN = """
const fs = require('fs');
const dep = require('dep');
const up = fs.lstatSync(require('path').parse(__dirname).root);  // npm's own walk down from the root
fs.promises.stat(__dirname + '/..').then((st) => console.log(dep(), up.isDirectory(), st.isDirectory()));
"""


def test_node_options_put_the_shim_first_and_keep_the_users_own(tmp_path: Path) -> None:
    shim = tmp_path / "My Profile" / "pythonator-site" / windows.NODE_SHIM
    env = windows.node_env({"NODE_OPTIONS": "--max-old-space-size=512"}, shim, [tmp_path / "a", tmp_path / "a"])
    assert env["NODE_OPTIONS"] == (f'--preserve-symlinks --preserve-symlinks-main --require "{shim.as_posix()}" '
                                   "--max-old-space-size=512")
    assert "\\" not in env["NODE_OPTIONS"]  # a backslash in NODE_OPTIONS' quotes escapes the next character
    assert env["PYTHONATOR_NODE_ROOTS"] == str(tmp_path / "a")
    again = windows.node_env({"NODE_OPTIONS": env["NODE_OPTIONS"]}, shim, [tmp_path])
    assert again["NODE_OPTIONS"] == env["NODE_OPTIONS"]  # never twice
    assert b"PYTHONATOR_NODE_ROOTS" in windows.node_shim() and b"realpathSync.native" in windows.node_shim()


def test_every_sandboxed_program_gets_the_shim(tmp_path: Path, dirs: DataDirs,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "Packages" / "pythonator.svc-x"
    monkeypatch.setattr(windows, "available", lambda: True)
    monkeypatch.setattr(launcher, "windows_profile", lambda _name: ("S-1-15-2-1", str(profile)))
    project = tmp_path / "project"
    project.mkdir()
    policy = base.Policy(grants=[base.Grant(project, True)])
    cmd = windows.wrap(["node", "main.js"], project, {"NODE_OPTIONS": "--trace-warnings"}, policy, "x", dirs)
    shim = profile / "pythonator-site" / windows.NODE_SHIM
    assert shim.read_bytes() == windows.node_shim()
    assert cmd.env["NODE_OPTIONS"].endswith(f'--require "{shim.as_posix()}" --trace-warnings')
    roots = cmd.env["PYTHONATOR_NODE_ROOTS"].split(os.pathsep)
    assert str(project) in roots and str(profile) in roots and cmd.env["PYTHONATOR_SANDBOX"] == "1"


@pytest.fixture
def node_app(tmp_path: Path) -> tuple[str, Path, Path]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js isn't installed")
    project = tmp_path / "deep" / "down" / "app"
    dep = project / "node_modules" / "dep"
    dep.mkdir(parents=True)
    (dep / "package.json").write_text('{"name": "dep", "main": "index.js", "bin": {"dep-bin": "cli.js"}}',
                                      encoding="utf-8")
    (dep / "index.js").write_text("module.exports = () => 'dep ok';", encoding="utf-8")
    (dep / "cli.js").write_text("#!/usr/bin/env node\nconsole.log('bin ran');", encoding="utf-8")
    (project / "main.js").write_text(MAIN, encoding="utf-8")
    (project / "esm.mjs").write_text("import dep from 'dep'; console.log('esm', dep());", encoding="utf-8")
    (project / "package.json").write_text(json.dumps({"name": "app", "version": "1.0.0", "private": True,
                                                      "scripts": {"hi": "node main.js"}}), encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    (site / "appcontainer.cjs").write_text(APPCONTAINER, encoding="utf-8")
    (site / windows.NODE_SHIM).write_bytes(windows.node_shim())
    return node, project, site


def run_node(node: str, project: Path, site: Path, args: list[str], *, shim: bool) -> subprocess.CompletedProcess[str]:
    install = Path(node).resolve().parent.parent
    roots = [project, site, install]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NODE_", "PYTHONATOR_", "npm_"))}
    env.update({"PYTHONATOR_SANDBOX": "1", "npm_config_cache": str(project / ".npm"),
                "npm_config_update_notifier": "false"})
    # The AppContainer's rules come first, as Windows applies them before anything runs.
    first = f'--preserve-symlinks --preserve-symlinks-main --require "{(site / "appcontainer.cjs").as_posix()}"'
    if shim:
        env.update(windows.node_env({}, site / windows.NODE_SHIM, roots))
        env["NODE_OPTIONS"] = f"{first} {env['NODE_OPTIONS']}"
    else:
        env.update({"NODE_OPTIONS": f'--require "{(site / "appcontainer.cjs").as_posix()}"',
                    "PYTHONATOR_NODE_ROOTS": os.pathsep.join(map(str, roots))})
    return subprocess.run([node, *args], cwd=project, env=env, capture_output=True, text=True, timeout=120,
                          check=False)


def test_without_the_fix_node_fails_as_on_windows(node_app: tuple[str, Path, Path]) -> None:
    node, project, site = node_app
    done = run_node(node, project, site, ["main.js"], shim=False)
    assert done.returncode != 0 and "EPERM: operation not permitted, lstat" in done.stderr


def test_with_the_shim_node_and_its_modules_run(node_app: tuple[str, Path, Path]) -> None:
    node, project, site = node_app
    done = run_node(node, project, site, ["main.js"], shim=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "dep ok true true"
    done = run_node(node, project, site, ["esm.mjs"], shim=True)
    assert done.returncode == 0 and done.stdout.strip() == "esm dep ok", done.stderr


def test_with_the_shim_npm_runs(node_app: tuple[str, Path, Path]) -> None:
    node, project, site = node_app
    cli = Path(node).resolve().parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not cli.is_file():
        pytest.skip("npm isn't next to node here")
    done = run_node(node, project, site, [str(cli), "run", "hi"], shim=True)  # npm, then node through a shell
    assert done.returncode == 0, done.stderr
    assert "dep ok true true" in done.stdout


def test_the_shim_changes_nothing_outside_the_sandbox(node_app: tuple[str, Path, Path]) -> None:
    node, project, site = node_app
    shim = (site / windows.NODE_SHIM).as_posix()
    done = subprocess.run([node, "--require", shim, "-e",
                           "const fs = require('fs'); console.log(fs.__pythonatorSandbox === undefined, "
                           "fs.realpathSync.name)"],
                          cwd=project, env={k: v for k, v in os.environ.items() if not k.startswith("PYTHONATOR_")},
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.stdout.split() == ["true", "realpathSync"], done.stderr


# -- removing and deleting in the background -----------------------------------------------------------------


def test_permissions_of_a_folder_about_to_be_deleted_are_not_taken_back_file_by_file(
        dirs: DataDirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    revoked: list[str] = []
    profiles: list[str] = []
    monkeypatch.setattr(launcher, "windows_revoke", lambda folder, _sids: revoked.append(folder))
    monkeypatch.setattr(launcher, "windows_delete_profile", profiles.append)
    project, cache = tmp_path / "project", tmp_path / "cache"
    windows.record_grants(dirs, "pythonator.svc-a", "S-1-15-2-1", [str(project), str(project / "sub"), str(cache)])
    windows.record_grants(dirs, "pythonator.svc-b", "S-1-15-2-2", [str(cache)])
    windows._forget(dirs, "pythonator.svc-a", project)
    assert revoked == [str(cache)] and profiles == ["pythonator.svc-a"]
    assert list(windows.read_grants(dirs)) == ["pythonator.svc-b"]


def test_the_ledger_takes_writes_from_many_threads(dirs: DataDirs) -> None:
    threads = [threading.Thread(target=windows.record_grants, args=(dirs, f"c{i}", f"S-1-15-2-{i}", [f"/f{i}"]))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(windows.read_grants(dirs)) == 20


def test_a_removal_cleans_up_in_the_background(harness: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               ) -> None:

    sup = harness.daemon.sup
    sid = harness.add(name="slow", folder=str(tmp_path), runtime="python", entry="x.py")
    release = threading.Event()
    seen: list[tuple[str, Path | None]] = []

    def slow_forget(_service: Service, _dirs: DataDirs, *, deleted: Path | None = None) -> None:
        seen.append((threading.current_thread().name, deleted))
        release.wait(10)  # Windows: minutes over a big node_modules

    monkeypatch.setattr(isolation, "forget", slow_forget)
    done: list[str] = []
    began = time.monotonic()
    sup.remove(sid, then=lambda: done.append(threading.current_thread().name), deleted=tmp_path)
    assert time.monotonic() - began < 1 and sid not in sup.slots  # gone from the list at once
    assert harness.ok({"op": "ping"}) is not None  # the background service still answers
    harness.wait(lambda: bool(seen))
    assert seen == [(f"remove-{sid}", tmp_path)] and not done
    release.set()
    harness.wait(lambda: done == [f"remove-{sid}"])


def test_a_held_file_doesnt_stop_a_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.daemon import supervisor

    folder = tmp_path / "project"
    (folder / "node_modules").mkdir(parents=True)
    calls: list[Path] = []
    real = shutil.rmtree

    def busy_twice(path: Path, **kwargs: Any) -> None:
        calls.append(Path(path))
        if len(calls) < 3:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        real(path, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", busy_twice)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    assert supervisor._delete_tree(folder) == "" and not folder.exists() and len(calls) == 3
    calls.clear()
    folder.mkdir()

    def denied(path: Path, **_kwargs: Any) -> None:
        calls.append(Path(path))
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(shutil, "rmtree", denied)
    assert supervisor._delete_tree(folder) == "Access is denied" and len(calls) == 4


def test_a_first_start_copies_node_in_the_background(harness: Any, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:

    sup = harness.daemon.sup
    (tmp_path / "x.py").write_text("print('ran')", encoding="utf-8")
    sid = harness.add(name="first", folder=str(tmp_path), runtime="python", entry="x.py", mode="full",
                      own_env=False)
    release = threading.Event()
    copies: list[str] = []

    def copy() -> None:
        copies.append(threading.current_thread().name)
        release.wait(10)

    monkeypatch.setattr(isolation, "slow_setup", lambda *_a, **_k: copy if len(copies) % 2 == 0 else None)
    try:
        sup.start(sid, confirmed=True)
        harness.wait(lambda: bool(copies))
        assert sup.slots[sid].state.value == "starting" and sup.slots[sid].runner is None
        assert "Copying Node.js for sandboxes" in sup.tail(sid) and copies == [f"set-up-{sid}"]
        assert harness.ok({"op": "ping"}) is not None  # the background service answers meanwhile
        release.set()
        harness.wait(lambda: "ran" in sup.tail(sid))
        harness.wait(lambda: sup.slots[sid].runner is None)

        copies.append("done")
        release.clear()
        sup.start(sid, confirmed=True)
        harness.wait(lambda: len(copies) == 3)
        sup.stop(sid)  # stopped while the copy runs: it never starts
        assert sup.slots[sid].state.value == "stopped"
        release.set()
        harness.qtbot.wait(500)
        assert sup.slots[sid].runner is None and sup.slots[sid].state.value == "stopped"
    finally:
        release.set()


def test_a_step_waits_for_the_copy_and_stop_ends_it(harness: Any, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:

    sup = harness.daemon.sup
    sid = harness.add(name="steps", folder=str(tmp_path), runtime="python", entry="x.py", mode="full")
    release = threading.Event()
    copies: list[str] = []

    def copy() -> None:
        copies.append("copy")
        release.wait(10)

    monkeypatch.setattr(isolation, "slow_setup", lambda *_a, **_k: copy)
    try:
        sup._run_steps(sup.slots[sid], [["python", "-c", "print('step ran')"]], "Installing packages")
        harness.wait(lambda: bool(copies))
        slot = sup.slots[sid]
        assert slot.state.value == "task" and slot.task is None and slot.steps
        sup.stop(sid)
        assert slot.state.value == "failed" and not slot.steps
        release.set()
        harness.qtbot.wait(500)
        assert slot.task is None and "step ran" not in sup.tail(sid)
    finally:
        release.set()


def test_the_window_lets_go_of_a_folder_before_deleting_it(qtbot: Any, tmp_path: Path) -> None:
    from pythonator.ui.editor import EditorPanel

    project, other = tmp_path / "project", tmp_path / "other"
    (project / "src").mkdir(parents=True)
    other.mkdir()
    (project / "src" / "app.js").write_text("x", encoding="utf-8")
    (other / "keep.py").write_text("y", encoding="utf-8")
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel.set_root(str(project), "project")
    panel.open(project / "src" / "app.js")
    panel.open(other / "keep.py")
    panel.current().setText("changed")  # type: ignore[union-attr]  # not asked about: the folder is going
    watched = panel.files.model
    panel.release(project)
    assert panel.files.root == "" and panel.files.model is not watched
    assert [panel.tabs.widget(i).path for i in range(panel.tabs.count())] == [other / "keep.py"]  # type: ignore[union-attr]
    panel.files.set_root(str(other), "other")  # the new model works
    assert panel.files.view.model() is panel.files.model and panel.files.root == str(other)


# -- the Project builder: one Create ---------------------------------------------------------------------------


def test_the_builder_has_one_create(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator import sdk
    from tests.test_official_plugins import plugin_module

    monkeypatch.setattr(sdk, "_send", lambda _m: None)
    builder = plugin_module("builder")
    plugin = builder.Builder(permissions=frozenset({"ui", "create_services"}))
    shown: list[dict[str, Any]] = []
    asked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(plugin, "project_templates", lambda: {"templates": [
        {"key": "vue", "label": "Vue", "tool": "create-vue", "port": 5173, "kind": "web", "ready": True,
         "needs": ""}], "folder": "/p"})
    monkeypatch.setattr(plugin, "list_projects", list)
    monkeypatch.setattr(plugin, "show", shown.append)

    def create(*args: Any) -> dict[str, Any]:
        asked.append(args)
        return {"folder": "/p/x", "port": 0}

    monkeypatch.setattr(plugin, "create_project", create)
    plugin.on_start()
    deadline = time.monotonic() + 5
    while plugin.loading and time.monotonic() < deadline:
        time.sleep(0.01)
    plugin.on_action("template", "Vue")
    text = json.dumps(shown[-1])
    assert text.count('"action": "create"') == 1 and "Create my-vue" not in text
    assert "my-vue if empty" in text
    plugin.on_action("create", "")
    deadline = time.monotonic() + 5
    while (plugin.busy or plugin.loading) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert asked == [("vue", "my-vue", "", True)]
