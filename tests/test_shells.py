from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from pythonator.core.models import AppSettings, ConsoleShell, ServiceConfig
from pythonator.core.paths import IS_WINDOWS, DataPaths
from pythonator.core.shells import Shell, command_hint, resolve_shell, shell_choices
from pythonator.supervisor import Supervisor

from .conftest import wait_until


def _decoded(argv: list[str]) -> str:
    assert "-EncodedCommand" not in argv  # it makes PowerShell print errors as CLIXML
    assert argv[argv.index("-OutputFormat") + 1] == "Text"
    return argv[argv.index("-Command") + 1]


def test_powershell_gets_the_script_exactly_and_stops_on_errors_only_for_multiline() -> None:
    ps = Shell(ConsoleShell.POWERSHELL, "pwsh", "PowerShell 7")
    argv, cmdline = ps.argv('Write-Output "it\'s 100% & fine"')
    assert cmdline is None and argv[0] == "pwsh" and "-NoProfile" in argv
    script = _decoded(argv)
    assert script.endswith('Write-Output "it\'s 100% & fine"') and "UTF8" in script
    assert "ErrorActionPreference" not in script
    multi = _decoded(ps.argv("mv .env.example .env\r\ndocker compose up -d\n")[0])
    assert "$ErrorActionPreference = 'Stop'" in multi and multi.endswith("mv .env.example .env\ndocker compose up -d")


@pytest.mark.parametrize(("line", "expected"), [
    (r'"C:\Program Files\nodejs\node.EXE" bot.js a b', r'& "C:\Program Files\nodejs\node.EXE" bot.js a b'),
    (r"  'C:\Tools\app.exe'", r"  & 'C:\Tools\app.exe'"),
    ('"./run.cmd" --fast', '& "./run.cmd" --fast'),
    ('& "C:\\x\\y.exe"', '& "C:\\x\\y.exe"'),
    ('"hello world"', '"hello world"'),
    ('"C:\\logs\\a.txt" | Get-Content', '"C:\\logs\\a.txt" | Get-Content'),
    ('"{0}\\{1}" -f $a, $b', '"{0}\\{1}" -f $a, $b'),
    ("npm run dev", "npm run dev"),
])
def test_powershell_runs_a_quoted_program_path(line: str, expected: str) -> None:
    """Commands copied from cmd or from Pythonator's own logs start with a quoted path."""
    script = _decoded(Shell(ConsoleShell.POWERSHELL, "pwsh", "PowerShell 7").argv(line)[0])
    assert script.splitlines()[-1] == expected


def test_cmd_chains_the_lines_of_a_pasted_block() -> None:
    cmd = Shell(ConsoleShell.CMD, r"C:\Windows\System32\cmd.exe", "cmd")
    argv, cmdline = cmd.argv("echo one\n\necho two;\n")
    assert argv[-1] == "(echo one) && (echo two;)"
    assert cmdline == r'"C:\Windows\System32\cmd.exe" /d /s /c "(echo one) && (echo two;)"'
    assert cmd.argv("dir")[0][-1] == "dir"


def test_posix_shells_stop_at_the_first_failing_line() -> None:
    sh = Shell(ConsoleShell.BASH, "/bin/bash", "bash")
    assert sh.argv("ls -la")[0] == ["/bin/bash", "-c", "ls -la"]
    assert sh.argv("a\nb")[0] == ["/bin/bash", "-c", "set -e\na\nb"]


def test_settings_keep_the_console_shell() -> None:
    assert AppSettings().console_shell is ConsoleShell.AUTO
    assert AppSettings.from_dict({"console_shell": "cmd"}).console_shell is ConsoleShell.CMD
    assert AppSettings.from_dict({"console_shell": "fish"}).console_shell is ConsoleShell.AUTO
    assert AppSettings.from_dict(AppSettings(console_shell=ConsoleShell.BASH).to_dict()).console_shell \
        is ConsoleShell.BASH
    assert ConsoleShell.AUTO in shell_choices() and (ConsoleShell.CMD in shell_choices()) == IS_WINDOWS


def test_auto_shell_is_powershell_on_windows_and_bash_elsewhere() -> None:
    shell = resolve_shell(ConsoleShell.AUTO)
    if IS_WINDOWS:
        assert shell.kind is ConsoleShell.POWERSHELL and Path(shell.program).is_file()
    else:
        assert shell.kind is ConsoleShell.BASH and shell.name in ("bash", "sh")


@pytest.mark.parametrize(("output", "windows", "shell", "expected"), [
    (["'docker' is not recognized as an internal or external command,"], True, ConsoleShell.CMD, "Docker Desktop"),
    (["docker : The term 'docker' is not recognized as a name of a cmdlet, function, script file"], True,
     ConsoleShell.POWERSHELL, "Docker Desktop"),
    (["apt : The term 'apt' is not recognized as the name of a cmdlet"], True, ConsoleShell.POWERSHELL,
     "apt is a Linux command"),
    (["Sudo is disabled on this machine. To enable it, go to the Developer Settings page"], True,
     ConsoleShell.CMD, "sudo is a Linux command"),
    (["'mv' is not recognized as an internal or external command,"], True, ConsoleShell.CMD,
     "Switch the console shell to PowerShell"),
    (["bash: line 1: mytool: command not found"], False, ConsoleShell.BASH, "mytool isn't installed"),
    (["/bin/sh: 1: mytool: not found"], False, ConsoleShell.BASH, "mytool isn't installed"),
    (["apt: command not found"], False, ConsoleShell.BASH, "apt isn't installed"),  # e.g. Fedora: no Windows talk
])
def test_hints_for_commands_that_dont_exist(output: list[str], windows: bool, shell: ConsoleShell,
                                            expected: str) -> None:
    hint = command_hint(output, windows=windows, shell=shell)
    assert hint is not None and expected in hint


def test_no_hint_for_ordinary_failures() -> None:
    assert command_hint(["Traceback (most recent call last):", "ValueError: nope"], windows=True) is None


def _supervisor(tmp_path: Path, qapp: object, shell: ConsoleShell = ConsoleShell.AUTO) -> tuple[Supervisor, str]:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    sup.config.settings.console_shell = shell
    script = tmp_path / "bot.py"
    script.write_text("print(1)\n")
    svc = sup.add(ServiceConfig(name="bot", target=str(script), python=sys.executable, workdir=str(tmp_path)))
    return sup, svc.id


def _run(sup: Supervisor, sid: str, command: str) -> str:
    svc = sup.get(sid)
    assert svc is not None and sup.run_shell(sid, command)
    assert wait_until(lambda: svc.task is None, timeout=60, qt=True)
    return "\n".join(ln.text for ln in svc.log.snapshot())


def _shells() -> list[ConsoleShell]:
    found = [ConsoleShell.AUTO]
    if shutil.which("pwsh") or IS_WINDOWS:
        found.append(ConsoleShell.POWERSHELL)
    if IS_WINDOWS:
        found.append(ConsoleShell.CMD)
    return found


@pytest.mark.parametrize("shell", _shells())
def test_real_shell_runs_a_pasted_block_and_stops_at_the_first_failure(
        shell: ConsoleShell, tmp_path: Path, qapp: object) -> None:
    sup, sid = _supervisor(tmp_path, qapp, shell)
    text = _run(sup, sid, "echo first-line\necho second-line")
    lines = [ln.strip() for ln in text.splitlines()]
    assert "first-line" in lines and "second-line" in lines and "done in" in text
    text = _run(sup, sid, "echo before-failure\nno-such-tool-xyz\necho after-failure")
    lines = [ln.strip() for ln in text.splitlines()]
    assert "before-failure" in lines and "after-failure" not in lines, text
    assert "failed" in text and "💡 no-such-tool-xyz isn't installed" in text, text
    assert "CLIXML" not in text and "\x1b[" not in text, text
    sup.shutdown()


@pytest.mark.parametrize("shell", _shells())
def test_real_shell_runs_a_quoted_program_path(shell: ConsoleShell, tmp_path: Path, qapp: object) -> None:
    """`"C:\\Program Files\\...\\python.exe" script.py` works in every shell, PowerShell included."""
    sup, sid = _supervisor(tmp_path, qapp, shell)
    quote = '"' if IS_WINDOWS else "'"
    (tmp_path / "say.py").write_text("import sys\nprint('said', *sys.argv[1:])\n")
    text = _run(sup, sid, f"{quote}{sys.executable}{quote} {quote}{tmp_path / 'say.py'}{quote} a b")
    assert "said a b" in text and "done in" in text, text
    sup.shutdown()


@pytest.mark.parametrize("shell", [s for s in _shells() if s is not ConsoleShell.CMD])
def test_real_shell_mv_works(shell: ConsoleShell, tmp_path: Path, qapp: object) -> None:
    """The user's case: `mv .env.example .env` from a README. PowerShell and bash both have mv."""
    (tmp_path / ".env.example").write_text("A=1\n")
    sup, sid = _supervisor(tmp_path, qapp, shell)
    text = _run(sup, sid, "mv .env.example .env")
    assert (tmp_path / ".env").read_text() == "A=1\n" and not (tmp_path / ".env.example").exists(), text
    sup.shutdown()
