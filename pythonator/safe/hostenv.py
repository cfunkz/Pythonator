"""Environments and program paths for host tools (git, podman, docker, systemd-run, pasta).
Host tools never inherit Pythonator's environment and are never found by bare name on PATH: a service folder can't
plant a `git` that runs as you, and a variable like GIT_SSH_COMMAND or LD_PRELOAD can't ride along."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

TRUSTED_DIRS_POSIX = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/snap/bin")
KEEP = (
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "USER", "LOGNAME",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
    "PROGRAMDATA",
)

def trusted_dirs() -> tuple[str, ...]:
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return (
            rf"{root}\System32", rf"{pf}\Docker\Docker\resources\bin", rf"{pf}\RedHat\Podman", rf"{pf}\Git\mingw64\bin", rf"{pf}\Git\cmd",
            rf"{pf}\nodejs",
        )
    return TRUSTED_DIRS_POSIX

def _safe_file(p: Path) -> bool:
    try:
        st = p.stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if sys.platform != "win32":
        if st.st_mode & 0o022:  # writable by group or others
            return False
        if st.st_uid not in (0, os.getuid()):
            return False
        return os.access(p, os.X_OK)
    return True

def resolve(name: str) -> str:
    """Absolute path of a host tool from trusted folders only, or '' when it isn't installed there."""
    exts = ("",) if sys.platform != "win32" else (".exe", ".cmd", ".bat")
    for d in trusted_dirs():
        for ext in exts:
            p = Path(d, name + ext)
            if _safe_file(p):
                return str(p)
    return ""

def host_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k in KEEP if (v := os.environ.get(k)) is not None}
    env["PATH"] = os.pathsep.join(trusted_dirs())
    env.setdefault("LANG", "C.UTF-8")
    if extra:
        env.update(extra)
    return env
