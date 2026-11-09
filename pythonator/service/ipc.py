"""IPC between pythonatord and its clients (the window, and plugins via the host).
Transport: a Unix socket in an owner-only folder. On Windows (where asyncio has no Unix sockets): TCP on 127.0.0.1
with a random port written to an owner-only file; the peer check there is the HMAC proof below, which a process
that can't read your profile's files can't pass.
Framing: one JSON object per line, at most 4 MB. Auth: both sides prove they know the key in ipc.key (0600) with
HMAC-SHA256 over two fresh nonces, so a process that can reach the socket but can't read your files is refused,
and a fake daemon can't impersonate the real one."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

from pythonator.store.files import ensure_private_dir, write_atomic

MAX_LINE = 4 * 1024 * 1024
PROTOCOL = 1

def socket_path(root: Path) -> Path:
    """The socket lives in an owner-only folder. Unix socket paths are limited to ~100 bytes, so a long data folder
    falls back to $XDG_RUNTIME_DIR, then /tmp/pythonator-<uid> (refused if someone else owns it)."""
    tag = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    candidates = [root / "run"]
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(xdg) / f"pythonator-{tag}")
    candidates.append(Path(tempfile.gettempdir()) / f"pythonator-{os.getuid() if hasattr(os, 'getuid') else 0}-{tag}")
    for d in candidates:
        if len(str(d / "pythonatord.sock")) < 100:
            return ensure_private_dir(d) / "pythonatord.sock"
    raise OSError("no short enough folder for the background service's socket")

def load_key(root: Path, create: bool = False) -> bytes:
    p = root / "ipc.key"
    if not p.exists():
        if not create:
            raise FileNotFoundError("ipc.key is missing; start pythonatord first")
        write_atomic(p, secrets.token_hex(32).encode())
    if sys.platform != "win32":
        st = p.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PermissionError("ipc.key must be owned by you and readable only by you")
    return bytes.fromhex(p.read_text().strip())

def proof(key: bytes, role: bytes, a: str, b: str) -> str:
    return hmac.new(key, role + b"|" + a.encode() + b"|" + b.encode(), hashlib.sha256).hexdigest()

def check(key: bytes, role: bytes, a: str, b: str, got: object) -> bool:
    return isinstance(got, str) and hmac.compare_digest(proof(key, role, a, b), got)

def nonce() -> str:
    return secrets.token_hex(16)

def dumps(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode()

def loads(raw: bytes) -> dict[str, Any]:
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("message must be an object")
    return obj

def use_tcp() -> bool:
    """Windows always; elsewhere only when PYTHONATOR_IPC=tcp (so the Windows transport is tested on Linux too)."""
    return sys.platform == "win32" or os.environ.get("PYTHONATOR_IPC") == "tcp"

def port_path(root: Path) -> Path:
    return ensure_private_dir(root / "run") / "pythonatord.port"

def read_port(root: Path) -> int:
    """The daemon's TCP port, or raise FileNotFoundError when it isn't running (same as a missing Unix socket)."""
    try:
        port = int(port_path(root).read_text().strip())
    except ValueError:
        raise FileNotFoundError("pythonatord.port is unreadable") from None
    if not 1024 <= port <= 65535:
        raise FileNotFoundError("pythonatord.port is out of range")
    return port

def endpoint_exists(root: Path) -> bool:
    return port_path(root).exists() if use_tcp() else socket_path(root).exists()

def spawn_daemon() -> None:
    """Start pythonatord detached from this window or terminal: closing either never stops your services."""
    import subprocess

    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    # The window moved into the data folder (process.harden), so `-m` won't find a source checkout: a window run from
    # source (`python -m pythonator` in the repo) passes its own code on, never an installed copy's.
    src = Path(__file__).resolve().parents[2]
    if (src / "pyproject.toml").is_file() and (src / "pythonator" / "__init__.py").is_file():
        kw["env"] = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(src), os.environ.get("PYTHONPATH"))))}
    subprocess.Popen([sys.executable, "-m", "pythonator.service.daemon"], **kw)  # noqa: S603 - our own interpreter

class RpcError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.fix = code, message, fix
