"""Folders no service may be given, kept as data and checked the same way on every OS.
A service folder (or an extra folder grant) is refused when it IS one of these, CONTAINS one of these, or is
INSIDE a secret folder. Home itself and filesystem roots are refused outright."""

from __future__ import annotations

import ntpath
import posixpath
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

# Relative to the user's home. Secret stores: never readable by a service, whatever the mode.
SECRET_DIRS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".config/gcloud",
    ".config/gh",
    ".netrc",
    ".password-store",
    ".local/share/keyrings",
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    "Library/Keychains",
    "Library/Application Support/Google/Chrome",
    "Library/Cookies",
    "AppData/Roaming/Microsoft/Credentials",
    "AppData/Local/Microsoft/Credentials",
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Roaming/Mozilla",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".cargo/credentials.toml",
    ".cargo/credentials",
    ".terraform.d",
    ".config/containers",
    ".local/share/containers",
    ".colima",
    ".lima",
    ".rd",
    ".orbstack",
    "Library/Containers/com.docker.docker",
    "Library/Application Support/Firefox",
    "AppData/Local/Docker",
    "AppData",  # every program's settings and tokens (Pythonator's own data is refused separately)
    "Library/Application Support",
)
# Files and folders that run code when you log in or open a shell: a service must never write here.
AUTOSTART: tuple[str, ...] = (
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".config/autostart",
    ".config/systemd",
    "Library/LaunchAgents",
    "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup",
    ".local/bin",
    ".local/share/applications",
    ".bash_login",
    ".bash_logout",
    ".zshenv",
    ".zlogin",
    ".xprofile",
    ".pam_environment",
    ".config/fish",
    ".config/environment.d",
    ".cargo/bin",
    "go/bin",
    "Documents/PowerShell",
    "Documents/WindowsPowerShell",
    "OneDrive/Documents/PowerShell",
    "OneDrive/Documents/WindowsPowerShell",
    ".local/lib",  # user site-packages: a .pth file there runs in every Python you start
    "Library/Python",
)
# Absolute system locations, refused as service folders on every OS.
SYSTEM_ROOTS: tuple[str, ...] = (
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/var",
    "/System", "/Library", "/Applications", "/private", "/Volumes", "/opt", "/srv", "/nix", "/snap",
)
# Refused only as the folder itself: shared with every program (agent, tmux and display sockets live there), while a
# project folder inside it is fine.
SHARED_EXACT: tuple[str, ...] = ("/tmp", "/private/tmp", "/var/tmp", "/mnt", "/media")  # noqa: S108 - refused, not used

@dataclass(frozen=True)
class FolderVerdict:
    ok: bool
    reason: str = ""
    code: str = ""

# Windows system folders, on whichever drive the folder is on.
WINDOWS_ROOTS: tuple[str, ...] = ("Windows", "Program Files", "Program Files (x86)", "ProgramData")

def is_windows_path(p: str) -> bool:
    """C:\\x, C:/x or \\\\server\\share: a Windows path, whatever OS is checking it."""
    return bool(PureWindowsPath(p).drive)

def is_absolute(p: str) -> bool:
    return p.startswith("/") or PureWindowsPath(p).is_absolute()

def _flavour(home: str) -> tuple[type[PurePath], object]:
    return (PureWindowsPath, ntpath) if is_windows_path(home) else (PurePosixPath, posixpath)

def _norm(p: str | PurePath, kind: type[PurePath] = PurePosixPath, mod: object = posixpath) -> PurePath:
    return kind(mod.normpath(str(p)))  # type: ignore[attr-defined]

def _is_relative_to(child: PurePath, parent: PurePath) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False

def check_folder(folder: str, home: str, *, data_dir: str = "", write: bool = True,
                 program_dirs: tuple[str, ...] = (), path_dirs: tuple[str, ...] = ()) -> FolderVerdict:
    """Pure check of a folder path against the deny list (no file system access; links are handled by safefs)."""
    kind, mod = _flavour(home)
    f = _norm(folder, kind, mod)
    h = _norm(home, kind, mod)
    if not f.is_absolute():
        return FolderVerdict(False, "The folder must be an absolute path.", "POL-PATH-001")
    if f == h:
        return FolderVerdict(False, "Your whole home folder can't be given to a service. Pick a project folder.", "POL-PATH-002")
    if len(f.parts) <= 1:  # "/", a bare drive or a bare network share
        return FolderVerdict(False, "A whole drive can't be given to a service.", "POL-PATH-003")
    low = str(f).replace("\\", "/").lower()
    if low.startswith(("//wsl$/", "//wsl.localhost/")):
        return FolderVerdict(False, "A WSL path reaches your drives under another name; use the Windows path.", "POL-PATH-010")
    if kind is not PureWindowsPath and str(f) in SHARED_EXACT:
        return FolderVerdict(False, f"{f} is shared by every program; pick a folder inside it.", "POL-PATH-004")
    roots = [f"{f.drive}/{r}" for r in WINDOWS_ROOTS] if kind is PureWindowsPath else list(SYSTEM_ROOTS)
    for root in roots:
        r = _norm(root, kind, mod)
        if f == r or (len(r.parts) > 1 and _is_relative_to(f, r) and not _is_relative_to(f, h)):
            return FolderVerdict(False, f"{root} is a system folder.", "POL-PATH-004")
    if _is_relative_to(h, f):
        return FolderVerdict(False, "This folder contains your home folder.", "POL-PATH-005")
    for rel in SECRET_DIRS:
        s = h / rel
        if f == s or _is_relative_to(f, s):
            return FolderVerdict(False, f"~/{rel} holds secrets; no mode or setting can grant it.", "POL-PATH-006")
        if _is_relative_to(s, f):
            return FolderVerdict(False, f"This folder contains ~/{rel}, which holds secrets.", "POL-PATH-007")
    if write:
        for rel in AUTOSTART:
            s = h / rel
            if f == s or _is_relative_to(f, s) or _is_relative_to(s, f):
                return FolderVerdict(False, f"~/{rel} runs code when you log in; a service can't write there.", "POL-PATH-008")
    if write:
        for d in path_dirs:  # a folder on your PATH: a program planted there runs when you type its name
            pd = _norm(d, kind, mod)
            if d and (f == pd or _is_relative_to(f, pd) or _is_relative_to(pd, f)):
                return FolderVerdict(False, f"{d} is on your PATH; a service can't write there.", "POL-PATH-012")
    for prog in program_dirs:  # Pythonator's own code and its Python: a .pth file there runs as you
        d = _norm(prog, kind, mod)
        if prog and (f == d or _is_relative_to(f, d) or (write and _is_relative_to(d, f))):
            return FolderVerdict(False, f"{prog} holds Pythonator's own program files.", "POL-PATH-011")
    if data_dir:
        d = _norm(data_dir, kind, mod)
        if f == d or _is_relative_to(f, d) or _is_relative_to(d, f):
            return FolderVerdict(False, "This is Pythonator's own data folder.", "POL-PATH-009")
    return FolderVerdict(True)

def overlaps(a: str, b: str) -> bool:
    """The same folder, or one inside the other (either way round)."""
    kind, mod = _flavour(a)
    x, y = _norm(a, kind, mod), _norm(b, kind, mod)
    return x == y or _is_relative_to(x, y) or _is_relative_to(y, x)

def as_you_clash(name: str, full: bool, folder: str, others: list[tuple[str, bool, list[str]]]) -> str:
    """A Full access service must never run in a folder an isolated service can change (it would run what the
    sandbox wrote, as you), and the reverse. `others`: (name, is Full access, folders it can write). Returns the
    other service's name, or ""."""
    for other, other_full, writable in others:
        if other == name or other_full == full:
            continue
        if full and any(overlaps(folder, w) for w in writable):
            return other
        if not full and any(overlaps(w, folder) for w in writable[:1]):  # theirs: the folder it runs in
            return other
    return ""

