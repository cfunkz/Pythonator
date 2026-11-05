"""Service logs: every line goes to a rotating file, the newest part stays in memory for the window.

Secrets a service was given (variables named like ``*TOKEN*``, ``*KEY*``, ``*SECRET*``, ``*PASSWORD*``) are masked
before anything is written or shown, so a bot that prints its token doesn't leak it into a log or a plugin.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO

from ..paths import make_private

SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|_KEY$|^KEY$", re.IGNORECASE)
MASK = "••••••"


def secrets_of(env: dict[str, str]) -> list[str]:
    return sorted({v for k, v in env.items() if SECRET_NAME.search(k) and len(v) >= 6}, key=len, reverse=True)


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
