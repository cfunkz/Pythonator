"""Guard rails: private files, safe quoting for cmd.exe, risky commands, secrets at prompts."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pythonator.core.launch import (
    LaunchError,
    LaunchSpec,
    base_environment,
    batch_cmdline,
    build_launch,
    build_shell,
    cmd_quote,
    node_bin_dirs,
)
from pythonator.core.logs import LogStore
from pythonator.core.models import AppConfig, LaunchMode, Runtime, ServiceConfig
from pythonator.core.paths import DataPaths
from pythonator.core.runner import ProcessRunner
from pythonator.core.safety import is_secret_prompt, risky_command, unsafe_reason
from pythonator.supervisor import Supervisor

from .conftest import wait_until
from .test_runner import Collector

WINDOWS = os.name == "nt"
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX permissions")


# -- risky commands -------------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "rm -rf /", "sudo rm -rf / --no-preserve-root", "rm -rf ~", "rm -fr *", "cd x && rm -rf ..", "rm -r $HOME",
    "rm -rf /usr", "rm -rf '/'", "rd /s /q C:\\", "rmdir /S %USERPROFILE%", "del /s /q *.pyc",
    "Remove-Item -Recurse -Force C:\\", "mkfs.ext4 /dev/sda1", "format c:", "dd if=x.img of=/dev/sdb",
    "echo x > /dev/sda", "shutdown -h now", "sudo reboot", "Stop-Computer", "chmod -R 777 /", "chown -R me ~",
    ":(){ :|:& };:", "curl -fsSL https://get.example.sh | bash", "wget -qO- https://x | sh",
    "iwr https://x | iex", "reg delete HKLM\\Software\\X", "git clean -fdx",
])
def test_risky_commands_are_flagged(command: str) -> None:
    assert risky_command(command)


@pytest.mark.parametrize("command", [
    "rm -rf node_modules", "rm -rf ./build", "rm -rf /tmp/x", "rm -rf /home/me/proj/dist", "rm file.txt",
    "rd /s /q node_modules", "pip install requests", "npm run shutdown-test", "python manage.py shutdown_workers",
    "echo reboot", "dd if=/dev/zero of=file bs=1M count=1", "chmod +x run.sh", "chmod -R 755 ./bin",
    "curl https://api.example.com/health", "git clean -fd", "cat /etc/hosts", "python format_output.py",
])
def test_ordinary_commands_are_not_flagged(command: str) -> None:
    assert risky_command(command) is None


def test_secret_prompts() -> None:
    for prompt in ("Enter password: ", "Discord token:", "API key? ", "Enter PIN:", "\x1b[1mPassphrase\x1b[0m:"):
        assert is_secret_prompt(prompt.replace("\x1b[1m", "").replace("\x1b[0m", "")), prompt
    for text in ("Ping: 32ms", "Loaded 3 tokens", "hello", "Token refreshed", ""):
        assert not is_secret_prompt(text), text


def test_answer_to_password_prompt_is_not_logged(tmp_path: Path, qapp: object) -> None:
    script = tmp_path / "ask.py"
    script.write_text("secret = input('Password: ')\nprint('got', len(secret), flush=True)\n"
                      "name = input('Name? ')\nprint('hi', name, flush=True)\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="ask", target=str(script), python=sys.executable))
    assert sup.start(svc.id)
    assert wait_until(lambda: "Password" in svc.log.last_output(), qt=True)
    assert sup.send_input(svc.id, "hunter2")
    assert wait_until(lambda: any(ln.text.endswith("got 7") for ln in svc.log.snapshot()), qt=True)
    assert wait_until(lambda: "Name" in svc.log.last_output(), qt=True)
    assert sup.send_input(svc.id, "Ada")  # not a secret: echoed as typed
    assert wait_until(lambda: any(ln.text.endswith("hi Ada") for ln in svc.log.snapshot()), qt=True)
    sup.shutdown()
    logged = svc.log.path.read_text(encoding="utf-8")
    assert "hunter2" not in logged and "> ••••••••" in logged and "> Ada" in logged


# -- cmd.exe quoting ------------------------------------------------------------------------------


def test_cmd_quote() -> None:
    assert cmd_quote("Newtonsoft.Json") == "Newtonsoft.Json"
    assert cmd_quote("a&calc") == '"a&calc"'
    assert cmd_quote("x|y>z^w") == '"x|y>z^w"'
    assert cmd_quote("100%PATH%") == '"100%%cd:~,%PATH%%cd:~,%"'
    assert cmd_quote("C:\\dir with space\\") == '"C:\\dir with space\\\\"'
    assert cmd_quote("") == '""'
    for bad in ('say "hi"', "two\nlines"):
        with pytest.raises(LaunchError):
            cmd_quote(bad)


def test_batch_cmdline_quotes_every_argument() -> None:
    line = batch_cmdline(["C:\\app\\run.bat", "plain", "a&calc", "x y"], "C:\\Windows\\System32\\cmd.exe")
    assert line == '"C:\\Windows\\System32\\cmd.exe" /d /v:off /s /c ""C:\\app\\run.bat" plain "a&calc" "x y""'


@pytest.mark.skipif(not WINDOWS, reason="batch files only run on Windows")
def test_batch_file_arguments_cannot_run_commands(tmp_path: Path) -> None:
    bat = tmp_path / "show.bat"
    bat.write_text("@echo off\r\necho ARG1=%1\r\necho ARG2=%2\r\n")
    spec = LaunchSpec([str(bat), "a&echo INJECTED", "100%PATH%"], tmp_path, base_environment())
    assert spec.cmdline is not None  # built for cmd.exe, not by list2cmdline
    c = Collector()
    ProcessRunner(spec, c.out, c.exit).start()
    assert c.done.wait(15)
    lines = [ln.strip() for ln in c.text.splitlines() if ln.strip()]
    assert lines == ['ARG1="a&echo INJECTED"', 'ARG2="100%PATH%"'], lines


@pytest.mark.skipif(not WINDOWS, reason="batch files only run on Windows")
def test_batch_service_gets_a_safe_command_line(tmp_path: Path) -> None:
    bat = tmp_path / "run.bat"
    bat.write_text("@echo off\r\n")
    spec = build_launch(ServiceConfig(name="b", runtime=Runtime.SHELL, target=str(bat), args='"a&b"'))
    assert spec.cmdline is not None and '"a&b"' in spec.cmdline and "/v:off" in spec.cmdline


def test_shell_comes_from_pythonators_environment(tmp_path: Path) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / ("cmd.exe" if WINDOWS else "bash")).write_text("planted")
    cfg = ServiceConfig(name="s", mode=LaunchMode.COMMAND, target="x", workdir=str(tmp_path),
                        env={"PATH": str(fake), "COMSPEC": str(fake / "cmd.exe")})
    spec = build_shell(cfg, "echo hi")
    assert not spec.argv[0].startswith(str(fake))
    assert spec.env["PATH"].startswith(str(fake))  # the command itself still sees the project's PATH


# -- config and data files ------------------------------------------------------------------------


def test_service_ids_cannot_point_outside_the_data_folder() -> None:
    cfg = AppConfig.from_dict({"services": [{"name": "a", "id": "../../../.bashrc"}, {"name": "b", "id": "ok_id-1"}]})
    assert cfg.services[0].id != "../../../.bashrc" and "/" not in cfg.services[0].id
    assert cfg.services[1].id == "ok_id-1"


def test_safety_flags_round_trip_and_default_off() -> None:
    cfg = AppConfig()
    assert not cfg.settings.allow_elevated
    cfg.settings.allow_elevated = True
    cfg.services.append(ServiceConfig(name="a", install_confirmed=True))
    back = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert back.settings.allow_elevated and back.services[0].install_confirmed
    loose = AppConfig.from_dict({"settings": {"allow_elevated": "yes"}, "services": [{"install_confirmed": 1}]})
    assert not loose.settings.allow_elevated and not loose.services[0].install_confirmed


def test_moving_a_service_to_another_folder_asks_before_installing_again(tmp_path: Path, qapp: object) -> None:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="a", workdir=str(tmp_path), install_confirmed=True))
    sup.update(svc.config.copy(args="--x"))
    assert svc.config.install_confirmed
    sup.update(svc.config.copy(workdir=str(tmp_path / "other")))
    assert not svc.config.install_confirmed


@posix_only
def test_data_files_are_private(tmp_path: Path, qapp: object) -> None:
    paths = DataPaths(tmp_path / "data")
    sup = Supervisor(paths)
    svc = sup.add(ServiceConfig(name="a", env={"TOKEN": "secret"}))
    sup.save()  # a second save writes the .bak
    svc.log.system("hello")
    svc.log.close()

    def mode(p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    assert mode(paths.root) == mode(paths.logs) == 0o700
    assert mode(paths.config) == mode(paths.config.with_name("pythonator.json.bak")) == 0o600
    assert mode(svc.log.path) == 0o600
    sup.shutdown()


@posix_only
def test_existing_readable_files_are_locked_down(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True)
    (root / "pythonator.json.bak").write_text("{}")
    (root / "logs").chmod(0o755)
    (root / "pythonator.json.bak").chmod(0o644)
    DataPaths(root).ensure()
    assert stat.S_IMODE((root / "logs").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "pythonator.json.bak").stat().st_mode) == 0o600


@posix_only
def test_settings_others_can_change_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    root.chmod(0o777)
    paths = DataPaths(root).ensure()
    assert paths.exposed and "every user" in paths.exposed
    assert stat.S_IMODE(root.stat().st_mode) == 0o700  # tightened for next time
    paths.ensure()
    assert paths.exposed  # still reported for this session
    assert DataPaths(root).ensure().exposed is None


@posix_only
def test_log_store_creates_private_file(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "x.log")
    store.system("token=abc")
    store.close()
    assert stat.S_IMODE((tmp_path / "x.log").stat().st_mode) == 0o600


@posix_only
def test_world_writable_node_modules_is_not_put_on_path(tmp_path: Path) -> None:
    shared = tmp_path / "node_modules" / ".bin"
    shared.mkdir(parents=True)
    project = tmp_path / "project"
    (project / "node_modules" / ".bin").mkdir(parents=True)
    assert node_bin_dirs(project)[:2] == [project / "node_modules" / ".bin", shared]
    (tmp_path / "node_modules").chmod(0o777)
    assert unsafe_reason(tmp_path / "node_modules")
    dirs = node_bin_dirs(project)
    assert dirs[0] == project / "node_modules" / ".bin" and shared not in dirs


# -- UI helpers -----------------------------------------------------------------------------------


def test_file_names_stay_inside_the_folder(qapp: object) -> None:
    from pythonator.ui.file_tree import _valid_name

    assert _valid_name("main.py") and _valid_name("src/app/main.py", nested=True)
    for bad in ("", ".", "..", "../x.py", "a/../../x", "/etc/passwd", "a\0b"):
        assert not _valid_name(bad, nested=True), bad
    assert not _valid_name("src/main.py")  # renaming can't move things into another folder
    if WINDOWS:
        for bad in ("C:\\x", "CON", "nul.txt", "name.", "a:b"):
            assert not _valid_name(bad, nested=True), bad


def test_only_web_links_are_opened(qapp: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    from pythonator.ui import main_window

    opened: list[str] = []

    def fake_open(url: QUrl) -> bool:
        opened.append(url.toString())
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", fake_open)
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ms-msdt:/id x", "smb://host/share", "http://"):
        assert not main_window.open_web_url(url), url
    assert main_window.open_web_url("http://localhost:8000/docs")
    assert opened == ["http://localhost:8000/docs"]
