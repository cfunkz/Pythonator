"""0.0.16, the audit: nothing a sandboxed service writes in its own folder runs outside the sandbox or opens more
of the computer to it (git config, its venv's Python, links and loader variables in .env, pyvenv.cfg), plugins
can't hold up the background service or read through links, and the window shows outside text as text."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator.core import envcheck, git
from pythonator.isolation import base, compose
from pythonator.isolation.base import Command
from pythonator.paths import DataDirs
from pythonator.plugins.files import FileRefused, ServiceFiles

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account can't make symbolic links")


# -- git ---------------------------------------------------------------------------------------------------------


@needs_git
def test_a_pull_refuses_a_checkout_whose_config_names_a_program(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/x.git"], check=True)
    git.check_checkout(repo)  # what git init and a clone write is fine
    subprocess.run(["git", "-C", str(repo), "config", "core.sshCommand", "touch pwned"], check=True)
    with pytest.raises(git.GitError, match=r"core\.sshcommand"):
        git.check_checkout(repo)


@needs_git
@pytest.mark.parametrize("key", ["filter.x.smudge", "credential.helper", "include.path", "core.gitProxy"])
def test_every_setting_that_runs_something_is_refused(tmp_path: Path, key: str) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", key, "x"], check=True)
    with pytest.raises(git.GitError, match="run programs"):
        git.check_checkout(repo)


def test_git_hooks_folder_is_private(dirs: DataDirs) -> None:
    flag = git._hooks_off()[1]
    assert flag.startswith("core.hooksPath=") and Path(flag.split("=", 1)[1]).is_relative_to(dirs.root)


# -- the project's own files never run outside the sandbox -------------------------------------------------------


def test_the_version_check_reads_the_venv_python_instead_of_running_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    venv = tmp_path / ".venv"
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_text(f"#!/bin/sh\ntouch {marker}\necho Python 3.13.0\n")
    py.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.1\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    problem = envcheck._declared_version("python", tmp_path, str(py))
    assert "3.11.0 or newer (this computer has 3.9.1)" in problem
    assert not marker.exists()


def test_env_file_links_out_of_the_folder_are_not_read(tmp_path: Path) -> None:
    folder, outside = tmp_path / "svc", tmp_path / "home"
    folder.mkdir()
    outside.mkdir()
    (outside / "credentials").write_text("aws_secret_access_key = hunter2hunter2\n")
    _link(folder / ".env", outside / "credentials")
    assert base.read_env_file(folder, ".env") == {}
    assert base.read_env_file(folder, "../home/credentials") == {}


def test_env_files_cant_change_programs_outside_the_sandbox(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LD_LIBRARY_PATH=/x\nPATH=/x\nDOCKER_HOST=ssh://x\nDOCKER_CONFIG=/x\n"
                                   "HOME=/x\nXDG_RUNTIME_DIR=/x\nGCONV_PATH=/x\nAPI_KEY=fine\n")
    assert base.read_env_file(tmp_path, ".env") == {"API_KEY": "fine"}


def test_inherited_secrets_stay_out_of_a_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.core.models import Service

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_inherited")
    monkeypatch.setenv("LANG", "C.UTF-8")
    service = Service(name="s", folder=str(tmp_path), runtime="custom", command=["x"], env={"MY_TOKEN": "mine"})
    sandboxed = base.base_env(service, sandboxed=True)
    assert "GITHUB_TOKEN" not in sandboxed and sandboxed["MY_TOKEN"] == "mine" and sandboxed["LANG"] == "C.UTF-8"
    assert base.base_env(service, sandboxed=False)["GITHUB_TOKEN"] == "ghp_inherited"  # Full access: yours


def test_pyvenv_cfg_cant_grant_a_sandbox_any_folder(tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()  # runtime_folders resolves (macOS /private/var, Windows short names)
    project, victim = tmp_path / "project", tmp_path / "victim"
    (victim / "bin").mkdir(parents=True)
    (victim / "secret.txt").write_text("x")
    venv = project / ".venv"
    py = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = {victim / 'bin'}\n")
    folders = base.runtime_folders(str(py), project)
    assert victim not in folders and venv in folders
    (victim / "lib" / "python3.13").mkdir(parents=True)  # a real Python installation is fine
    (victim / "lib" / "python3.13" / "os.py").write_text("")
    (victim / "Lib").mkdir(exist_ok=True)
    (victim / "Lib" / "os.py").write_text("")
    assert victim in base.runtime_folders(str(py), project)


def test_a_folder_that_holds_keys_cant_be_given_to_a_sandbox(dirs: DataDirs) -> None:
    assert base.forbidden_read(Path.home() / ".config", dirs) == "a folder of passwords or keys"
    assert base.forbidden_write(Path.home() / ".ssh" / "x", dirs) == "a folder of passwords or keys"
    assert base.forbidden_read(Path.home() / "projects" / "app", dirs) == ""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox plan")
def test_linux_sandbox_reads_only_the_device_files_it_needs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from pythonator.isolation import linux

    sup = linux.Support(landlock=5, seccomp="libseccomp.so.2", bwrap="")
    monkeypatch.setattr(linux, "support", lambda: sup)
    cmd = linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
    plan = json.loads(cmd.argv[-1])
    assert "/dev" not in plan["landlock"]["ro"] and "/dev/null" in plan["landlock"]["rw"]
    assert "/dev/pts" not in plan["landlock"]["rw"]  # without bubblewrap those are your own terminals
    assert plan["seccomp"]["unix"]
    with_bwrap = linux.Support(landlock=5, seccomp="libseccomp.so.2", bwrap="/usr/bin/bwrap")
    monkeypatch.setattr(linux, "support", lambda: with_bwrap)
    cmd = linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
    assert json.loads(cmd.argv[-1])["seccomp"]["unix"]  # Landlock 5 can't keep X11's abstract socket out


def test_compose_audit_sees_bind_mounts_by_another_name(tmp_path: Path) -> None:
    config: dict[str, Any] = {
        "services": {"app": {"image": "x", "volumes": [{"type": "volume", "source": "data", "target": "/d"}],
                             "networks": {"outside": None}}},
        "volumes": {"data": {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/root"}}},
        "networks": {"outside": {"name": "host", "external": True}},
    }
    problems = compose.audit(config, tmp_path, lan=False)
    assert any("mounts /root" in p for p in problems) and any("host network" in p for p in problems)


# -- the background service never waits on a program or a plugin -------------------------------------------------


def test_input_to_a_program_that_never_reads_doesnt_block(tmp_path: Path) -> None:
    from pythonator.daemon.runner import MAX_INPUT, Runner

    sink: queue.SimpleQueue[Any] = queue.SimpleQueue()
    cmd = Command([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, dict(os.environ))
    runner = Runner("x", cmd, sink)
    runner.start()
    try:
        began = time.monotonic()
        results = [runner.write("é" * 65_536) for _ in range(MAX_INPUT + 50)]
        assert time.monotonic() - began < 2
        assert not results[-1]  # refused once it hasn't read what it was given
    finally:
        runner.kill()


def test_notify_reads_every_retry_after() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("notify_plugin", Path(__file__).parent.parent / "plugins"
                                                  / "notify" / "plugin.py")
    assert spec is not None and spec.loader is not None
    notify: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = notify  # its dataclasses look themselves up there
    try:
        spec.loader.exec_module(notify)
    finally:
        sys.modules.pop(spec.name, None)
    assert notify.retry_after("7") == 7
    assert notify.retry_after("-3") == 1
    assert notify.retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 1
    assert notify.retry_after(None) == 5 and notify.retry_after("soon") == 5


# -- what plugins may read and suggest ---------------------------------------------------------------------------


def test_plugins_cant_read_through_links(tmp_path: Path) -> None:
    (tmp_path / "id_rsa").write_text("PRIVATE KEY\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url = https://ghp_token@github.com/x\n")
    _link(tmp_path / "readme.md", tmp_path / "id_rsa")
    _link(tmp_path / "g", tmp_path / ".git")
    files = ServiceFiles(tmp_path)
    for name in ("readme.md", "g/config"):
        with pytest.raises(FileRefused, match="link"):
            files.read(name)
    with pytest.raises(FileRefused, match="link"):
        files.propose("p", "s", "readme.md", [{"old": "PRIVATE", "new": "x"}], "")


def test_a_plugin_cant_guess_a_hidden_value(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("KEY = 'live-value-123456'\nprint(KEY)\n")
    files = ServiceFiles(tmp_path, "", {"API_KEY": "live-value-123456"})
    answers = set()
    for guess in ("KEY = 'live", "KEY = 'dead"):
        with pytest.raises(FileRefused) as info:
            files.propose("p", "s", "app.py", [{"old": guess, "new": "x"}], "")
        answers.add(str(info.value))
    assert len(answers) == 1  # right and wrong guesses get the same answer


def test_proposals_cant_add_ci_workflows_or_hidden_process_calls(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n")
    files = ServiceFiles(tmp_path)
    for new in ("from os import system as s", "getattr(os, 'sys' + 'tem')('x')", "__builtins__.eval"):
        with pytest.raises(FileRefused, match="Blocked"):
            files.propose("p", "s", "app.py", [{"old": "print('hi')", "new": new}], "")
    with pytest.raises(FileRefused, match="script"):
        files.propose("p", "s", ".github/workflows/x.yml", [{"old": "", "new": "on: push"}], "")


# -- the window shows outside text as text -----------------------------------------------------------------------


def test_labels_and_tooltips_never_read_html(qtbot: Any) -> None:
    from PyQt6.QtCore import Qt

    from pythonator.ui.widgets import label, tip

    assert label("<img src='/dev/zero'>").textFormat() == Qt.TextFormat.PlainText
    assert tip("plain name") == "plain name"
    assert tip("<img src='/dev/zero'>") == "<qt>&lt;img src=&#x27;/dev/zero&#x27;&gt;</qt>"


def test_a_failed_save_keeps_the_tab_open(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from pythonator.ui import editor as editor_module
    from pythonator.ui.editor import CodeEditor, EditorPanel

    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.open(path)
    ed = panel.current()
    assert isinstance(ed, CodeEditor)
    ed.setText("x = 2\n")
    warned: list[str] = []
    monkeypatch.setattr(editor_module, "ask", lambda *a, **k: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(editor_module, "warn", lambda _p, _t, text, *a, **k: warned.append(text))

    def refuse() -> bool:
        raise PermissionError("in use")

    monkeypatch.setattr(ed, "save", refuse)
    panel.close_tab(panel.tabs.indexOf(ed))
    assert panel.tabs.indexOf(ed) >= 0 and warned  # still open, and the user was told


def test_saving_over_a_change_on_disk_asks_first(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from pythonator.ui import editor as editor_module
    from pythonator.ui.editor import EditorPanel

    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    panel = EditorPanel(11)
    qtbot.addWidget(panel)
    panel.open(path)
    ed = panel.current()
    assert ed is not None
    ed.setText("x = 2\n")
    path.write_text("x = 3  # pulled\n")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
    monkeypatch.setattr(editor_module, "ask", lambda *a, **k: QMessageBox.StandardButton.Cancel)
    panel.save()
    assert path.read_text() == "x = 3  # pulled\n"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux sandbox without bubblewrap")
def test_stop_ends_a_program_that_left_its_process_group(harness: Any, project: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    from pythonator.isolation import linux

    real = linux.support()
    if not real.landlock:
        pytest.skip("no Landlock here")
    monkeypatch.setattr(linux, "support", lambda: linux.Support(real.landlock, real.seccomp, ""))
    (project / "d.py").write_text("import os, time\nif os.fork() == 0:\n    os.setsid()\n    if os.fork() == 0:\n"
                                  "        os.execvp('sleep', ['sleep', '3117'])\n    os._exit(0)\n"
                                  "print('forked', flush=True)\nwhile True: time.sleep(0.2)\n")

    def daemonised() -> list[psutil.Process]:
        return [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] == ["sleep", "3117"]]

    sid = harness.add(name="d", folder=str(project), entry="d.py", restart="never")
    try:
        harness.ok({"op": "start", "id": sid})
        harness.wait(lambda: "forked" in harness.output(sid))
        harness.wait(lambda: bool(daemonised()))
        time.sleep(2.5)  # the stats thread has seen it in the service's tree
        harness.ok({"op": "stop", "id": sid})
        harness.wait(lambda: harness.states(sid)[-1] == "stopped")
        harness.wait(lambda: not daemonised(), timeout=3000)
    finally:
        for p in daemonised():
            p.kill()


def test_a_plugin_that_crashed_shows_its_panel_again(harness: Any, tmp_path: Path) -> None:
    """After an unexpected exit the window's panel is cleared, so the same panel from the restarted plugin must be
    shown again, not skipped as unchanged (Windows CI: the AI assistant's panel never came back)."""
    from tests.test_plugins import install

    install(harness, tmp_path, "phoenix", "from pythonator.sdk import ui\nclass P(Plugin):\n    def on_start(self):\n"
            "        self.show(ui.panel('Same', ui.text('the same panel')))\n"
            "        mark = self.data_dir / 'crashed'\n"
            "        if 'ui' in self.permissions and not mark.exists():\n"
            "            mark.write_text('x')\n"
            "            time.sleep(0.5)\n"
            "            os._exit(3)\n", perms=["ui"])
    harness.ok({"op": "plugin_grant", "plugin": "phoenix", "permissions": ["ui"], "live": True})

    def panels() -> list[Any]:
        return [e.get("panel") for e in harness.events if e.get("ev") == "panel" and e.get("plugin") == "phoenix"]

    harness.wait(lambda: sum(1 for x in panels() if x) >= 2 and bool(panels()[-1]), timeout=30_000)
