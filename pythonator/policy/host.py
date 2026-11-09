"""Facts about this machine that the compiler needs. `probe()` is the only impure part; compilers take HostFacts."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from pythonator.safe.hostenv import resolve
from pythonator.safe.process import NO_WINDOW


@dataclass(frozen=True)
class HostFacts:
    os: str  # "linux", "darwin", "windows"
    home: str
    data_dir: str
    run_dir: str = ""  # short private folder for sockets (Unix socket paths must stay under ~100 bytes)
    uid: int = 0
    gid: int = 0
    program: tuple[str, ...] = ()  # Pythonator's package folder and its Python prefixes: never a service folder
    path_dirs: tuple[str, ...] = ()  # your PATH folders inside your home: never writable by a service
    bwrap: str = ""
    pasta: str = ""
    systemd_run: str = ""
    systemd_user: bool = False
    landlock_abi: int = 0
    usr_merged: bool = True
    python: str = ""  # interpreter for Python services (uv-owned runtime later; system Python in 0.0.1)
    launcher: str = ""  # path of the stdlib in-jail launcher
    node: str = ""
    podman: str = ""
    docker: str = ""
    winjail: str = ""  # Windows jail launcher
    appcontainer: bool = False  # Windows ran a probe in an AppContainer inside a job object
    wsl: str = ""  # wsl.exe, by its System32 path
    wsl_ready: bool = False  # Pythonator's own WSL distribution is set up
    sandbox_exec: str = ""  # macOS Seatbelt
    seatbelt_base: str = ""  # the Apple base profile this macOS needs ("" for none)
    cpus: int = 1
    seccomp: bool = False  # libseccomp loads
    runtimes: tuple[tuple[str, str], ...] = ()  # (runtime, executable) found here; python and node are also above
    display: tuple[tuple[str, str], ...] = ()  # Linux: DISPLAY, WAYLAND_DISPLAY… of your session, for GUI apps

    def runtime(self, key: str) -> str:
        if key == "python":
            return self.python
        if key == "node" and self.node:
            return self.node
        return next((exe for k, exe in self.runtimes if k == key), "")

def landlock_abi() -> int:
    """Landlock ABI version, 0 when the kernel doesn't support it (syscall 444, LANDLOCK_CREATE_RULESET_VERSION)."""
    if sys.platform != "linux":
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        v = libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
        return int(v) if v > 0 else 0
    except OSError:
        return 0

def _systemd_user_ok() -> bool:
    if not resolve("systemd-run"):
        return False
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(xdg) and Path(xdg, "systemd", "private").exists()

DISPLAY_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                "XDG_SESSION_TYPE", "QT_QPA_PLATFORM")

def probe(data_dir: str) -> HostFacts:
    osname = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform)
    here = Path(__file__).resolve().parent.parent
    return HostFacts(
        os=osname, home=str(Path.home()), data_dir=data_dir, run_dir=_run_dir(data_dir),
        uid=os.getuid() if hasattr(os, "getuid") else 0, gid=os.getgid() if hasattr(os, "getgid") else 0,
        bwrap=resolve("bwrap"), pasta=resolve("pasta"), systemd_run=resolve("systemd-run"),
        systemd_user=_systemd_user_ok(), landlock_abi=landlock_abi(),
        usr_merged=Path("/bin").is_symlink() if osname == "linux" else True,
        python=resolve("python3") or _console_python(), launcher=str(here / "jail" / "launcher.py"),
        node=resolve("node"), podman=resolve("podman"), docker=resolve("docker"),
        winjail=str(here / "jail" / "winjail.py"), appcontainer=osname == "windows" and _probe_appcontainer(here),
        wsl=_wsl_exe() if osname == "windows" else "", wsl_ready=osname == "windows" and _wsl_ready(data_dir),
        sandbox_exec=(sbx := _seatbelt()[0]), seatbelt_base=_seatbelt()[1] if sbx else "", cpus=os.cpu_count() or 1,
        seccomp=osname == "linux" and _libseccomp(), runtimes=_runtimes(),
        display=tuple((k, v) for k in DISPLAY_KEYS if (v := os.environ.get(k))) if osname == "linux" else (),
        program=tuple(dict.fromkeys(p for p in (str(here), sys.prefix, sys.base_prefix) if p)), path_dirs=_path_dirs(),
    )

def _path_dirs() -> tuple[str, ...]:
    home = str(Path.home())
    out = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.isabs(d) and d.startswith(home + os.sep):
            out.append(os.path.normpath(d))
    return tuple(dict.fromkeys(out))

def _runtimes() -> tuple[tuple[str, str], ...]:
    from pythonator.policy.runtimes import find_all

    return find_all()

def _libseccomp() -> bool:
    try:
        ctypes.CDLL("libseccomp.so.2")
        return True
    except OSError:
        return False

def _probe_appcontainer(here: Path) -> bool:
    """Start `cmd /c exit 7` in a throwaway AppContainer, once per background-service start (about 100 ms)."""
    import json
    import subprocess

    try:
        done = subprocess.run(  # noqa: S603 - fixed argv: this Python and our own launcher
            [_console_python(), "-I", "-S", str(here / "jail" / "winjail.py"), "--probe"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return bool(json.loads(done.stdout or "{}").get("appcontainer"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

_SEATBELT: tuple[str, str] | None = None

def _seatbelt() -> tuple[str, str]:
    """(sandbox-exec, base profile) when this macOS runs our profile; ("", "") otherwise. Probed once."""
    global _SEATBELT
    if _SEATBELT is None:
        _SEATBELT = ("", "")
        exe = "/usr/bin/sandbox-exec"
        if sys.platform == "darwin" and Path(exe).exists():
            import subprocess

            from pythonator.policy import seatbelt

            for base in seatbelt.BASES:
                if base and not Path("/System/Library/Sandbox/Profiles", base).is_file():
                    continue
                text, params = seatbelt.profile([(str(Path.home()), True)], network=True, ports=[], base=base)
                try:
                    ok = (
                        subprocess.run(  # noqa: S603 - fixed argv
                            [*seatbelt.argv(exe, text, params), "/usr/bin/true"], capture_output=True, timeout=10, check=False, **NO_WINDOW
                        ).returncode
                        == 0
                    )
                except (OSError, subprocess.SubprocessError):
                    ok = False
                if ok:
                    _SEATBELT = (exe, base)
                    break
    return _SEATBELT

def _wsl_exe() -> str:
    exe = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "wsl.exe"
    return str(exe) if exe.is_file() else ""

def _run_dir(data_dir: str) -> str:
    """$XDG_RUNTIME_DIR/pythonator when the session has one (short, private, cleared at logout), else the data dir."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if sys.platform == "linux" and xdg and Path(xdg).is_dir() and Path(xdg).stat().st_uid == os.getuid():
        return f"{xdg}/pythonator"
    return f"{data_dir}/run"

def _wsl_ready(data_dir: str) -> bool:
    return (Path(data_dir) / "wsl" / "ready").is_file()

def _console_python() -> str:
    """This interpreter, but never pythonw.exe: services need real stdout and stderr for their logs."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)

def describe() -> str:
    return f"{platform.system()} {platform.release()} · Python {platform.python_version()}"
