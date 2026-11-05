"""Strict checks for everything a user, a file or a plugin hands to Pythonator.

Each function returns the cleaned value or raises :class:`ValueError` with a message that can be shown as is.
Nothing here builds a shell command: Pythonator only ever starts programs from argument lists, so these
checks stop mistakes and smuggled options (a ``-`` at the start of a git URL, a NUL in a path) rather than
shell injection, which can't happen.
"""

from __future__ import annotations

import re
import shlex
import unicodedata
from pathlib import Path

from ..paths import IS_WINDOWS

MAX_NAME = 64
MAX_ARG = 8192
MAX_ARGS = 256
MAX_ENV = 256
MAX_ENV_VALUE = 32_768
MAX_PORTS = 32

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_GIT_URL = re.compile(
    r"(?:https://[A-Za-z0-9.-]+(?::\d{1,5})?/[A-Za-z0-9._~%/+-]+"  # https://host[:port]/path
    r"|ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(?::\d{1,5})?/[A-Za-z0-9._~%/+-]+"  # ssh://user@host/path
    r"|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~%/+-]+)"  # git@host:owner/repo(.git)
)
_GIT_REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")
_IMAGE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
                    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?")
_NET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
# Variables that change how every program loads code: refused in a service's environment, because a
# sandbox would still apply them to the runtime itself and they are a common way to hijack a process.
DANGEROUS_ENV = frozenset({"LD_PRELOAD", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
                           "DYLD_FRAMEWORK_PATH", "PYTHONINSPECT", "NODE_OPTIONS_PRELOAD"})


def _no_controls(text: str, what: str, *, newlines: bool = False) -> str:
    for ch in text:
        if ch == "\x00":
            raise ValueError(f"{what} contains a NUL character")
        if unicodedata.category(ch) in ("Cc", "Cf") and not (newlines and ch in "\n\t\r"):
            raise ValueError(f"{what} contains an invisible control character (U+{ord(ch):04X})")
    return text


def name(value: str) -> str:
    """A display name: 1 to 64 printable characters, letters, digits, spaces and ``. _ - ( )``."""
    value = _no_controls(value.strip(), "The name")
    if not value:
        raise ValueError("Give it a name")
    if len(value) > MAX_NAME:
        raise ValueError(f"Names are at most {MAX_NAME} characters")
    if not all(ch.isalnum() or ch in " ._-()" for ch in value):
        raise ValueError("Use letters, digits, spaces and . _ - ( ) in names")
    return value


def ident(value: str, what: str = "id") -> str:
    """A machine id: lower-case letters, digits and dashes."""
    if not _ID.fullmatch(value):
        raise ValueError(f"Not a valid {what}: use lower-case letters, digits and dashes")
    return value


def env_key(value: str) -> str:
    if not _ENV_KEY.fullmatch(value):
        raise ValueError(f"Not a variable name: {value!r} (letters, digits and _, not starting with a digit)")
    if value.upper() in DANGEROUS_ENV:
        raise ValueError(f"{value} changes how programs load code and isn't allowed")
    return value


def env(values: dict[str, str]) -> dict[str, str]:
    if len(values) > MAX_ENV:
        raise ValueError(f"At most {MAX_ENV} variables")
    out: dict[str, str] = {}
    for key, val in values.items():
        if len(val) > MAX_ENV_VALUE:
            raise ValueError(f"The value of {key} is too long")
        out[env_key(key)] = _no_controls(val, f"The value of {key}")
    return out


def arg(value: str) -> str:
    if len(value) > MAX_ARG:
        raise ValueError("An argument is too long")
    return _no_controls(value, "An argument")


def args(values: list[str]) -> list[str]:
    if len(values) > MAX_ARGS:
        raise ValueError(f"At most {MAX_ARGS} arguments")
    return [arg(v) for v in values]


def split_command(text: str) -> list[str]:
    """``python -m http.server 8000`` -> ``["python", "-m", "http.server", "8000"]``. Quotes group words."""
    _no_controls(text, "The command")
    try:
        parts = shlex.split(text, posix=not IS_WINDOWS)
    except ValueError as exc:
        raise ValueError(f"Can't read the command: {exc}") from None
    if IS_WINDOWS:  # posix=False keeps the quotes; Windows paths keep their backslashes
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return args(parts)


def join_command(parts: list[str]) -> str:
    if IS_WINDOWS:
        return " ".join(f'"{p}"' if (" " in p or not p) else p for p in parts)
    return shlex.join(parts)


def port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError(f"Not a port: {value} (1 to 65535)")
    return value


def ports(values: list[int]) -> list[int]:
    out: list[int] = []
    for v in values:
        if port(v) not in out:
            out.append(v)
    if len(out) > MAX_PORTS:
        raise ValueError(f"At most {MAX_PORTS} ports")
    return out


def parse_ports(text: str) -> list[int]:
    """``"8000, 8443"`` -> ``[8000, 8443]``."""
    out: list[int] = []
    for item in re.split(r"[\s,;]+", text.strip()):
        if not item:
            continue
        if not item.isdigit():
            raise ValueError(f"Not a port: {item!r}")
        out.append(int(item))
    return ports(out)


def folder(value: str, what: str = "The folder", *, must_exist: bool = True) -> str:
    """An absolute folder path, normalised. ``~`` is expanded."""
    _no_controls(value, what)
    if not value.strip():
        raise ValueError(f"{what} is empty")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{what} must be a full path")
    if must_exist and not path.is_dir():
        raise ValueError(f"{what} doesn't exist: {path}")
    return str(path.resolve() if must_exist else path)


def relative_file(value: str, base: str) -> str:
    """A file inside ``base`` (no ``..`` escapes, no absolute paths elsewhere)."""
    _no_controls(value, "The file")
    value = value.strip()
    if not value:
        return ""
    root = Path(base).resolve()
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError("The file must be inside the service's folder")
    return target.relative_to(root).as_posix()


def git_url(value: str) -> str:
    value = value.strip()
    if value.startswith("-") or not _GIT_URL.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("Use an https:// or SSH address, e.g. https://github.com/owner/repo.git")
    return value


def git_ref(value: str) -> str:
    value = value.strip()
    if value and (value.startswith(("-", "/")) or ".." in value or not _GIT_REF.fullmatch(value)):
        raise ValueError("Not a branch or tag name")
    return value


def image(value: str) -> str:
    value = value.strip()
    if value and (len(value) > 255 or not _IMAGE.fullmatch(value)):
        raise ValueError("Not a container image name, e.g. python:3.13-slim")
    return value


def network_name(value: str) -> str:
    value = value.strip()
    if value and not _NET_NAME.fullmatch(value):
        raise ValueError("Not a container network name")
    if value in ("host",):
        raise ValueError("The host network would turn the container's network isolation off")
    return value
