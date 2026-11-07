"""Unexpected errors don't end the program.

PyQt's default for an exception no code caught, in a slot or in a method Qt calls (a model, an icon provider, a
paint), is ``qFatal()``: the whole window, or the whole background service and with it every service on Windows,
ends at once. One bug in one panel must not do that. The error is written to the log instead, and the window
shows a line about it.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from types import TracebackType

log = logging.getLogger("pythonator")
_notify: list[Callable[[str], None]] = []


def keep_running(notify: Callable[[str], None] | None = None) -> None:
    """Log what nothing caught, and carry on. ``notify`` gets one line about it (the window's status bar)."""
    if notify is not None:
        _notify[:] = [notify]
    sys.excepthook = _hook
    threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)


def _hook(kind: type[BaseException], exc: BaseException | None, tb: TracebackType | None) -> None:
    if issubclass(kind, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(kind, exc or kind(), tb)
        return
    try:
        log.error("Unexpected error (the program goes on)", exc_info=(kind, exc or kind(), tb))
        for notify in _notify:
            notify(f"Something went wrong ({kind.__name__}: {str(exc)[:200]}). It is in the log; nothing was closed.")
    except Exception:  # noqa: S110 - the hook itself must never raise into Qt
        pass
