"""One of each per data folder: one background service, one window. A lock the OS holds for the process (released
when it ends, even when it crashes) decides; a second window wakes the first instead of opening.

Two windows each showed their own question when the Project builder asked to delete a project, and two
background services ran every service and plugin twice: both came from Pythonator started again while it was
already running (in the tray, or at sign-in). Standard library only.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import IO


class InstanceLock:
    """An exclusive lock on a file, held until :meth:`release` or the process ends."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another one does."""
        if self._file is not None:
            return True
        try:
            f = self.path.open("a+b")  # held open for the process's life
        except OSError:
            return True  # can't even make the file: don't stop Pythonator over it
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        with contextlib.suppress(OSError):
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()).encode())
            f.flush()
        self._file = f
        return True

    def release(self) -> None:
        f, self._file = self._file, None
        if f is None:
            return
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
