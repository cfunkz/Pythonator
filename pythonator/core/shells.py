"""The shell behind the console's ``$`` mode, and hints when a command there isn't found.

Windows uses PowerShell by default: it has ``mv``, ``ls``, ``rm``, ``cp`` and ``cat`` like the
commands people copy from READMEs, where cmd.exe has none of them. A command pasted as several
lines runs as one script that stops at the first failing line, in every shell.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import LaunchError
from .models import ConsoleShell, Runtime
from .paths import IS_WINDOWS
from .runtimes import find_program, runtime_info

SHELL_LABELS = {
    ConsoleShell.AUTO: "Automatic",
    ConsoleShell.POWERSHELL: "PowerShell",
    ConsoleShell.CMD: "Command Prompt (cmd)",
    ConsoleShell.BASH: "Git Bash" if IS_WINDOWS else "bash",
}


def shell_choices() -> list[ConsoleShell]:
    """The shells that make sense on this platform, for the settings dialog."""
    if IS_WINDOWS:
        return [ConsoleShell.AUTO, ConsoleShell.POWERSHELL, ConsoleShell.CMD, ConsoleShell.BASH]
    return [ConsoleShell.AUTO, ConsoleShell.BASH, ConsoleShell.POWERSHELL]


@dataclass(frozen=True, slots=True)
class Shell:
    kind: ConsoleShell  # never AUTO
    program: str
    name: str  # for the console, e.g. "PowerShell 7"

    def argv(self, command: str) -> tuple[list[str], str | None]:
        """argv for running ``command``, plus the exact Windows command line where one is needed."""
        command = command.replace("\r\n", "\n").strip("\n")
        multiline = "\n" in command
        if self.kind is ConsoleShell.POWERSHELL:
            prelude = (
                "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $OutputEncoding = [Text.Encoding]::UTF8; "
                "$ProgressPreference = 'SilentlyContinue'; "
                "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\n"
            )
            if multiline:  # stop at the first failing line, native programs included (PowerShell 7.3+)
                prelude += "$ErrorActionPreference = 'Stop'; $PSNativeCommandUseErrorActionPreference = $true\n"
            script = prelude + "\n".join(_call_operator(ln) for ln in command.split("\n"))
            # -Command (not -EncodedCommand, which makes PowerShell print errors as CLIXML) with
            # -OutputFormat Text: errors come out as readable text.
            return [self.program, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-OutputFormat", "Text", "-Command", script], None
        if self.kind is ConsoleShell.CMD:
            # cmd /c runs only the first line of a multi-line command: chain the lines instead.
            lines = [ln.strip() for ln in command.split("\n") if ln.strip()]
            joined = " && ".join(f"({ln})" if multiline else ln for ln in lines)
            argv = [self.program, "/d", "/s", "/c", joined]
            # cmd.exe has its own quoting rules: wrap the whole command once and let /s unwrap it.
            return argv, f'"{self.program}" /d /s /c "{joined}"'
        script = f"set -e\n{command}" if multiline else command
        return [self.program, "-c", script], None


_QUOTED_START = re.compile(r"""^(?P<indent>\s*)(?P<q>["'])(?P<target>[^"']*)(?P=q)(?=\s|$)""")
_PROGRAM_LIKE = re.compile(r"[\\/]|\.(?:exe|cmd|bat|com|ps1)$", re.IGNORECASE)
_OPERATOR_NEXT = re.compile(r"^(?:[|>+=,;.]|-(?:f|eq|ne|gt|lt|ge|le|like|match|replace|join|split)\b)",
                            re.IGNORECASE)


def _call_operator(line: str) -> str:
    """``"C:\\Program Files\\nodejs\\node.exe" app.js`` runs the program in cmd and bash, but PowerShell
    reads a line that starts with a quoted string as a string. Add PowerShell's ``&`` so it runs."""
    match = _QUOTED_START.match(line)
    if not match or not _PROGRAM_LIKE.search(match["target"]):
        return line
    if _OPERATOR_NEXT.match(line[match.end():].strip()):
        return line
    return f"{match['indent']}& {line[len(match['indent']):]}"

def _windows_powershell() -> str | None:
    path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                "powershell.exe")
    return str(path) if path.is_file() else shutil.which("powershell")


def _pwsh() -> str | None:
    try:
        found = find_program(runtime_info(Runtime.POWERSHELL), "", dict(os.environ))
    except LaunchError:
        return None
    return found if Path(found).stem.lower() == "pwsh" else None


def _comspec() -> str:
    if comspec := os.environ.get("COMSPEC"):
        return comspec
    return str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "cmd.exe"))


def resolve_shell(choice: ConsoleShell) -> Shell:
    """The shell to run console commands with. Programs come from Pythonator's own environment."""
    if choice is ConsoleShell.POWERSHELL or (choice is ConsoleShell.AUTO and IS_WINDOWS):
        if pwsh := _pwsh():
            return Shell(ConsoleShell.POWERSHELL, pwsh, "PowerShell 7")
        if IS_WINDOWS and (legacy := _windows_powershell()):
            return Shell(ConsoleShell.POWERSHELL, legacy, "Windows PowerShell")
        if choice is ConsoleShell.POWERSHELL:
            raise LaunchError("PowerShell was not found. Install it from https://aka.ms/powershell, "
                              "or pick another console shell in Settings.")
    if IS_WINDOWS and choice is ConsoleShell.BASH:
        try:
            return Shell(ConsoleShell.BASH, find_program(runtime_info(Runtime.SHELL), "", dict(os.environ)),
                         "Git Bash")
        except LaunchError:
            raise LaunchError("Git Bash was not found. Install Git for Windows (https://git-scm.com/downloads), "
                              "or pick another console shell in Settings.") from None
    if IS_WINDOWS:
        return Shell(ConsoleShell.CMD, _comspec(), "cmd")
    bash = shutil.which("bash")
    return Shell(ConsoleShell.BASH, bash or "/bin/sh", "bash" if bash else "sh")


# -- "command not found" hints -------------------------------------------------------------------

_NOT_FOUND = (
    re.compile(r"'(?P<name>[^']+)' is not recognized as an internal or external command"),  # cmd
    re.compile(r"The term '(?P<name>[^']+)' is not recognized as"),  # PowerShell
    re.compile(r"(?:^|: )(?P<name>[\w.+-]+): command not found"),  # bash
    re.compile(r"^\S*sh: \d+: (?P<name>[\w.+-]+): not found"),  # dash / sh
)
_WINDOWS_SUDO = "Sudo is disabled on this machine"
LINUX_ONLY = frozenset({
    "apt", "apt-get", "apt-cache", "dpkg", "yum", "dnf", "rpm", "pacman", "zypper", "apk", "snap", "flatpak",
    "systemctl", "service", "journalctl", "sudo", "su", "chmod", "chown", "useradd", "update-alternatives",
})
UNIX_TOOLS = frozenset({"mv", "ls", "rm", "cp", "cat", "touch", "grep", "which", "pwd", "head", "tail", "export"})
_INSTALL = {
    "docker": "Docker isn't installed, or isn't on PATH. Install Docker Desktop "
              "(https://docs.docker.com/desktop/), start it once, then restart Pythonator.",
    "git": "Git isn't installed, or isn't on PATH. Install it from https://git-scm.com/downloads.",
    "brew": "Homebrew is for macOS and Linux.",
}


def command_hint(output: list[str], *, windows: bool = IS_WINDOWS, shell: ConsoleShell | None = None) -> str | None:
    """A hint for a console command that failed because a program doesn't exist here."""
    name = ""
    for line in output:
        if windows and _WINDOWS_SUDO in line:
            name = "sudo"
            break
        for pattern in _NOT_FOUND:
            if m := pattern.search(line.strip()):
                name = m.group("name")
                break
        if name:
            break
    if not name:
        return None
    base = name.lower().removesuffix(".exe")
    if windows and base in LINUX_ONLY:
        return (f"{name} is a Linux command, and this console runs on Windows. Copy the Windows install steps "
                "instead (often a winget command or an installer), or run Linux commands inside WSL.")
    if base in _INSTALL:
        return _INSTALL[base]
    if windows and shell is ConsoleShell.CMD and base in UNIX_TOOLS:
        return (f"cmd.exe has no {name}. Switch the console shell to PowerShell in Settings, where mv, ls, rm, cp "
                "and cat work.")
    return (f"{name} isn't installed, or isn't on PATH. If you installed it after starting Pythonator, "
            "restart Pythonator so it sees the new PATH.")
