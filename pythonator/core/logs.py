"""Service logs: every line goes to a rotating file, the newest part stays in memory for the window.

Secrets a service was given (variables named like ``*TOKEN*``, ``*KEY*``, ``*SECRET*``, ``*PASSWORD*``) are masked
before anything is written or shown, so a bot that prints its token doesn't leak it into a log or a plugin. What
goes on to plugins (and through them to an AI provider or a webhook) is masked once more: every value of the
service's own variables and env file, whatever its name (:func:`plugin_secrets`).
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

from ..paths import make_private

SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|_KEY$|^KEY$", re.IGNORECASE)
MASK = "••••••"


def secrets_of(env: dict[str, str]) -> list[str]:
    return sorted({v for k, v in env.items() if SECRET_NAME.search(k) and len(v) >= 6}, key=len, reverse=True)


# Values that are settings, not secrets: masking them would only make the output hard to read.
PLAIN_VALUES = frozenset({
    "true", "false", "yes", "no", "on", "off", "none", "null", "localhost", "development", "production", "staging",
    "testing", "debug", "info", "warning", "error", "critical", "verbose", "enabled", "disabled",
})


def plugin_secrets(values: dict[str, str], ports: tuple[int, ...] | list[int] = ()) -> list[str]:
    """Every value of a service's variables and env file worth hiding from plugins, whatever its name: 6 or more
    characters, not a number, an address, a port or a plain setting such as ``production``. The password inside a
    URL (``postgres://app:password@db/app``) counts on its own too. The same set ServiceFiles blanks in file
    reads (plus the URL passwords). Longest first, so a value that contains another is masked whole."""
    found: set[str] = set()
    skip = {str(p) for p in ports}
    for raw in values.values():
        value = raw.strip()
        if "://" in value:
            with contextlib.suppress(ValueError):
                if (password := urlsplit(value).password) and len(password) >= 6:
                    found.update({password, unquote(password)})
        if len(value) < 6 or value in skip or value.lower() in PLAIN_VALUES or _is_number_or_address(value):
            continue
        found.add(value)
    return sorted(found, key=len, reverse=True)


def _is_number_or_address(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        pass
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class Masker:
    def __init__(self, secrets: list[str]) -> None:
        self._pattern = re.compile("|".join(map(re.escape, secrets))) if secrets else None

    def __call__(self, text: str) -> str:
        return self._pattern.sub(MASK, text) if self._pattern else text


class ServiceLog:
    """One service's log file and in-memory tail."""

    def __init__(self, path: Path, *, max_bytes: int, backups: int, keep_chars: int = 256 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.keep_chars = keep_chars
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._fh: BinaryIO | None = None
        self.mask = Masker([])
        self.plugin_mask = Masker([])  # on top of ``mask``, for what plugins get (plugin_secrets)

    def _open(self) -> BinaryIO:
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("ab")
            make_private(self.path)
        return self._fh

    def write(self, text: str) -> str:
        """Record ``text`` (already decoded); returns it masked, as it should be shown."""
        text = self.mask(text)
        self._tail.append(text)
        self._tail_len += len(text)
        while self._tail_len > self.keep_chars and len(self._tail) > 1:
            self._tail_len -= len(self._tail.popleft())
        with contextlib.suppress(OSError):
            fh = self._open()
            fh.write(text.encode("utf-8", "replace"))
            if fh.tell() > self.max_bytes:
                self._rotate()
        return text

    def note(self, text: str) -> str:
        """A line from Pythonator itself, marked with the time."""
        return self.write(f"\x1b[2m[{time.strftime('%H:%M:%S')}] {text}\x1b[0m\n")

    def flush(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.flush()

    def tail(self) -> str:
        return "".join(self._tail)

    def clear(self) -> None:
        self._tail.clear()
        self._tail_len = 0

    def _rotate(self) -> None:
        self.close()
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            if src.exists():
                with contextlib.suppress(OSError):
                    src.replace(self.path.with_name(f"{self.path.name}.{i}"))
        if self.backups == 0:
            with contextlib.suppress(OSError):
                self.path.unlink()

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None
