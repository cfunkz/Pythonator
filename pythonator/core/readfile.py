"""Reading a file a service may have written, from outside its sandbox.

A sandboxed program can put anything at a path in its own folder: a FIFO (``mkfifo package.json``) that blocks
whoever opens it until something writes to it, a link to ``/dev/zero`` or a file of many gigabytes. Pythonator reads
some of those files itself (``package.json``, ``pyproject.toml``, the env file, ``pyvenv.cfg``) and so do plugins
through it, so each read here takes only a regular file, never waits, and stops at a size.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

MAX_PROJECT_FILE = 1024 * 1024  # a package.json, pyproject.toml or .env file is a few KB


def read_regular(path: Path | str, limit: int = MAX_PROJECT_FILE, *, head: bool = False) -> bytes:
    """The bytes of the regular file at ``path``, if it holds at most ``limit`` bytes; else OSError. With ``head``,
    a bigger file gives its first ``limit + 1`` bytes instead (so the caller can tell it was cut)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):  # a FIFO, socket or device: opened without waiting, refused here
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        chunks, total = [], 0
        while total <= limit:  # the size it says may be wrong (/proc) or change while it is read
            data = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not data:
                break
            chunks.append(data)
            total += len(data)
        if total > limit and not head:
            raise OSError(errno.EFBIG, f"larger than {limit // 1024} KB", str(path))
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_text(path: Path | str, limit: int = MAX_PROJECT_FILE, *, encoding: str = "utf-8") -> str:
    """:func:`read_regular`, decoded (UnicodeDecodeError, a ValueError, for text that isn't)."""
    return read_regular(path, limit).decode(encoding)
