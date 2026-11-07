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
import sys
import threading
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from .. import launcher
from ..paths import IS_WINDOWS, DataDirs, quiet_start
from .base import Command, Grant, IsolationError, Policy, launcher_argv, merge
from .winloopback import EXIT_WFP, compile_lines, script_lines

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


NODE_SHIM = "pythonator-node.cjs"


@cache
def node_shim() -> bytes:
    """``resources/node_sandbox.cjs``: lets Node.js and npm work although the sandbox can't look at the folders
    above its own (see the file)."""
    return (Path(__file__).resolve().parent.parent / "resources" / "node_sandbox.cjs").read_bytes()


def node_env(env: dict[str, str], shim: Path, roots: Sequence[Path]) -> dict[str, str]:
    """What every Node.js in the sandbox starts with (``NODE_OPTIONS`` reaches the ones npm starts too): no
    realpath of module paths (``--preserve-symlinks``: it would stat C:\\ and every folder down, which an
    AppContainer may not), and the shim for npm's own stats. The service's own NODE_OPTIONS come after."""
    # Forward slashes: inside NODE_OPTIONS' quotes a backslash escapes the next character.
    ours = f'--preserve-symlinks --preserve-symlinks-main --require "{shim.as_posix()}"'
    mine = env.get("NODE_OPTIONS", "")
    return {"NODE_OPTIONS": f"{ours} {mine}".strip() if ours not in mine else mine,
            "PYTHONATOR_NODE_ROOTS": os.pathsep.join(dict.fromkeys(str(r) for r in roots))}


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
    shim = site / NODE_SHIM
    with contextlib.suppress(OSError):
        if not shim.is_file() or shim.read_bytes() != node_shim():
            shim.write_bytes(node_shim())
    env = {**env, "USERPROFILE": str(home), "HOME": str(home), "APPDATA": str(roaming), "LOCALAPPDATA": str(local),
           "TEMP": str(temp), "TMP": str(temp), "PYTHONATOR_TEMP": str(temp), "PYTHONATOR_SANDBOX": "1"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(site), env.get("PYTHONPATH", "")]))
    env.update(node_env(env, shim, [g.path for g in grants] + [base]))
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


_LEDGER = threading.Lock()  # a removal cleans up in the background while other services start


def record_grants(dirs: DataDirs, name: str, sid: str, folders: list[str]) -> None:
    with _LEDGER:
        ledger = read_grants(dirs)
        known = ledger.get(name, (sid, []))[1]
        ledger[name] = (sid, sorted({*known, *folders}))
        _write_grants(dirs, ledger)


def forget(dirs: DataDirs, ident: str, *, deleted: Path | None = None) -> None:
    """Take a removed service's AppContainer off every folder it was given (except ``deleted``, which is about
    to go), and delete its profile."""
    if not IS_WINDOWS:
        return
    _forget(dirs, container_name(ident), deleted)


def forget_all(dirs: DataDirs) -> None:
    """Uninstalling: take every recorded AppContainer off its folders and delete its profile."""
    for name in read_grants(dirs):
        if NAME.fullmatch(name):
            _forget(dirs, name)


def _forget(dirs: DataDirs, name: str, deleted: Path | None = None) -> None:
    with _LEDGER:
        ledger = read_grants(dirs)
        entry = ledger.pop(name, None)
        _write_grants(dirs, ledger)  # even when the profile was already gone
    if entry is not None:
        sid, folders = entry
        for folder in folders:
            if deleted is not None and Path(folder).resolve().is_relative_to(deleted.resolve()):
                continue
            with contextlib.suppress(OSError, launcher.SandboxError):
                launcher.windows_revoke(folder, [sid])
    with contextlib.suppress(OSError, launcher.SandboxError):
        launcher.windows_delete_profile(name)


# -- programs installed for all users ----------------------------------------------------------------------


ALL_APP_PACKAGES = "*S-1-15-2-1"  # icacls' name for ALL APPLICATION PACKAGES


def blocked_program_folder(folder: Path) -> bool:
    """A program's folder that the sandbox can't use and the launcher can't share: no read access for
    AppContainers, and not yours to change (installed for all users, such as C:\\Program Files\\nodejs set up
    without the entry Windows gives C:\\Program Files itself)."""
    if not IS_WINDOWS or not folder.is_dir():
        return False
    try:
        if launcher.windows_shared_with_sandboxes(str(folder)):
            return False
    except (OSError, launcher.SandboxError):
        return False  # can't tell: the launcher tries, and says what it couldn't do
    return not can_change_permissions(folder)


def can_change_permissions(folder: Path) -> bool:
    """Whether you may change ``folder``'s permissions (open it for WRITE_DAC), as the launcher would. A test of
    write access can't tell: Windows reports every folder as writable to ``os.access``."""
    if not IS_WINDOWS:
        return True
    import ctypes
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    k32.CreateFileW.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    # WRITE_DAC; share read, write and delete; OPEN_EXISTING; FILE_FLAG_BACKUP_SEMANTICS (needed for a folder)
    handle = k32.CreateFileW(str(folder), 0x00040000, 7, None, 3, 0x02000000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        return False
    k32.CloseHandle(handle)
    return True


def check_program_folders(folders: Sequence[Path]) -> None:
    """Refuse a start that can only fail with "Access is denied", with the fix. Raises IsolationError. (Node.js
    is not refused: sandboxes get a private copy of it, see :mod:`.private_node`.)"""
    for folder in folders:
        if blocked_program_folder(folder):
            raise IsolationError(
                f"The sandbox can't run programs from {folder}: it was installed for all users, and Windows only "
                "lets an administrator share it with sandboxes. Fix it once: Help → System check → Let sandboxes "
                f"use it, or in an administrator terminal: icacls \"{folder}\" "
                f"/grant \"{ALL_APP_PACKAGES}:(OI)(CI)(RX)\" "
                "(read and run only, as C:\\Program Files already allows). Or run this service in Container mode.")


def share_script(folder: Path) -> str:
    """The PowerShell that lets every sandbox read and run what is in ``folder`` (never write it)."""
    resolved = folder.resolve()
    if not resolved.is_dir() or any(c in str(resolved) for c in "'\"`$\n\r"):
        raise ValueError("not a program folder")
    path = str(resolved)
    return "\n".join(["$ErrorActionPreference = 'Stop'",
                      '$tool = Join-Path $env:SystemRoot "System32\\icacls.exe"',
                      f"& $tool '{path}' /grant '{ALL_APP_PACKAGES}:(OI)(CI)(RX)' /Q | Out-Null",
                      "exit $LASTEXITCODE"])


def share_program_folder(folder: Path) -> tuple[bool, str]:
    """Let sandboxes read and run ``folder``, with one UAC prompt: ``(done, message)``. Blocks (call it off the
    GUI thread)."""
    if not IS_WINDOWS:
        return False, "Only needed on Windows."
    try:
        code = _run_elevated(share_script(folder))
    except (OSError, ValueError) as exc:
        return False, f"Couldn't change the permissions: {exc}"
    if code == ERROR_CANCELLED:
        return False, "Cancelled: Windows didn't get administrator rights."
    if code:
        return False, f"Windows refused the change (icacls exit code {code})."
    return True, f"Done: sandboxed services can now run programs from {folder} (read and run only)."


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
    lines += [_remove_rule(rule_name(container, local=local)) for local in (True, False)]
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


def system32() -> Path:
    """Windows' own System32 folder, from Windows itself (``GetSystemDirectoryW``), not from a variable."""
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        if 0 < kernel32.GetSystemDirectoryW(buf, len(buf)) < len(buf):
            return Path(buf.value)
    return Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"


WSL_PROGRAMS = frozenset({"bash.exe", "wsl.exe", "wslhost.exe", "wslg.exe"})


def check_not_wsl(program: str) -> None:
    """Refuses WSL's own launchers. ``C:\\Windows\\System32\\bash.exe`` (found first on PATH on many PCs)
    hands the script to WSL, which runs it in its Linux system with your rights there, including your
    Windows drives under /mnt: out of reach of the AppContainer. Raises IsolationError."""
    path = Path(program)
    if path.name.lower() not in WSL_PROGRAMS:
        return
    if str(path.parent).rstrip("\\/").lower() == str(system32()).rstrip("\\/").lower() or "windowsapps" in \
            str(path).lower():
        raise IsolationError(f"{path.name} here is WSL's: it would run the script inside WSL, outside the Windows "
                             "sandbox. Install Git for Windows and put its bash first on PATH, or use Container mode")


def powershell() -> Path:
    """Windows PowerShell by its full path. A bare ``powershell.exe`` is looked for in the current folder first,
    which may be a project folder a sandboxed service can write: Make reachable would run its program elevated."""
    return system32() / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _exempt_sids() -> set[str] | None:
    """The SIDs (lower case) in Windows' list of loopback exemptions (``CheckNetIsolation LoopbackExempt -s``,
    which needs no administrator). None when Windows can't say."""
    tool = system32() / "CheckNetIsolation.exe"
    try:
        done = subprocess.run([str(tool), "LoopbackExempt", "-s"], capture_output=True, text=True,
                              errors="replace", timeout=20, check=False, creationflags=0x08000000,
                              startupinfo=quiet_start())
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return {m.group(0).lower() for m in SID.finditer(done.stdout)}


def loopback_exempt(ident: str) -> bool | None:
    """Whether **Make reachable** was done for this service: its AppContainer is in Windows' list of loopback
    exemptions. None when Windows can't say."""
    if not IS_WINDOWS:
        return None
    try:
        sid, _folder = launcher.windows_profile(container_name(ident))
    except (OSError, launcher.SandboxError):
        return None
    exempt = _exempt_sids()
    return None if exempt is None else sid.lower() in exempt


# -- what Make reachable leaves behind, and taking it back after a service is gone ----------------------------

RULE = re.compile(r"Pythonator (Pythonator\.[A-Za-z0-9.-]{1,53})(?: \(this PC\))?")
# Lists Pythonator's firewall rules with their ports, one per line: "<display name>\t<ports>". Every rule is read
# (no -Group filter): a filter that matches nothing is an error, and silencing it would hide "access denied" too.
LIST_RULES = "\n".join([
    "$ErrorActionPreference = 'Stop'", "$ProgressPreference = 'SilentlyContinue'",
    "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }",
    "foreach ($r in @(Get-NetFirewallRule | Where-Object { $_.Group -eq 'Pythonator' })) {",
    "  $p = @(($r | Get-NetFirewallPortFilter).LocalPort) -join ','",
    "  [Console]::Out.WriteLine($r.DisplayName + \"`t\" + $p)",
    "}",
    "exit 0"])


def reach_rules() -> dict[str, list[int]] | None:
    """The firewall rules Make reachable added on this PC: AppContainer name → the ports they open ([] for any
    port). Reading them needs no administrator. None when Windows can't say. Slow (a PowerShell): call it off the
    GUI thread."""
    if not IS_WINDOWS:
        return None
    try:
        done = subprocess.run([str(powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-EncodedCommand", encoded(LIST_RULES)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False,
                              creationflags=0x08000000, startupinfo=quiet_start(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return parse_rules(done.stdout)


def parse_rules(text: str) -> dict[str, list[int]]:
    found: dict[str, set[int]] = {}
    for line in text.splitlines():
        name, _tab, ports = line.strip().partition("\t")
        if (m := RULE.fullmatch(name.strip())) is None:
            continue
        found.setdefault(m.group(1), set()).update(
            int(p) for p in re.split(r"[,\s]+", ports) if p.isdigit() and 1 <= int(p) <= 65535)
    return {name: sorted(ports) for name, ports in sorted(found.items())}


def reach_left(containers: Sequence[str]) -> list[str] | None:
    """Which of these AppContainers still have Make reachable's firewall rules or loopback exemption (their
    services may be gone: nothing is created to find out). None when Windows can't say."""
    if not IS_WINDOWS:
        return []
    rules = reach_rules()
    exempt = _exempt_sids()
    if rules is None and exempt is None:
        return None
    left = []
    for container in containers:
        if container in (rules or {}):
            left.append(container)
            continue
        with contextlib.suppress(OSError, launcher.SandboxError):
            if exempt and launcher.windows_sid(container).lower() in exempt:
                left.append(container)
    return left


EXIT_LIST = 6  # the list of sandboxes to undo held something else


def unreach_list(entries: Sequence[tuple[str, str]]) -> str:
    """The list :func:`unreach_script` reads: one ``SID<tab>AppContainer name`` per line. Raises ValueError."""
    for sid, container in entries:
        if not SID.fullmatch(sid) or not NAME.fullmatch(container):
            raise ValueError("not an AppContainer of Pythonator")
    return "".join(f"{sid}\t{container}\n" for sid, container in entries)


def unreach_script(listing: Path) -> str:
    """The PowerShell that takes back everything Make reachable added for each sandbox in the file ``listing``
    (:func:`unreach_list`): the loopback exemption, the loopback permit and both firewall rules. Each step is
    skipped quietly when it is already gone, so running it twice is harmless.

    The list is read from a file, not written into the command: the WFP code alone takes most of what a command
    line may hold, and a dozen sandboxes made it too long for Windows ("The parameter is incorrect"). The file is
    yours to change, so the elevated script checks every line again and refuses anything that isn't a Pythonator
    sandbox: the most a changed file can do is undo Make reachable for another one."""
    path = str(listing)
    if any(c in path for c in "\r\n\0"):
        raise ValueError("not a usable file name")
    quoted = path.replace("'", "''")  # a single-quoted PowerShell string
    lines = ["$ErrorActionPreference = 'Stop'", "$ProgressPreference = 'SilentlyContinue'",
             '$tool = Join-Path $env:SystemRoot "System32\\CheckNetIsolation.exe"',
             "$entries = @()",
             f"foreach ($line in [IO.File]::ReadAllLines('{quoted}')) {{",
             "  if ($line -eq '') { continue }",
             "  $sid, $name, $extra = $line.Split([char]9)",
             "  if ($null -ne $extra -or $sid -cnotmatch '^S-1-15-2(-[0-9]+){1,14}$' -or "
             f"$name -cnotmatch '^Pythonator\\.[A-Za-z0-9.-]{{1,53}}$') {{ exit {EXIT_LIST} }}",
             "  $entries += ,@($sid, $name)",
             "}",
             "foreach ($e in $entries) { & $tool LoopbackExempt -d \"-p=$($e[0])\" | Out-Null }"]
    lines += compile_lines()
    lines += ["foreach ($e in $entries) {",
              "  $wfp = [PythonatorWfp]::Run($e[0], \"Pythonator $($e[1]) (this PC, loopback)\", [int[]]@(), $true)",
              f"  if (-not $wfp.StartsWith('ok')) {{ [Console]::Error.WriteLine($wfp); exit {EXIT_WFP} }}",
              "}",
              "$names = @{}",
              "foreach ($e in $entries) { $names[\"Pythonator $($e[1])\"] = 1; "
              "$names[\"Pythonator $($e[1]) (this PC)\"] = 1 }",
              "Get-NetFirewallRule | Where-Object { $_.Group -eq 'Pythonator' -and "
              "$names.ContainsKey($_.DisplayName) } | Remove-NetFirewallRule",
              "exit 0"]
    return "\n".join(lines)


def _remove_rule(display: str) -> str:
    return f"Get-NetFirewallRule -DisplayName '{display}' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"


def unreach(containers: Sequence[str]) -> tuple[bool, str]:
    """Undo Make reachable for these AppContainers, removed services' included, with one UAC prompt:
    ``(done, message)``. Blocks until done (call it off the GUI thread)."""
    import tempfile

    if not IS_WINDOWS:
        return True, "Nothing to undo: only Windows needs Make reachable."
    if not containers:
        return True, "Nothing to undo."
    listing: Path | None = None
    try:
        entries = [(launcher.windows_sid(c), c) for c in dict.fromkeys(containers)]
        with tempfile.NamedTemporaryFile("w", encoding="ascii", suffix=".txt", prefix="pythonator-undo-",
                                         delete=False) as f:
            f.write(unreach_list(entries))
            listing = Path(f.name)
        code = _run_elevated(unreach_script(listing))
    except (OSError, ValueError, launcher.SandboxError) as exc:
        return False, f"Couldn't change the Windows settings: {exc}"
    finally:
        if listing is not None:
            with contextlib.suppress(OSError):
                listing.unlink()
    if code:
        return False, _REACH_ERRORS.get(code, f"Windows refused the change (exit code {code}).")
    many = len(entries) > 1
    return True, (f"Done: removed the firewall rules and loopback permit{'s' if many else ''} of "
                  f"{len(entries)} sandbox{'es' if many else ''}.")


def unreach_all() -> tuple[bool, str] | None:
    """Uninstalling: undo every Make reachable found on this PC. None when there was nothing (or Windows can't
    say)."""
    rules = reach_rules()
    return unreach(list(rules)) if rules else None


REACH_NOTE = ("Windows keeps a sandboxed server out of reach, even of the browser on this PC, until you allow it "
              "once for this service: press Open (the browser button) or Configure → Network → Make reachable…, "
              "and accept the administrator prompt.")


_REACH_ERRORS = {ERROR_CANCELLED: "Cancelled: Windows didn't get administrator rights.",
                 EXIT_EXEMPT: "Windows refused the loopback exemption (CheckNetIsolation failed).",
                 EXIT_RULE: "Windows refused the firewall rule.",
                 EXIT_WFP: "Windows refused the loopback permit (Windows Filtering Platform).",
                 EXIT_LIST: "The list of firewall rules to remove was changed before Windows read it. Try again."}


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
    if code:
        return False, _REACH_ERRORS.get(code, f"Windows refused the change (exit code {code}).")
    if undo:
        return True, "Done: this PC and other computers can't reach it any more."
    if lan and ports:
        return True, f"Done: this PC and home or work networks can reach port {', '.join(map(str, ports))}."
    return True, "Done: programs on this PC, your browser included, can reach it on 127.0.0.1."


MAX_PARAMETERS = 32_000  # Windows' limit for a command line is 32767 characters, program name included


def elevated_parameters(command: str) -> str:
    """PowerShell's command line for ``command``. Raises OSError when Windows can't start one that long (it says
    only "The parameter is incorrect")."""
    parameters = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded(command)}"
    if len(parameters) > MAX_PARAMETERS:
        raise OSError(f"the PowerShell command is too long for Windows ({len(parameters)} characters)")
    return parameters


def _run_elevated(command: str) -> int:
    """Run ``command`` in an elevated, hidden PowerShell: its exit code, or ERROR_CANCELLED when you said no to
    the administrator prompt. Raises OSError when Windows couldn't start it at all."""
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
    # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI: Windows shows no error box of its own,
    # which also made it report any failure as ERROR_CANCELLED ("you cancelled") after you closed that box.
    info.fMask = 0x00000040 | 0x00000100 | 0x00000400
    info.lpVerb = "runas"
    info.lpFile = str(powershell())  # never a bare name: Windows would look in the current folder first
    info.lpParameters = elevated_parameters(command)
    info.lpDirectory = str(system32())
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = int(ctypes.get_last_error())  # type: ignore[attr-defined,unused-ignore]
        if error == ERROR_CANCELLED:
            return ERROR_CANCELLED
        reason = ctypes.FormatError(error).strip() if error else "no reason given"  # type: ignore[attr-defined,unused-ignore]
        raise OSError(f"Windows couldn't start an administrator PowerShell: {reason} ({error})")
    try:
        kernel32.WaitForSingleObject(info.hProcess, 120_000)
        code = w.DWORD(1)
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
