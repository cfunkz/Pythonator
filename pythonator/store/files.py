"""Owner-only files, written atomically. Every file Pythonator keeps goes through here."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import platformdirs

APP = "Pythonator"

def data_dir() -> Path:
    override = os.environ.get("PYTHONATOR_DATA")
    p = Path(override) if override else Path(platformdirs.user_data_dir(APP, appauthor=False))
    ensure_private_dir(p)
    return p

def ensure_private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        st = p.stat()
        if st.st_uid != os.getuid():
            raise PermissionError(f"{p} is owned by someone else")
        if st.st_mode & 0o077:
            os.chmod(p, 0o700)
    return p

def write_atomic(path: Path, data: bytes, *, backup: bool = False) -> None:
    ensure_private_dir(path.parent)
    if backup and path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            os.replace(path, bak)
            # restore immediately as a copy so `path` is never missing; .bak keeps the previous version
            with open(bak, "rb") as src:
                prev = src.read()
            _write(path, prev)
        except OSError:
            pass
    _write(path, data)

def _write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def append_line(path: Path, rec: dict[str, Any], sync: bool) -> None:
    """One JSON line onto an append-only, owner-only file (the ledger fsyncs: it must survive a crash)."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode())
        if sync:
            os.fsync(fd)
    finally:
        os.close(fd)

def read_json(path: Path, default: Any = None) -> Any:
    """A missing, unreadable or corrupt file (torn write, NaN, a hand edit) reads as the default; the .bak and the
    file itself stay for you to look at. Infinity and NaN are refused, not read as numbers."""
    def no_constants(name: str) -> Any:
        raise ValueError(f"{name} isn't allowed")

    try:
        with open(path, "rb") as f:
            return json.loads(f.read(4 * 1024 * 1024), parse_constant=no_constants)
    except (OSError, ValueError):
        return default

def write_json(path: Path, obj: Any) -> None:
    write_atomic(path, json.dumps(obj, indent=1, sort_keys=True).encode())
