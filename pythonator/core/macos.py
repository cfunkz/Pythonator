"""macOS specifics that need no extra packages: App Nap, .app bundles.

App Nap throttles the timers and threads of an app whose windows are hidden or covered, which for
a supervisor means late restarts, late scheduled runs and console output read in bursts. Pythonator
tells the system it is doing user-requested work, the same way ``caffeinate``-style tools do, via
``NSProcessInfo`` called through the Objective-C runtime with ctypes (no PyObjC needed). The Mac
can still go to sleep as usual.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import plistlib
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"

# NSActivityUserInitiatedAllowingIdleSystemSleep: no App Nap or timer throttling, sleep still allowed.
_ACTIVITY_OPTIONS = 0x00FFFFFF & ~(1 << 20)
_activity: int | None = None  # the retained activity token; App Nap stays off while it is alive


def _objc() -> tuple[Any, Any, Any]:
    objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    # objc_msgSend must be called through a prototype that matches each method (arm64 ABI).
    send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    return objc.objc_getClass, objc.sel_registerName, send


def disable_app_nap(reason: str = "Supervising long-running services") -> bool:
    """Keep macOS from napping Pythonator while it runs. True if the system accepted it."""
    global _activity
    if not IS_MAC:
        return False
    if _activity:
        return True
    try:
        get_class, sel, send = _objc()
        msg = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        msg_str = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(send)
        msg_begin = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
        )(send)
        info = msg(get_class(b"NSProcessInfo"), sel(b"processInfo"))
        text = msg_str(get_class(b"NSString"), sel(b"stringWithUTF8String:"), reason.encode())
        token = msg_begin(info, sel(b"beginActivityWithOptions:reason:"), _ACTIVITY_OPTIONS, text)
        if not token:
            return False
        _activity = msg(token, sel(b"retain"))
    except (OSError, AttributeError, TypeError, ValueError):
        log.warning("Could not turn off App Nap; services may be throttled while the window is hidden",
                    exc_info=True)
        return False
    return bool(_activity)


def bundle_executable(app: Path) -> Path | None:
    """The program inside a ``.app`` bundle (``Contents/MacOS/<CFBundleExecutable>``)."""
    contents = app / "Contents"
    name = ""
    with contextlib.suppress(OSError, ValueError, plistlib.InvalidFileException):
        with (contents / "Info.plist").open("rb") as fh:
            data = plistlib.load(fh)
        if isinstance(data, dict):
            name = str(data.get("CFBundleExecutable") or "")
    candidates = [contents / "MacOS" / name] if name else []
    candidates.append(contents / "MacOS" / app.stem)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    with contextlib.suppress(OSError):
        programs = sorted(p for p in (contents / "MacOS").iterdir() if p.is_file())
        if len(programs) == 1:
            return programs[0]
    return None

