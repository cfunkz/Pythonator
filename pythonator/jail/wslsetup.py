"""Pythonator's own WSL distribution, for WSL mode on Windows.
Set up once (Engines → WSL → Set up): Microsoft's Debian image, installed
with WSL's own `--install` (WSL verifies the package), under Pythonator's data folder. It is configured so it can't
see your Windows drives or start Windows programs, gets bubblewrap and the runtimes from Debian's signed archive,
and receives the jail runner. Services then run there through `wslrun.py`; nothing runs in your own distributions."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

DISTRO = "Pythonator"
RUNNER = "/usr/local/lib/pythonator/wslrun.py"
WSL_CONF = """[automount]
enabled = false
mountFsTab = false

[interop]
enabled = false
appendWindowsPath = false

[user]
default = root
"""
PACKAGES = "bubblewrap libseccomp2 python3 python3-venv nodejs npm golang-go default-jre-headless ruby cargo ca-certificates"

def _run(argv: list[str], data: bytes | None = None, timeout: float = 900) -> tuple[int, str]:
    done = subprocess.run(  # noqa: S603 - wsl.exe by its System32 path, fixed arguments
        argv,
        input=data,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.returncode, decode(done.stdout + done.stderr)

def _env() -> dict[str, str]:
    import os

    keep = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "USERPROFILE", "LOCALAPPDATA", "TEMP", "TMP", "COMSPEC")
    return {**{k: v for k in keep if (v := os.environ.get(k))}, "WSL_UTF8": "1"}

def decode(data: bytes) -> str:
    """wsl.exe's own messages: UTF-16 on versions that ignore WSL_UTF8."""
    if b"\x00" in data:
        try:
            return data.decode("utf-16-le").lstrip("﻿")
        except UnicodeDecodeError:
            data = data.replace(b"\x00", b"")
    return data.decode("utf-8", "replace")

def installed(wsl: str) -> bool:
    """Whether Pythonator's distribution is really registered (the ready marker alone can outlive it)."""
    try:
        code, out = _run([wsl, "--list", "--quiet"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return code == 0 and DISTRO in [x.strip() for x in out.splitlines()]

def setup(wsl: str, data_dir: Path, say: Callable[[str], None]) -> tuple[bool, str]:
    """Install and configure the distribution. Safe to run again: each step checks or repeats harmlessly."""
    home = data_dir / "wsl"
    home.mkdir(parents=True, exist_ok=True)
    (home / "ready").unlink(missing_ok=True)
    code, out = _run([wsl, "--list", "--quiet"], timeout=30)
    if code != 0:
        return False, "WSL isn't installed. In an administrator PowerShell: wsl --install --no-distribution"
    if DISTRO not in [x.strip() for x in out.splitlines()]:
        say("Installing Debian from Microsoft's WSL catalog (about 100 MB)…")
        code, out = _run([wsl, "--install", "Debian", "--name", DISTRO, "--location", str(home / "disk"), "--no-launch"])
        if code != 0:
            return False, f"wsl --install failed: {out.strip()[:300]}. Update WSL first: wsl --update"
    root = [wsl, "--distribution", DISTRO, "--user", "root", "--exec"]
    say("Turning off drive mounts and Windows interop…")
    code, out = _run([*root, "/bin/sh", "-c", "cat > /etc/wsl.conf"], WSL_CONF.encode())
    if code != 0:
        return False, f"Couldn't write wsl.conf: {out.strip()[:300]}"
    _run([wsl, "--terminate", DISTRO], timeout=60)
    say("Installing bubblewrap and the runtimes from Debian's signed archive…")
    code, out = _run([*root, "/bin/sh", "-c", f"apt-get update -q && apt-get install -y -q --no-install-recommends {PACKAGES}"])
    if code != 0:
        return False, f"apt-get failed: {out.strip()[-300:]}"
    ok, msg = _install_files(wsl)
    if not ok:
        return False, msg
    code, out = _run([*root, "/bin/sh", "-c", "! grep -q ' /mnt/c ' /proc/mounts && test -x /usr/bin/bwrap"], timeout=60)
    if code != 0:
        return False, "The distribution still sees your C: drive or has no bubblewrap; setup didn't take. Run it again."
    (home / "ready").write_text(digest(), encoding="ascii")
    return True, "WSL mode is ready: Pythonator's own Debian, no drives mounted, no Windows interop."

def _files() -> list[tuple[Path, int]]:
    here = Path(__file__).parent
    return [(here / "wslrun.py", 755), (here / "launcher.py", 644), (here.parent / "net" / "gate.py", 644)]

def digest() -> str:
    """Which runner is in the distribution: kept in the ready marker, so an update reaches it without a new setup."""
    import hashlib

    h = hashlib.sha256()
    for src, _mode in _files():
        h.update(src.read_bytes())
    return h.hexdigest()[:16]

def _install_files(wsl: str) -> tuple[bool, str]:
    root = [wsl, "--distribution", DISTRO, "--user", "root", "--exec"]
    lib = Path(RUNNER).parent.as_posix()
    for src, mode in _files():
        dst = f"{lib}/{src.name}"
        code, out = _run([*root, "/bin/sh", "-c", f"mkdir -p {lib} && cat > {dst} && chmod {mode} {dst}"], src.read_bytes(),
                         timeout=120)
        if code != 0:
            return False, f"Couldn't install {src.name}: {out.strip()[:300]}"
    return True, ""

def refresh(wsl: str, data_dir: Path) -> bool:
    """After an update of Pythonator: copy the new runner into an existing distribution. True when it's current."""
    marker = data_dir / "wsl" / "ready"
    try:
        if marker.read_text(encoding="ascii").strip() == digest():
            return True
    except OSError:
        return False
    ok, _msg = _install_files(wsl)
    if ok:
        marker.write_text(digest(), encoding="ascii")
    return ok

def remove(wsl: str, data_dir: Path) -> tuple[bool, str]:
    code, out = _run([wsl, "--unregister", DISTRO], timeout=120)
    (data_dir / "wsl" / "ready").unlink(missing_ok=True)
    return code == 0, out.strip()[:300] or "Removed Pythonator's WSL distribution."
