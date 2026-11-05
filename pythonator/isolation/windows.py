"""Windows sandbox: one AppContainer per service, plus *Make reachable* for servers. Ported from 0.0.9.

An AppContainer is an identity with no access to your files, other processes or, without network capabilities,
the network. The launcher (``--sandbox-exec``) grants that identity access to the service's folders and starts the
service in it. Pythonator remembers each folder it granted in ``sandbox/windows-grants.json`` and takes the entry
off again when the service is removed.

Reaching a sandboxed server: Windows keeps AppContainers off loopback and blocks inbound connections to them.
**Make reachable** adds, with one UAC prompt, a loopback exemption, the loopback permit of :mod:`.winloopback` and a
firewall rule that admit this PC only (remote address ``127.0.0.1``; Windows refuses ``::1`` in a rule), and for LAN
exposure a second rule for the listed ports on Private/Domain networks. The script is built here from validated
values only (a SID, digits, fixed names).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from .. import launcher
from ..paths import IS_WINDOWS, DataDirs, quiet_start
from .base import Command, Grant, IsolationError, Policy, launcher_argv, merge
from .winloopback import EXIT_WFP, script_lines

SID = re.compile(r"S-1-15-2(?:-\d+){1,14}")
NAME = re.compile(r"Pythonator\.[A-Za-z0-9.-]{1,53}")
ERROR_CANCELLED = 1223
EXIT_EXEMPT, EXIT_RULE = 3, 4
GRANTS_FILE = "windows-grants.json"

# Python 3.13 (and 3.12.4+) gives a folder made with os.mkdir(path, 0o700) private permissions without an entry
# for the AppContainer (CVE-2024-4030), so tempfile, ensurepip and pip fail in the sandbox. Every Python started
# in the sandbox loads this first through PYTHONPATH.
SITECUSTOMIZE = '''\
"""Pythonator: makes Python work in the Windows sandbox."""
import os
import sys

if os.name == "nt" and os.environ.get("PYTHONATOR_SANDBOX") == "1":
    _mkdir = os.mkdir

    def mkdir(path, mode=0o777, **kwargs):
        return _mkdir(path, 0o777 if mode == 0o700 else mode, **kwargs)

    os.mkdir = mkdir
    _temp = os.environ.get("PYTHONATOR_TEMP", "")
    if _temp and os.path.isdir(_temp):
        for _name in ("TEMP", "TMP"):
            if not os.path.isdir(os.environ.get(_name, "")):
                os.environ[_name] = _temp


def _next_sitecustomize():
    """The project's own sitecustomize, which this one hides: run it too."""
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or ".") != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


_next_sitecustomize()
'''


def container_name(ident: str) -> str:
    return launcher.container_name(ident)


@cache
def available() -> bool:
    """Run ``cmd /c exit 7`` in a throwaway AppContainer, once."""
    if not IS_WINDOWS:
        return False
    try:
        done = subprocess.run([*launcher_argv(), launcher.PROBE], capture_output=True, text=True, timeout=30,
                              check=False, creationflags=0x08000000, startupinfo=quiet_start())
        return bool(json.loads(done.stdout or "{}").get("appcontainer"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def capabilities() -> list[tuple[str, str]]:
    ok = "enforced" if available() else "not enforced"
    return [("Files outside its folders", ok), ("Network off", ok), ("Other processes", ok),
            ("Only listed ports", "enforced after Make reachable (firewall rules)"),
            ("Only this computer reaches its ports", "enforced (firewall rule for 127.0.0.1)")]


def temp_folder(local_appdata: Path, name: str) -> Path:
    """Windows sets TEMP of every AppContainer process to this folder under its LOCALAPPDATA, whatever the
    environment says; Pythonator makes exactly that folder so it exists."""
    return local_appdata / "Packages" / name.lower() / "AC" / "Temp"


def wrap(argv: list[str], cwd: Path, env: dict[str, str], policy: Policy, ident: str, dirs: DataDirs,
         cmdline: str | None = None) -> Command:
    if not available():
        raise IsolationError("Windows refused to start a program in an AppContainer here")
    name = container_name(ident)
    try:
        sid, folder = launcher.windows_profile(name)
    except (OSError, launcher.SandboxError) as exc:
        raise IsolationError(f"Could not set up the sandbox: {exc}") from exc
    base = Path(folder)
    home, roaming, local = base / "home", base / "home" / "Roaming", base / "home" / "Local"
    temp, site = temp_folder(local, name), base / "pythonator-site"
    for d in (home, roaming, local, temp, site):
        with contextlib.suppress(OSError):
            d.mkdir(parents=True, exist_ok=True)
    grants = merge([*policy.grants, Grant(home, True)])
    record_grants(dirs, name, sid, [str(g.path) for g in grants])
    with contextlib.suppress(OSError):
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    env = {**env, "USERPROFILE": str(home), "HOME": str(home), "APPDATA": str(roaming), "LOCALAPPDATA": str(local),
           "TEMP": str(temp), "TMP": str(temp), "PYTHONATOR_TEMP": str(temp), "PYTHONATOR_SANDBOX": "1"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(site), env.get("PYTHONPATH", "")]))
    plan = {"appcontainer": {"name": name, "network": policy.network,
                             "grants": [[str(g.path), g.write] for g in grants],
                             "cmdline": cmdline or subprocess.list2cmdline(argv)}}
    return Command([*launcher_argv(), json.dumps(plan)], cwd, env, describe="AppContainer")


# -- folders granted to sandboxes, and taking them back ---------------------------------------------------


def _ledger(dirs: DataDirs) -> Path:
    return dirs.sandboxes / GRANTS_FILE


def read_grants(dirs: DataDirs) -> dict[str, tuple[str, list[str]]]:
    """AppContainer name → (its SID, the folders it was given). Anything malformed is dropped."""
    try:
        data = json.loads(_ledger(dirs).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[str, list[str]]] = {}
    for name, entry in (data.items() if isinstance(data, dict) else ()):
        if isinstance(entry, dict) and SID.fullmatch(str(entry.get("sid", ""))) \
                and isinstance(entry.get("folders"), list):
            out[str(name)] = (str(entry["sid"]), [str(f) for f in entry["folders"]])
    return out


def _write_grants(dirs: DataDirs, ledger: dict[str, tuple[str, list[str]]]) -> None:
    from ..core.store import write_atomic

    data = {name: {"sid": sid, "folders": folders} for name, (sid, folders) in ledger.items()}
    with contextlib.suppress(OSError):
        write_atomic(_ledger(dirs), json.dumps(data, indent=1))


def record_grants(dirs: DataDirs, name: str, sid: str, folders: list[str]) -> None:
    ledger = read_grants(dirs)
    known = ledger.get(name, (sid, []))[1]
    ledger[name] = (sid, sorted({*known, *folders}))
    _write_grants(dirs, ledger)


def forget(dirs: DataDirs, ident: str) -> None:
    """Take a removed service's AppContainer off every folder it was given, and delete its profile."""
    if not IS_WINDOWS:
        return
    _forget(dirs, container_name(ident))


def forget_all(dirs: DataDirs) -> None:
    """Uninstalling: take every recorded AppContainer off its folders and delete its profile."""
    for name in read_grants(dirs):
        if NAME.fullmatch(name):
            _forget(dirs, name)


def _forget(dirs: DataDirs, name: str) -> None:
    ledger = read_grants(dirs)
    entry = ledger.pop(name, None)
    if entry is not None:
        sid, folders = entry
        for folder in folders:
            with contextlib.suppress(OSError, launcher.SandboxError):
                launcher.windows_revoke(folder, [sid])
    with contextlib.suppress(OSError, launcher.SandboxError):
        launcher.windows_delete_profile(name)
    _write_grants(dirs, ledger)  # even when the profile was already gone


# -- Make reachable -------------------------------------------------------------------------------------


def rule_name(container: str, *, local: bool = False) -> str:
    return f"Pythonator {container}" + (" (this PC)" if local else "")


def reach_script(sid: str, container: str, ports: Sequence[int], *, lan: bool, undo: bool = False) -> str:
    """The PowerShell that applies (or with ``undo`` removes) the loopback exemption, the loopback permit and the
    firewall rules."""
    if not SID.fullmatch(sid) or not NAME.fullmatch(container):
        raise ValueError("not an AppContainer of Pythonator")
    if any(not 1 <= int(p) <= 65535 for p in ports):
        raise ValueError("not a port")
    listed = ",".join(str(int(p)) for p in ports)
    lines = ["$ErrorActionPreference = 'Stop'", "$ProgressPreference = 'SilentlyContinue'",
             '$tool = Join-Path $env:SystemRoot "System32\\CheckNetIsolation.exe"',
             f"& $tool LoopbackExempt {'-d' if undo else '-a'} '-p={sid}' | Out-Null"]
    if not undo:
        lines.append(f"if ($LASTEXITCODE -ne 0) {{ exit {EXIT_EXEMPT} }}")
    lines += script_lines(sid, container, listed, undo=undo)
    for local in (True, False):
        lines.append(f"Get-NetFirewallRule -DisplayName '{rule_name(container, local=local)}' "
                     "-ErrorAction SilentlyContinue | Remove-NetFirewallRule")
    if not undo:
        port = f" -LocalPort {listed}" if listed else ""
        rules = [f"New-NetFirewallRule -DisplayName '{rule_name(container, local=True)}' -Group 'Pythonator' "
                 f"-Direction Inbound -Action Allow -Protocol TCP{port} -RemoteAddress 127.0.0.1 -Package '{sid}' "
                 "-Profile Any | Out-Null"]
        if lan and ports:
            rules.append(f"New-NetFirewallRule -DisplayName '{rule_name(container)}' -Group 'Pythonator' "
                         f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {listed} -Package '{sid}' "
                         "-Profile Private,Domain | Out-Null")
        lines += [f"try {{ {rule} }} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit {EXIT_RULE} }}"
                  for rule in rules]
    lines.append("exit 0")
    return "\n".join(lines)


def encoded(command: str) -> str:
    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")


def make_reachable(ident: str, ports: Sequence[int], *, lan: bool, undo: bool = False) -> tuple[bool, str]:
    """Apply the settings with one UAC prompt: ``(done, message)``. Blocks until done (call it off the GUI thread)."""
    if not IS_WINDOWS:
        return False, "Only needed on Windows: elsewhere this computer can always reach a sandboxed server."
    container = container_name(ident)
    try:
        sid, _folder = launcher.windows_profile(container)
        code = _run_elevated(reach_script(sid, container, ports, lan=lan, undo=undo))
    except (OSError, ValueError, launcher.SandboxError) as exc:
        return False, f"Couldn't change the Windows settings: {exc}"
    messages = {ERROR_CANCELLED: "Cancelled: Windows didn't get administrator rights.",
                EXIT_EXEMPT: "Windows refused the loopback exemption (CheckNetIsolation failed).",
                EXIT_RULE: "Windows refused the firewall rule.",
                EXIT_WFP: "Windows refused the loopback permit (Windows Filtering Platform)."}
    if code:
        return False, messages.get(code, f"Windows refused the change (exit code {code}).")
    if undo:
        return True, "Done: this PC and other computers can't reach it any more."
    if lan and ports:
        return True, f"Done: this PC and home or work networks can reach port {', '.join(map(str, ports))}."
    return True, "Done: programs on this PC, your browser included, can reach it on 127.0.0.1."


def _run_elevated(command: str) -> int:
    """Run ``command`` in an elevated, hidden PowerShell: its exit code, or ERROR_CANCELLED."""
    import ctypes
    from ctypes import wintypes as w

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = (("cbSize", w.DWORD), ("fMask", w.ULONG), ("hwnd", w.HWND), ("lpVerb", w.LPCWSTR),
                    ("lpFile", w.LPCWSTR), ("lpParameters", w.LPCWSTR), ("lpDirectory", w.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", w.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", w.LPCWSTR), ("hkeyClass", w.HKEY), ("dwHotKey", w.DWORD),
                    ("hIconOrMonitor", w.HANDLE), ("hProcess", w.HANDLE))

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    kernel32.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
    kernel32.CloseHandle.argtypes = (w.HANDLE,)
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded(command)}"
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return int(ctypes.get_last_error()) or ERROR_CANCELLED  # type: ignore[attr-defined,unused-ignore]
    try:
        kernel32.WaitForSingleObject(info.hProcess, 120_000)
        code = w.DWORD(1)
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
