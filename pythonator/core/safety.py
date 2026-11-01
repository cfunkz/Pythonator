"""Guard rails for running other people's code on this computer.

Pythonator runs whatever its services point at, with the user's own rights; it can't sandbox
that. What it can do is keep its own files private, refuse to act on settings someone else could
have changed, and ask before things that are easy to regret: running everything as
root/Administrator, a shell command that wipes a disk, or installing a project's packages.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"


# -- elevated rights ------------------------------------------------------------------------------


def elevation() -> str | None:
    """"root" / "Administrator" when Pythonator itself runs with full control of the machine."""
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            return "Administrator" if ctypes.windll.shell32.IsUserAnAdmin() else None
        except (AttributeError, OSError):
            return None
    geteuid = getattr(os, "geteuid", None)
    return "root" if geteuid is not None and geteuid() == 0 else None


# -- private files --------------------------------------------------------------------------------


def make_private(path: Path) -> None:
    """Owner-only access (0700 folders, 0600 files) on POSIX: config and logs can hold tokens.

    Windows already keeps %APPDATA% per user, so nothing is changed there.
    """
    if IS_WINDOWS:
        return
    with contextlib.suppress(OSError):
        path.chmod(0o700 if path.is_dir() else 0o600)


def open_private_append(path: Path) -> int:
    """A descriptor appending to ``path``; a new file is created readable by its owner only."""
    # O_BINARY: without it the Windows C runtime would turn every \n into \r\n behind Python's back.
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)


def unsafe_reason(path: Path) -> str | None:
    """Why another local account could have changed ``path`` (None if it can't).

    Settings decide which programs run, so a settings file another user can write to is a way
    to run their code as you. Only POSIX permissions are checked; Windows profiles are per user.
    """
    if IS_WINDOWS or not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_uid not in (os.geteuid(), 0):
        return f"{path} belongs to another user (uid {st.st_uid})"
    if st.st_mode & stat.S_IWOTH and not (path.is_dir() and st.st_mode & stat.S_ISVTX):
        return f"{path} can be changed by every user on this computer"
    return None


def trusted_dir(path: Path) -> bool:
    """False for a folder another (non-root) user owns or anyone may write to, e.g. under /tmp."""
    return unsafe_reason(path) is None


# -- risky shell commands -------------------------------------------------------------------------

_CMD = r"(?:^|[;&|(]\s*|\b(?:sudo|doas)\s+)\s*"  # where a command name starts
_ANY = r"[^;&|\n]*"  # the rest of the same command, not the next one in a chain
_END = r"[\"']?(?:\s|$|[;&|)])"
# Targets that mean "everything": / , ~ , $HOME, * , .. , top-level system folders (Linux and macOS);
# C:\ , %USERPROFILE%.
_SYSTEM_DIRS = r"bin|boot|dev|etc|home|lib\w*|opt|root|sbin|usr|var|Applications|Library|System|Users|Volumes|private"
_POSIX_ALL = rf"(?:/\*?|~/?\*?|\$\{{?HOME\}}?/?\*?|\*|\.\./?\*?|/(?:{_SYSTEM_DIRS})/?\*?)"
_WIN_ALL = r"(?:[A-Za-z]:\\?(?:\*(?:\.\*)?)?|\\|%\w+%\\?|\*(?:\.\*)?|\.\.\\?)"
_EVERYTHING = rf"\s[\"']?(?:{_POSIX_ALL}|{_WIN_ALL}){_END}"

_RISKY: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (rf"{_CMD}rm\s{_ANY}(?:-\w*[rR]\w*|--recursive)\b{_ANY}{_EVERYTHING}",
         "permanently deletes a whole folder tree (/ , your home folder, or everything here with *)"),
        (rf"{_CMD}(?:rd|rmdir)\s{_ANY}/s\b{_ANY}{_EVERYTHING}", "permanently deletes a drive or a whole folder tree"),
        (rf"{_CMD}(?:del|erase)\s{_ANY}/s\b", "permanently deletes files in every subfolder"),
        (rf"{_CMD}(?:Remove-Item|ri|rm)\s{_ANY}-Recurse\b{_ANY}{_EVERYTHING}",
         "permanently deletes a drive or a whole folder tree"),
        (rf"{_CMD}(?:mkfs(?:\.\w+)?|wipefs|fdisk|sfdisk|parted|diskpart|Format-Volume|Clear-Disk)\b",
         "can erase or repartition a disk"),
        (rf"{_CMD}format\s+[A-Za-z]:", "erases a drive"),
        (rf"{_CMD}diskutil\s+(?:apfs\s+)?(?:erase\w*|zeroDisk|randomDisk|secureErase|reformat|partitionDisk"
         r"|deleteContainer|deleteVolume)\b", "can erase or repartition a disk"),
        (rf"{_CMD}dd\s{_ANY}\bof=/dev/(?!null\b|zero\b)", "writes raw data straight onto a device"),
        (r">\s*/dev/(?:sd|nvme|hd|vd|xvd|mmcblk|r?disk)\w*", "overwrites a disk device"),
        (rf"{_CMD}(?:shutdown|reboot|poweroff|halt|Stop-Computer|Restart-Computer|init\s+[06])\b",
         "shuts down or restarts this computer"),
        (rf"{_CMD}(?:chmod|chown|chgrp)\s{_ANY}-\w*R\w*\b{_ANY}{_EVERYTHING}",
         "changes permissions or ownership of system or home files, recursively"),
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "is a fork bomb that freezes the computer"),
        (r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
         "downloads a script from the internet and runs it straight away"),
        (r"\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n|]*\|\s*(?:iex|Invoke-Expression)\b",
         "downloads a script from the internet and runs it straight away"),
        (rf"{_CMD}reg\s+delete\b", "deletes Windows registry entries"),
        (rf"{_CMD}(?:bcdedit|vssadmin\s+delete|wbadmin\s+delete)\b", "changes boot settings or deletes backups"),
        (rf"{_CMD}git\s+clean\s{_ANY}-\w*x", "permanently deletes untracked and ignored files (.env, data, venvs)"),
    )
)


def risky_command(command: str) -> str | None:
    """Why ``command`` could damage this computer or lose data, or None if it looks ordinary.

    A short list of well-known destructive patterns, so a typo or a pasted one-liner gets a
    second look. It is a seat belt, not a sandbox: anything not listed runs without a question.
    """
    for pattern, reason in _RISKY:
        if pattern.search(command):
            return reason
    return None


# -- secrets typed into a running program -----------------------------------------------------------

_SECRET_PROMPT = re.compile(
    r"\b(?:pass(?:word|phrase|code)?|token|secret|api[\s_-]?key|private[\s_-]?key|pin|otp|2fa|credentials?)\b"
    r"[^\n]{0,24}[:>?]$",
    re.IGNORECASE,
)


def is_secret_prompt(text: str) -> bool:
    """True when a program's last output line asks for a password, token, API key, ..."""
    return bool(_SECRET_PROMPT.search(text.rstrip()[-200:]))
