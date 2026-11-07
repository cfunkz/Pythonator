"""Windows: Node.js in the AppContainer sandbox (its private copy and the shim), program folders sandboxes can't
use, Make reachable, PowerShell and WSL, and the grants ledger."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation, launcher
from pythonator.core.models import Service
from pythonator.isolation import base, private_node, program_folders, windows
from pythonator.paths import DataDirs


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


def fake_pe(body: bytes, certificate: bytes = b"") -> bytes:
    """A PE32+ program: header, ``body``, and an Authenticode certificate table at the end when given."""
    head = bytearray(0x200)
    head[:2] = b"MZ"
    head[0x3C:0x40] = (0x80).to_bytes(4, "little")
    head[0x80:0x84] = b"PE\0\0"
    head[0x98:0x9A] = (0x20B).to_bytes(2, "little")  # optional header magic: PE32+
    start = len(head) + len(body)
    if certificate:
        entry = 0x98 + 112 + 4 * 8
        head[entry:entry + 8] = start.to_bytes(4, "little") + len(certificate).to_bytes(4, "little")
    return bytes(head) + body + certificate


def test_the_sandbox_copy_of_node_names_its_pipes_under_local(tmp_path: Path) -> None:
    from pythonator.isolation import private_node

    assert len(private_node.PIPE_NAME) == len(private_node.LOCAL_PIPE_NAME)  # nothing after it moves
    exe = tmp_path / "node.exe"
    body = b"code" + private_node.PIPE_NAME + b"more code"
    exe.write_bytes(fake_pe(body, certificate=b"\x08\x00\x00\x00signed!!"))
    assert private_node.pipes_hang(exe)
    assert private_node.fix_pipe_names(exe)
    fixed = exe.read_bytes()
    assert fixed == fake_pe(b"code" + private_node.LOCAL_PIPE_NAME + b"more code")  # and no signature
    assert not private_node.pipes_hang(exe) and not private_node.fix_pipe_names(exe)
    other = tmp_path / "other.exe"  # a Node.js with libuv 1.53's names already: left as it is
    other.write_bytes(fake_pe(b"\\\\?\\pipe\\%suv\\%llu-%lu\x00", certificate=b"sig"))
    assert not private_node.pipes_hang(other) and not private_node.fix_pipe_names(other)


def test_a_node_whose_pipes_hang_runs_from_the_fixed_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Node.js the sandbox could read where it is installed (an nvm-windows one, or a shared folder) still runs
    from the copy when its pipes would hang; one with fixed pipe names runs where it is."""
    from pythonator.isolation import private_node, windows
    from pythonator.paths import DataDirs

    node = tmp_path / "nodejs"
    (node / "node_modules" / "npm" / "bin").mkdir(parents=True)
    (node / private_node.EXE).write_bytes(fake_pe(private_node.PIPE_NAME))
    monkeypatch.setattr(private_node, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "blocked_program_folder", lambda folder: False)
    dirs = DataDirs(tmp_path / "d")
    argv, _env, private = private_node.for_sandbox([str(node / private_node.EXE), "vite.js"], {"PATH": ""}, dirs)
    assert private is not None and argv == [str(private / private_node.EXE), "vite.js"]
    assert private.name.startswith("node-") and private.parent == dirs.root / "runtimes"
    assert private_node.LOCAL_PIPE_NAME in (private / private_node.EXE).read_bytes()
    assert private_node.PIPE_NAME in (node / private_node.EXE).read_bytes()  # the installed one is never changed
    (node / private_node.EXE).write_bytes(fake_pe(b"\\\\?\\pipe\\%suv\\%llu-%lu\x00 and a newer Node.js"))
    same = [str(node / private_node.EXE), "vite.js"]
    assert private_node.for_sandbox(same, {"PATH": ""}, dirs) == (same, {"PATH": ""}, None)


REALPATH = """
const fs = require('fs'); const path = require('path');
const me = path.join(__dirname, 'main.js');
const out = [fs.realpathSync(me) === me, fs.realpathSync.native(me) === me,
             fs.realpathSync.native(me, 'buffer').toString() === me];
try { fs.realpathSync.native(path.join(__dirname, 'missing.js')); } catch (e) { out.push(e.code); }
fs.realpath(me, (e, a) => fs.realpath.native(me, (e2, b) => fs.realpath(path.join(__dirname, 'nope'), (e3) =>
  fs.promises.realpath(me).then((c) => console.log(...out, a === me, b === me, e3.code, c === me)))));
"""


def test_a_realpath_windows_refuses_in_the_sandbox_is_the_path_itself(tmp_path: Path) -> None:
    """Vite asks Windows' own realpath for each file it loads (once ``net use`` has run, which it now can), and an
    AppContainer refuses it: the shim answers with the path, made absolute, when it exists."""
    node, project, site = node_app.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    (project / "real.js").write_text(REALPATH, encoding="utf-8")
    done = run_node(node, project, site, ["real.js"], shim=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["true", "true", "true", "ENOENT", "true", "true", "ENOENT", "true"]


def test_windows_says_whether_a_sandbox_was_made_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CheckNetIsolation LoopbackExempt -s`` lists the exempted AppContainers: Make reachable was done for a
    service when its sandbox's SID is among them."""
    import subprocess

    from pythonator import launcher
    from pythonator.isolation import windows

    if sys.platform != "win32":
        assert windows.loopback_exempt("x") is None  # elsewhere there's nothing to check
    listed = ("List Loopback Exempted AppContainers\n\n[1] -----\n    Name: pythonator.svc-a\n"
              "    SID:  S-1-15-2-11-22-33-44-55-66-77\n\nOK.\n")
    sids = {"svc-a": "S-1-15-2-11-22-33-44-55-66-77", "svc-b": "S-1-15-2-11-22-33-44-55-66-7"}
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "quiet_start", lambda: None)
    monkeypatch.setattr(launcher, "windows_profile", lambda name: (sids[name.rsplit(".", 1)[-1]], "folder"))
    monkeypatch.setattr(windows, "container_name", lambda ident: f"Pythonator.{ident}")
    code = [0]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code[0], listed, ""))
    assert windows.loopback_exempt("svc-a") is True
    assert windows.loopback_exempt("svc-b") is False  # a SID that only starts the same isn't it
    code[0] = 5
    assert windows.loopback_exempt("svc-a") is None  # Windows couldn't say: no note, no question


def test_powershell_is_windows_own() -> None:
    from pythonator.isolation import windows

    ps = windows.powershell()
    # Windows file names ignore case, and GetSystemDirectoryW may answer "system32".
    assert tuple(part.lower() for part in ps.parts[-4:]) == ("system32", "windowspowershell", "v1.0", "powershell.exe")
    assert ps.is_absolute() or sys.platform != "win32"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' System32")
def test_powershell_is_found_through_windows_on_windows() -> None:
    from pythonator.isolation import windows

    system = windows.system32()
    assert system.is_dir() and (system / "kernel32.dll").is_file()
    assert os.path.normcase(str(system)) == os.path.normcase(str(Path(os.environ["SYSTEMROOT"]) / "System32"))
    assert windows.powershell().is_file()


def test_wsl_bash_is_refused_for_a_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    system = tmp_path / "System32"
    monkeypatch.setattr(windows, "system32", lambda: system)
    for name in ("bash.exe", "wsl.exe", "BASH.EXE"):
        with pytest.raises(base.IsolationError, match="WSL"):
            windows.check_not_wsl(str(system / name))
    windows.check_not_wsl(str(tmp_path / "Git" / "bin" / "bash.exe"))  # Git Bash is a normal program
    windows.check_not_wsl(str(system / "cmd.exe"))
