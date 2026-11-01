"""macOS support and the per-platform pieces: PATH for GUI launches, login items, bundles, App Nap."""

from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest

from pythonator.core import login, macos, userenv
from pythonator.core.errors import LaunchError
from pythonator.core.launch import build_launch
from pythonator.core.models import Runtime, ServiceConfig, runtime_for_path
from pythonator.core.runtimes import is_mac_stub
from pythonator.core.safety import risky_command

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shells and permissions")


# -- PATH of an app opened from the Finder / Dock -----------------------------------------------


@posix_only  # PATH completion is for macOS (':'-separated)
def test_launchd_default_path_is_incomplete() -> None:
    assert userenv.looks_incomplete("/usr/bin:/bin:/usr/sbin:/sbin")
    assert not userenv.looks_incomplete("/opt/homebrew/bin:/usr/bin:/bin")
    assert not userenv.looks_incomplete("/usr/local/bin/:/usr/bin")


@posix_only  # PATH completion is for macOS (':'-separated)
def test_merged_path_prefers_the_shell_and_keeps_the_rest(tmp_path: Path) -> None:
    (tmp_path / ".cargo" / "bin").mkdir(parents=True)
    merged = userenv.merged_path("/usr/bin:/bin:/extra", "/shell/a:/usr/bin", home=tmp_path).split(os.pathsep)
    assert merged[:4] == ["/shell/a", "/usr/bin", "/bin", "/extra"]
    assert str(tmp_path / ".cargo" / "bin") in merged  # well-known folders that exist come last
    assert str(tmp_path / ".volta" / "bin") not in merged  # ... and only if they exist
    assert len(merged) == len(set(merged))


@posix_only
def test_login_shell_path_reads_between_markers(tmp_path: Path) -> None:
    shell = tmp_path / "fakesh"
    # A chatty interactive profile: whatever it prints around the command must be ignored.
    shell.write_text('#!/bin/sh\necho "Welcome!"\nPATH=/from/profile:/usr/bin\nexport PATH\n'
                     'shift\n/bin/sh -c "$1"\necho bye\n')
    shell.chmod(shell.stat().st_mode | stat.S_IXUSR)
    assert userenv.login_shell_path(str(shell)) == "/from/profile:/usr/bin"


@posix_only
def test_login_shell_path_gives_up_on_a_hanging_shell(tmp_path: Path) -> None:
    shell = tmp_path / "hangsh"
    shell.write_text("#!/bin/sh\nsleep 30\n")
    shell.chmod(shell.stat().st_mode | stat.S_IXUSR)
    assert userenv.login_shell_path(str(shell), timeout=0.5) is None
    assert userenv.login_shell_path(str(tmp_path / "missing")) is None


def test_complete_path_leaves_a_terminal_path_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"PATH": "/opt/homebrew/bin:/usr/bin"}
    assert not userenv.complete_path(env)
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"
    env = {"PATH": "/usr/bin:/bin", "PYTHONATOR_KEEP_PATH": "1"}
    assert not userenv.complete_path(env, force=True)


@posix_only  # PATH completion is for macOS (':'-separated)
def test_complete_path_merges_the_login_shell_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userenv, "login_shell_path", lambda: "/opt/homebrew/bin:/Users/me/.nvm/bin:/usr/bin")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    assert userenv.complete_path(env, force=True)
    assert env["PATH"].split(os.pathsep)[:5] == [
        "/opt/homebrew/bin", "/Users/me/.nvm/bin", "/usr/bin", "/bin", "/usr/sbin"]


@posix_only
def test_open_file_limit_is_raised() -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(256, soft), hard))
        new = userenv.raise_open_file_limit(1024)
        assert new is not None and new >= min(1024, hard if hard != resource.RLIM_INFINITY else 1024)
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == new
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# -- start at login ------------------------------------------------------------------------------


def test_launch_command_starts_this_app_minimized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHONATOR_HOME", raising=False)
    argv = login.launch_command()
    assert argv[-1] == "--minimized"
    assert argv[1:3] == ["-m", "pythonator"] or (argv[1].endswith("app.py") and Path(argv[1]).is_file())
    monkeypatch.setenv("PYTHONATOR_HOME", str(tmp_path))
    assert login.launch_command()[-2:] == ["--home", str(tmp_path.resolve())]


def test_macos_launch_agent(tmp_path: Path) -> None:
    item = login.LoginItem("darwin", home=tmp_path)
    assert item.supported and not item.is_enabled()
    item.set_enabled(True, ["/Applications/Pythonator.app/Contents/MacOS/Pythonator", "--minimized"])
    assert item.is_enabled()
    assert item.path == tmp_path / "Library" / "LaunchAgents" / f"{login.LABEL}.plist"
    data = plistlib.loads(item.path.read_bytes())
    assert data["Label"] == login.LABEL and data["RunAtLoad"] is True
    assert data["ProgramArguments"] == ["/Applications/Pythonator.app/Contents/MacOS/Pythonator", "--minimized"]
    assert data["LimitLoadToSessionType"] == "Aqua" and "KeepAlive" not in data  # quitting stays quit
    item.set_enabled(False)
    assert not item.is_enabled()
    item.set_enabled(False)  # turning it off twice is fine


def test_linux_autostart_entry_quotes_arguments(tmp_path: Path) -> None:
    item = login.LoginItem("linux", home=tmp_path)
    item.set_enabled(True, ["/home/me/my apps/python", "-m", "pythonator", "--minimized", "--home", "/data/50%$x"])
    assert item.path == tmp_path / ".config" / "autostart" / "pythonator.desktop"
    text = item.path.read_text()
    assert "[Desktop Entry]" in text and "Type=Application" in text
    exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))
    assert exec_line == 'Exec="/home/me/my apps/python" -m pythonator --minimized --home "/data/50%%\\$x"'
    item.set_enabled(False)
    assert not item.path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_windows_run_key() -> None:
    item = login.LoginItem("win32", key=r"Software\PythonatorTests\Run", name="PythonatorTest")
    try:
        item.set_enabled(True, [r"C:\Program Files\Pythonator\Pythonator.exe", "--minimized"])
        assert item.is_enabled()
        assert item._registry_value() == r'"C:\Program Files\Pythonator\Pythonator.exe" --minimized'
    finally:
        item.set_enabled(False)
    assert not item.is_enabled()


# -- macOS programs, placeholders and safety -------------------------------------------------------


def _fake_bundle(root: Path, executable: str = "Server") -> Path:
    app = root / "My Server.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": executable}))
    program = app / "Contents" / "MacOS" / executable
    program.write_text("#!/bin/sh\necho hi\n")
    program.chmod(0o755)
    return app


def test_app_bundles_run_the_program_inside(tmp_path: Path) -> None:
    app = _fake_bundle(tmp_path)
    assert macos.bundle_executable(app) == app / "Contents" / "MacOS" / "Server"
    assert runtime_for_path(str(app)) is Runtime.EXECUTABLE
    assert runtime_for_path("start.command") is Runtime.SHELL
    spec = build_launch(ServiceConfig(name="s", runtime=Runtime.EXECUTABLE, target=str(app), args="--port 1"))
    assert spec.argv == [str(app / "Contents" / "MacOS" / "Server"), "--port", "1"]
    empty = tmp_path / "Empty.app"
    (empty / "Contents").mkdir(parents=True)
    with pytest.raises(LaunchError, match="No program found inside"):
        build_launch(ServiceConfig(name="e", runtime=Runtime.EXECUTABLE, target=str(empty)))


def test_mac_placeholders_are_only_skipped_on_macos() -> None:
    if sys.platform != "darwin":
        assert not is_mac_stub("/usr/bin/java") and not is_mac_stub("/usr/bin/python3")
    assert not is_mac_stub("/opt/homebrew/bin/java")


@pytest.mark.parametrize("command", [
    "rm -rf /Users", "sudo rm -rf /Library/", "rm -rf /System", "diskutil eraseDisk APFS X disk2",
    "diskutil apfs deleteContainer disk3", "cat x > /dev/rdisk2",
])
def test_macos_destructive_commands_are_flagged(command: str) -> None:
    assert risky_command(command)


@pytest.mark.parametrize("command", ["rm -rf /Users/me/proj/dist", "diskutil list", "ls /Library"])
def test_ordinary_macos_commands_pass(command: str) -> None:
    assert risky_command(command) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS App Nap")
def test_app_nap_can_be_turned_off() -> None:
    assert macos.disable_app_nap()
    assert macos.disable_app_nap()  # idempotent


def test_app_nap_is_a_no_op_elsewhere() -> None:
    if sys.platform != "darwin":
        assert not macos.disable_app_nap()


# -- the app: quit requests, shortcuts, settings ---------------------------------------------------


def test_platform_quit_request_stops_services_first(tmp_path: Path, qapp: object) -> None:
    """⌘Q / Dock → Quit / logout must stop services cleanly, even with the window hidden in the tray."""
    from PyQt6.QtCore import QEvent

    from pythonator.core.paths import DataPaths
    from pythonator.supervisor import Status, Supervisor
    from pythonator.ui.app import QuitRouter
    from pythonator.ui.main_window import MainWindow

    from .conftest import wait_until

    script = tmp_path / "bot.py"
    script.write_text("import time\nprint('up', flush=True)\ntime.sleep(60)\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="bot", target=str(script), python=sys.executable, stop_timeout=2))
    window = MainWindow(sup)
    window.hide()  # "kept running in the tray"
    router = QuitRouter(window)
    sup.start(svc.id)
    assert wait_until(lambda: svc.status is Status.RUNNING, qt=True)
    event = QEvent(QEvent.Type.Quit)
    assert router.eventFilter(None, event) is False  # handled: the quit may go ahead
    assert window.closed and svc.status is Status.STOPPED
    assert router.eventFilter(None, QEvent(QEvent.Type.Quit)) is False  # and isn't handled twice


def test_cancelled_quit_request_is_refused(tmp_path: Path, qapp: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QEvent

    from pythonator.core.paths import DataPaths
    from pythonator.supervisor import Supervisor
    from pythonator.ui.app import QuitRouter
    from pythonator.ui.main_window import MainWindow

    window = MainWindow(Supervisor(DataPaths(tmp_path / "data")))
    monkeypatch.setattr(window.editor, "confirm_close_all", lambda: False)  # "Cancel" on unsaved files
    event = QEvent(QEvent.Type.Quit)
    assert QuitRouter(window).eventFilter(None, event) is True
    assert not event.isAccepted() and not window.closed
    monkeypatch.undo()
    window.quit()
    assert window.closed


def test_shortcut_labels_follow_the_platform(qapp: object) -> None:
    from PyQt6.QtGui import QKeySequence

    from pythonator.ui import keys

    if keys.IS_MAC:
        assert keys.text(keys.SAVE) == "⌘S" and keys.text(keys.START) == "⌘R"
        assert keys.COMPLETE == "Meta+Space" and keys.GO_TO_LINE == "Ctrl+L"
    else:
        assert keys.text(keys.SAVE) == "Ctrl+S" and keys.text(keys.START) == "F5"
    assert all(not s.isEmpty() for s in keys.sequences(keys.STOP))
    ctrl_h = QKeySequence("Ctrl+H")
    assert [s.toString() for s in keys.unique(ctrl_h, QKeySequence("Ctrl+H"), QKeySequence())] == ["Ctrl+H"]


def test_mac_menu_bar_builds(tmp_path: Path, qapp: object) -> None:
    from pythonator.core.paths import DataPaths
    from pythonator.supervisor import Supervisor
    from pythonator.ui.main_window import MainWindow

    window = MainWindow(Supervisor(DataPaths(tmp_path / "data")))
    window._build_mac_menus()
    bar = window.menuBar()
    assert bar is not None and [a.text() for a in bar.actions()][:2] == ["File", "Window"]
    window.quit()


def test_settings_turn_start_at_login_on_and_off(tmp_path: Path, qapp: object,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QDialog

    from pythonator.core.paths import DataPaths
    from pythonator.supervisor import Supervisor
    from pythonator.ui import main_window
    from pythonator.ui.main_window import MainWindow, SettingsDialog

    item = login.LoginItem("darwin" if sys.platform == "darwin" else "linux", home=tmp_path / "home")
    monkeypatch.setattr(main_window, "LoginItem", lambda: item)
    window = MainWindow(Supervisor(DataPaths(tmp_path / "data")))

    def run_dialog(checked: bool) -> None:
        def fake_exec(dlg: SettingsDialog) -> int:
            assert dlg.start_at_login.isEnabled()
            dlg.start_at_login.setChecked(checked)
            return QDialog.DialogCode.Accepted.value

        monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
        window.open_settings()

    run_dialog(True)
    assert item.is_enabled()
    run_dialog(False)
    assert not item.is_enabled()
    window.quit()


def test_editor_shortcuts_are_not_ambiguous(qapp: object) -> None:
    """Two shortcuts on the same key in one widget both stop working (Qt calls them ambiguous)."""
    from PyQt6.QtGui import QShortcut

    from pythonator.ui.editor_panel import EditorPanel

    panel = EditorPanel()
    keys_seen = [s.key().toString() for s in panel.findChildren(QShortcut) if s.parent() is panel]
    assert len(keys_seen) == len(set(keys_seen)), keys_seen
