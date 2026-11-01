"""Build Pythonator's release packages for the system this runs on.

    pip install pyinstaller pillow
    python packaging/build.py

| System  | Packages in release/                                                         |
| ------- | ---------------------------------------------------------------------------- |
| Windows | Pythonator-<v>-windows-x64-setup.exe (Inno Setup, if installed), …-x64.zip    |
| macOS   | Pythonator-<v>-macos-<arm64|x86_64>.dmg                                       |
| Linux   | pythonator_<v>_<arch>.deb, Pythonator-<v>-linux-<arch>.tar.gz                 |

PyInstaller can't build for another system, so each package is built on its own system. Pushing a
version tag builds all of them on GitHub and attaches them to a release (.github/workflows/release.yml).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT = ROOT / "release"
ICON = ROOT / "pythonator" / "resources" / "icon.ico"
APP_ID = "io.github.cfunkz.pythonator"
DESCRIPTION = "Supervisor for long-running scripts, web servers and bots"
# Qt's xcb platform plugin needs these; everything else ships inside the app.
DEB_DEPENDS = "libegl1, libgl1, libxkbcommon0, libxkbcommon-x11-0, libfontconfig1, libdbus-1-3, libxcb-cursor0"


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from pythonator import __version__  # noqa: PLC0415

    return __version__


def run(*argv: str | Path, cwd: Path = ROOT) -> None:
    print("+", " ".join(map(str, argv)), flush=True)
    subprocess.run([str(a) for a in argv], cwd=cwd, check=True)


def arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x86_64" if sys.platform == "darwin" else "x64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def pyinstaller() -> None:
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "packaging/pythonator.spec")


# -- Windows ----------------------------------------------------------------------------------------

_INNO = r"""
[Setup]
AppId={{C0A8C92C-8AF7-43C3-A27E-F5C80F07B7EB}
AppName=Pythonator
AppVersion=%(version)s
AppPublisher=cfunkz
AppPublisherURL=https://github.com/cfunkz/Pythonator
DefaultDirName={autopf}\Pythonator
DefaultGroupName=Pythonator
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=%(out)s
OutputBaseFilename=%(name)s
SetupIconFile=%(icon)s
UninstallDisplayIcon={app}\Pythonator.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "%(src)s\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Pythonator"; Filename: "{app}\Pythonator.exe"
Name: "{autodesktop}\Pythonator"; Filename: "{app}\Pythonator.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Pythonator.exe"; Description: "{cm:LaunchProgram,Pythonator}"; Flags: nowait postinstall skipifsilent
"""


def _iscc() -> Path | None:
    if found := shutil.which("iscc"):
        return Path(found)
    for root in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
        if root and (candidate := Path(root, "Inno Setup 6", "ISCC.exe")).is_file():
            return candidate
    return None


def package_windows(v: str) -> list[Path]:
    src = DIST / "Pythonator"
    base = f"Pythonator-{v}-windows-{arch()}"
    archive = OUT / f"{base}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            zf.write(path, Path("Pythonator") / path.relative_to(src))
    made = [archive]
    if iscc := _iscc():
        script = ROOT / "build" / "pythonator.iss"
        script.parent.mkdir(exist_ok=True)
        script.write_text(_INNO % {"version": v, "out": OUT, "name": f"{base}-setup", "icon": ICON, "src": src},
                          encoding="utf-8")
        run(iscc, "/Q", script)
        made.append(OUT / f"{base}-setup.exe")
    else:
        print("Inno Setup (ISCC.exe) not found: building the .zip only. https://jrsoftware.org/isdl.php")
    return made


# -- macOS ------------------------------------------------------------------------------------------


def package_macos(v: str) -> list[Path]:
    app = DIST / "Pythonator.app"
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    run("ditto", app, staging / "Pythonator.app")  # ditto keeps the bundle's symlinks and signature
    (staging / "Applications").symlink_to("/Applications")  # drag-to-install
    dmg = OUT / f"Pythonator-{v}-macos-{arch()}.dmg"
    dmg.unlink(missing_ok=True)
    for attempt in range(3):  # hdiutil sometimes fails with "Resource busy" while Spotlight looks at the image
        try:
            run("hdiutil", "create", "-volname", f"Pythonator {v}", "-srcfolder", staging, "-fs", "HFS+",
                "-format", "UDZO", "-ov", dmg)
            break
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(5)
    return [dmg]


# -- Linux ------------------------------------------------------------------------------------------

_DESKTOP = f"""[Desktop Entry]
Type=Application
Name=Pythonator
GenericName=Process supervisor
Comment={DESCRIPTION}
Exec=/opt/pythonator/Pythonator
Icon=pythonator
Terminal=false
Categories=Development;Utility;
StartupWMClass=Pythonator
"""


def _png_icon(target: Path) -> None:
    from PIL import Image  # noqa: PLC0415 - pillow is a build requirement

    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(ICON) as im:
        im.size = max(im.info.get("sizes", {im.size}))
        im.convert("RGBA").save(target)


def package_linux(v: str) -> list[Path]:
    src = DIST / "Pythonator"
    deb_arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True,
                              check=False).stdout.strip() if shutil.which("dpkg") else ""
    made: list[Path] = []
    tar = OUT / f"Pythonator-{v}-linux-{arch()}.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(src, arcname="Pythonator")
    made.append(tar)
    if not deb_arch or not shutil.which("dpkg-deb"):
        print("dpkg-deb not found: building the .tar.gz only.")
        return made

    root = ROOT / "build" / "deb"
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(src, root / "opt" / "pythonator", symlinks=True)
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "pythonator").symlink_to("/opt/pythonator/Pythonator")
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / f"{APP_ID}.desktop").write_text(_DESKTOP, encoding="utf-8")
    _png_icon(root / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "pythonator.png")
    size_kb = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) // 1024
    control = root / "DEBIAN" / "control"
    control.parent.mkdir()
    control.write_text(
        f"Package: pythonator\nVersion: {v}\nArchitecture: {deb_arch}\nMaintainer: cfunkz <noreply@github.com>\n"
        f"Installed-Size: {size_kb}\nDepends: {DEB_DEPENDS}\nSection: devel\nPriority: optional\n"
        f"Homepage: https://github.com/cfunkz/Pythonator\nDescription: {DESCRIPTION}\n"
        " Runs Python, Node.js, Go, Java, .NET, Ruby, PHP and shell services with a live console,\n"
        " crash recovery, schedules and a built-in editor.\n",
        encoding="utf-8",
    )
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)
    deb = OUT / f"pythonator_{v}_{deb_arch}.deb"
    run("dpkg-deb", "--build", "--root-owner-group", "-Zxz", root, deb)
    made.append(deb)
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-build", action="store_true", help="package an existing dist/ build")
    args = parser.parse_args()
    v = version()
    OUT.mkdir(exist_ok=True)
    if not args.skip_build:
        pyinstaller()
    if sys.platform == "win32":
        made = package_windows(v)
    elif sys.platform == "darwin":
        made = package_macos(v)
    else:
        made = package_linux(v)
    print("\nBuilt:")
    for path in made:
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
