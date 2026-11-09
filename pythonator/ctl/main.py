"""pythonatorctl: the version, and uninstall. Everything else is done in the window.
`shutdown` and `service` are kept for the installers and aren't listed in the help."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pythonator import __version__
from pythonator.service import ipc
from pythonator.service.client import Client
from pythonator.store.files import data_dir


def _call_daemon(method: str, start: bool, **params: object) -> bool:
    """Call the background service; with `start`, start it first if it isn't running (it must take back grants)."""
    root = data_dir()
    c = None
    for i in range(50 if start else 1):
        try:
            c = Client(root)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if i == 0 and start:
                ipc.spawn_daemon()
            time.sleep(0.1)
    if c is None:
        return False
    with c:
        try:
            c.call(method, **params)
        except ipc.RpcError as e:
            print(f"error {e.code}: {e.message}", file=sys.stderr)
            return False
        except OSError:  # it may close the connection as it stops
            pass
    return True

def install_dirs() -> tuple[Path, list[Path]]:
    """Where the installer put the app on this OS, and the shortcuts and links it made."""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        programs = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Microsoft/Windows/Start Menu/Programs"
        return local / "Programs" / "Pythonator", [programs / "Pythonator.lnk"]
    if sys.platform == "darwin":
        return home / "Library/Application Support/Pythonator/app", [home / "Applications/Pythonator.app",
                                                                     home / ".local/bin/pythonator",
                                                                     home / ".local/bin/pythonatorctl"]
    share = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    return share / "pythonator-app", [share / "applications/pythonator.desktop", home / ".local/bin/pythonator",
                                     home / ".local/bin/pythonatorctl"]

def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink(missing_ok=True)
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=True)

def _windows_path_without(folder: Path) -> None:
    import winreg  # noqa: PLC0415 - Windows only

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            return
        keep = [p for p in str(value).split(";") if p and p.rstrip("\\").lower() != str(folder).rstrip("\\").lower()]
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(keep))

def uninstall(remove_data: bool) -> int:
    from pythonator.service import osservice

    print("Removing Pythonator")
    _call_daemon("shutdown", start=True, uninstall=True)  # stops services and takes back every folder permission
    ok, msg = osservice.remove()
    if not ok:
        print(f"  login service: {msg}", file=sys.stderr)
    if sys.prefix.startswith("/opt/pythonator/"):  # the .deb owns the app's files
        if remove_data:
            _remove(data_dir())
        print("  Stopped. Remove the app with:  sudo apt remove pythonator")
        return 0
    app, links = install_dirs()
    if ".app/Contents/Resources/" in sys.prefix + "/":  # the .dmg's app, wherever it was dragged
        links.append(Path(sys.prefix).parents[2])
    for p in links:
        _remove(p)
    data = data_dir()
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            _windows_path_without(app / "bin")
        with contextlib.suppress(OSError):  # the Settings > Apps entry
            import winreg  # noqa: PLC0415

            for key in ("Pythonator", "Pythonator_is1"):  # an older install's entry, or setup.exe's
                with contextlib.suppress(OSError):
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{key}")
        # This program runs from the folder being removed, so a hidden cmd finishes the job once it has exited.
        if remove_data and (wsl := Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "wsl.exe").is_file():
            from pythonator.jail import wslsetup  # its disk is in the data folder

            with contextlib.suppress(OSError, subprocess.SubprocessError):
                wslsetup.remove(str(wsl), data)
        targets = [app] + ([data] if remove_data else [])
        rm = " & ".join(f'rmdir /s /q "{t}"' for t in targets)
        sys32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
        # One command line string: cmd strips the outer quotes after /c and keeps the inner ones.
        subprocess.Popen(f'"{sys32 / "cmd.exe"}" /c ""{sys32 / "PING.EXE"}" -n 3 127.0.0.1 >nul & {rm}"',  # noqa: S603
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 | 0x00000008)  # CREATE_NO_WINDOW | DETACHED_PROCESS
    else:
        _remove(app)
        if remove_data:
            _remove(data)
    print("  Done. Your services' folders were not touched." + ("" if remove_data else f" Settings kept in {data}."))
    return 0

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pythonatorctl", description="Pythonator is used from its window. This only "
                                 "shows the version and uninstalls.")
    ap.add_argument("--version", action="version", version=f"pythonator {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{version,uninstall}")
    sub.add_parser("version", help="print the version")
    un = sub.add_parser("uninstall", help="stop everything, take back folder permissions and remove the app")
    un.add_argument("--remove-data", action="store_true", help="also delete your settings, logs and service list")
    # for the installers
    sd = sub.add_parser("shutdown")
    sd.add_argument("--uninstall", action="store_true")
    sub.add_parser("service").add_argument("action", choices=["install", "remove", "status"])
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to cp1252
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    if args.cmd == "version":
        print(f"pythonator {__version__}")
        return 0
    if args.cmd == "uninstall":
        return uninstall(args.remove_data)
    if args.cmd == "shutdown":
        _call_daemon("shutdown", start=args.uninstall, uninstall=bool(args.uninstall))
        return 0
    from pythonator.service import osservice

    if args.action == "status":
        print(osservice.status())
        return 0
    ok, msg = osservice.install() if args.action == "install" else osservice.remove()
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
