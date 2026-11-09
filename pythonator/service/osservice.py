"""Start pythonatord at login, as you, and restart it if it dies: a systemd user unit, a LaunchAgent or a logon task.
Never root, never SYSTEM. The installer (`pythonatorctl service install`) writes the file for this OS and starts it; `remove` stops it
and deletes the file. The OS restarts the background service after a crash, and the reconciler recovers from its
ledger, so your services come back too."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "io.github.cfunkz.pythonatord"
UNIT = "pythonatord.service"
TASK = "\\Pythonator\\pythonatord"

def _python() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")  # no console window at logon
    return str(exe)

def _run(argv: list[str], data: str | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(  # noqa: S603 - fixed system tools, our own arguments
            argv, input=data, capture_output=True, text=True, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return done.returncode, (done.stdout + done.stderr).strip()

def systemd_unit(python: str) -> str:
    return f"""[Unit]
Description=Pythonator background service
After=default.target

[Service]
Type=simple
ExecStart="{python}" -m pythonator.service.daemon
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
UMask=0077
Delegate=yes

[Install]
WantedBy=default.target
"""

def launch_agent(python: str, log: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{escape(python)}</string><string>-m</string><string>pythonator.service.daemon</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>4096</integer></dict>
  <key>Umask</key><integer>63</integer>
  <key>StandardErrorPath</key><string>{escape(log)}</string>
</dict></plist>
"""

def logon_task(python: str, user: str) -> str:
    """Task Scheduler XML: at your logon, limited rights, restarted on failure, not stopped on battery."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Pythonator background service</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(python)}</Command><Arguments>-m pythonator.service.daemon</Arguments></Exec></Actions>
</Task>
"""

def _user() -> str:
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")

def install() -> tuple[bool, str]:
    py = _python()
    if sys.platform == "linux":
        unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(py), encoding="utf-8")
        code, out = _run(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            return False, f"No systemd user session ({out[:200]}). The window starts the background service instead."
        code, out = _run(["systemctl", "--user", "enable", "--now", UNIT])
        return code == 0, "Starts at login (systemd user unit)." if code == 0 else out[:300]
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        log = str(Path.home() / "Library" / "Logs" / "pythonatord.log")
        plist.write_text(launch_agent(py, log), encoding="utf-8")
        uid = os.getuid()
        _run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
        code, out = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        return code == 0, "Starts at login (LaunchAgent)." if code == 0 else out[:300]
    if sys.platform == "win32":
        with tempfile.TemporaryDirectory() as d:
            xml = Path(d) / "task.xml"
            xml.write_text(logon_task(py, _user()), encoding="utf-16")
            code, out = _run(["schtasks", "/Create", "/F", "/TN", TASK, "/XML", str(xml)])
        if code != 0:
            return False, out[:300]
        _run(["schtasks", "/Run", "/TN", TASK])
        return True, "Starts at logon (scheduled task, your user, limited rights)."
    return False, "Not supported on this OS."

def remove() -> tuple[bool, str]:
    with contextlib.suppress(OSError):
        window_at_login(False)
    if sys.platform == "linux":
        _run(["systemctl", "--user", "disable", "--now", UNIT])
        unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / UNIT
        unit.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
        (Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist").unlink(missing_ok=True)
    elif sys.platform == "win32":
        _run(["schtasks", "/End", "/TN", TASK])
        code, out = _run(["schtasks", "/Delete", "/F", "/TN", TASK])
        if code != 0 and "cannot find" not in out.lower():
            return False, out[:300]
    return True, "Removed: the background service no longer starts at login."

def status() -> str:
    if sys.platform == "linux":
        return _run(["systemctl", "--user", "is-active", UNIT])[1] or "unknown"
    if sys.platform == "darwin":
        return "loaded" if _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])[0] == 0 else "not loaded"
    if sys.platform == "win32":
        return "installed" if _run(["schtasks", "/Query", "/TN", TASK])[0] == 0 else "not installed"
    return "unsupported"

def at_login(on: bool | None = None) -> bool:
    """Whether pythonatord starts at login. `on` adds or removes that, without starting or stopping it now."""
    if sys.platform == "linux":
        unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / UNIT
        if on and not Path("/usr/lib/systemd/user", UNIT).exists():  # the .deb has its own
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(systemd_unit(_python()), encoding="utf-8")
            _run(["systemctl", "--user", "daemon-reload"])
        if on is not None:
            _run(["systemctl", "--user", "enable" if on else "disable", UNIT])
        return _run(["systemctl", "--user", "is-enabled", UNIT])[0] == 0
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if on:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(launch_agent(_python(), str(Path.home() / "Library" / "Logs" / "pythonatord.log")), "utf-8")
        elif on is False:
            plist.unlink(missing_ok=True)
        return plist.exists()
    if sys.platform == "win32" and on is not None:
        _run(["schtasks", "/Change", "/TN", TASK, "/ENABLE" if on else "/DISABLE"])
        if on and _run(["schtasks", "/Query", "/TN", TASK])[0] != 0:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "task.xml").write_text(logon_task(_python(), _user()), encoding="utf-16")
                _run(["schtasks", "/Create", "/F", "/TN", TASK, "/XML", str(Path(d) / "task.xml")])
    # The XML of a disabled task says <Enabled>false</Enabled> in its settings, in every language.
    code, out = _run(["schtasks", "/Query", "/TN", TASK, "/XML"]) if sys.platform == "win32" else (1, "")
    return code == 0 and "<Settings>" in out and "<Enabled>false</Enabled>" not in out.split("<Settings>", 1)[1]

def window_at_login(on: bool | None = None) -> bool:
    """Open the window, hidden in the tray, at login: a Run value on Windows, a LaunchAgent on macOS, an autostart
    entry on Linux. `on` None only says whether it is set."""
    py = _python()
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415 - Windows only

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            if on:
                winreg.SetValueEx(k, "Pythonator", 0, winreg.REG_SZ, f'"{py}" -m pythonator --tray')
            elif on is False:
                with contextlib.suppress(OSError):
                    winreg.DeleteValue(k, "Pythonator")
            try:
                return bool(winreg.QueryValueEx(k, "Pythonator")[0])
            except OSError:
                return False
    if sys.platform == "darwin":
        f = Path.home() / "Library" / "LaunchAgents" / "io.github.cfunkz.pythonator.plist"
        text = launch_agent(py, "/dev/null").replace(LABEL, "io.github.cfunkz.pythonator").replace(
            "pythonator.service.daemon</string>", "pythonator</string><string>--tray</string>")
    else:
        f = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "pythonator.desktop"
        text = f'[Desktop Entry]\nType=Application\nName=Pythonator\nExec="{py}" -m pythonator --tray\nIcon=pythonator\n'
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    elif on is False:
        f.unlink(missing_ok=True)
    return f.exists()
