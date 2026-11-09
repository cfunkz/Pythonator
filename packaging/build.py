"""Build this OS's downloads into release/:  python packaging/build.py

    Windows  Pythonator-<v>-windows-x64-setup.exe (installer, for you only) and -windows-x64.zip (portable)
    macOS    Pythonator-<v>-macos-<arch>.dmg (drag to Applications) and -macos-<arch>.zip (the app alone)
    Linux    pythonator_<v>_<arch>.deb and Pythonator-<v>-linux-<arch>.tar.gz (portable)

Each carries its own Python: a relocatable CPython from python-build-standalone (installed by uv), with Pythonator
and every package from requirements.lock installed into it, checked against its hash. Installing needs no network
and no Python of yours. Run it after `pip wheel` has put Pythonator's wheel in dist/ (the release workflow does).
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "package"
OUT = ROOT / "release"
PYTHON = "3.13"
ICON_PNG = ROOT / "pythonator" / "resources" / "icon-256.png"
ICON_ICO = ROOT / "pythonator" / "resources" / "icon.ico"
DOCS = ("README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md")


def run(*argv: object, **kw: object) -> None:
    print("+", " ".join(str(a) for a in argv), flush=True)
    subprocess.run([str(a) for a in argv], check=True, **kw)  # noqa: S603 - build tools, our own arguments


def version() -> str:
    for line in (ROOT / "pythonator" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise SystemExit("no __version__")


def arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def python_exe(home: Path) -> Path:
    return home / "python.exe" if sys.platform == "win32" else home / "bin" / "python3"


def bundled_python(dest: Path) -> Path:
    """A relocatable CPython at `dest` with Pythonator and the locked packages in it. Returns its python."""
    cache = BUILD / "uv-python"
    env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(cache), "UV_PYTHON_BIN_DIR": str(BUILD / "uv-bin")}
    uv = [sys.executable, "-m", "uv"] if importlib.util.find_spec("uv") else [shutil.which("uv") or "uv"]
    run(*uv, "python", "install", PYTHON, env=env)  # uv checks each download against its own sha256
    src = next(p for p in sorted(cache.glob(f"cpython-{PYTHON}*")) if p.is_dir())
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, symlinks=True)
    for marker in dest.rglob("EXTERNALLY-MANAGED"):  # uv marks its own Pythons; this one is ours alone
        marker.unlink()
    py = python_exe(dest)
    run(py, "-m", "ensurepip")  # offline, and a no-op when pip is already there
    pip = [py, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--no-input"]
    run(*pip, "--require-hashes", "--only-binary=:all:", "-r", ROOT / "requirements.lock")
    wheel = next((ROOT / "dist").glob("pythonator-*.whl"))
    run(*pip, "--no-deps", "--no-index", wheel)
    # Console scripts carry this build machine's path (Pythonator's launchers run `python -m` instead). ruff and uv
    # are real programs, found next to Python, and stay. Nothing is compiled against this Python, so no headers.
    for p in [*dest.glob("bin/*"), *dest.glob("Scripts/*")]:
        if p.is_symlink() or p.name.startswith("python3") or p.stem in ("ruff", "uv", "uvx", "uvw"):
            continue
        if p.suffix == ".exe" or p.read_bytes()[:2] == b"#!":
            p.unlink()
    for p in dest.glob("bin/*"):
        if p.is_symlink() and not p.exists():
            p.unlink()
    shutil.rmtree(dest / "include", ignore_errors=True)
    run(py, "-m", "pythonator.ctl.main", "version")
    return py


def script(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\r\n" if path.suffix == ".cmd" else "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def docs_into(folder: Path) -> None:
    for name in DOCS:
        shutil.copy2(ROOT / name, folder / name)


def tar_gz(folder: Path, out: Path) -> None:
    with tarfile.open(out, "w:gz") as t:
        t.add(folder, arcname=folder.name)


def windows(v: str) -> None:
    app = BUILD / "Pythonator"
    shutil.rmtree(app, ignore_errors=True)
    app.mkdir(parents=True)
    bundled_python(app / "python")
    shutil.copy2(ICON_ICO, app / "pythonator.ico")
    script(app / "Pythonator.cmd", '@start "" "%~dp0python\\pythonw.exe" -m pythonator\n')
    # bin\ is put on your PATH by setup.exe
    script(app / "bin" / "pythonator.cmd", '@start "" "%~dp0..\\python\\pythonw.exe" -m pythonator\n')
    script(app / "bin" / "pythonatorctl.cmd", '@"%~dp0..\\python\\python.exe" -m pythonator.ctl.main %*\n')
    docs_into(app)
    with zipfile.ZipFile(OUT / f"Pythonator-{v}-windows-x64.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(app.rglob("*")):
            z.write(p, Path("Pythonator") / p.relative_to(app))
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    run(iscc, f"/DVersion={v}", f"/DSource={app}", f"/DOut={OUT}", ROOT / "packaging" / "pythonator.iss")


def macos(v: str) -> None:
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    contents = stage / "Pythonator.app" / "Contents"
    (contents / "Resources").mkdir(parents=True)
    bundled_python(contents / "Resources" / "python")
    iconset = BUILD / "icon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 128, 256):
        for scale in (1, 2):
            px = size * scale
            run("sips", "-z", px, px, ICON_PNG, "--out", iconset / f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png")
    run("iconutil", "-c", "icns", iconset, "-o", contents / "Resources" / "icon.icns")
    # The launcher keeps the login service pointing at this copy of the app, wherever it was dragged.
    script(contents / "MacOS" / "Pythonator", """#!/bin/sh
PY="$(cd "$(dirname "$0")/.." && pwd)/Resources/python/bin/python3"
AGENT="$HOME/Library/LaunchAgents/io.github.cfunkz.pythonatord.plist"
grep -qF "$PY" "$AGENT" 2>/dev/null || "$PY" -m pythonator.ctl.main service install >/dev/null 2>&1
exec "$PY" -m pythonator "$@"
""")
    (contents / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Pythonator</string>
<key>CFBundleDisplayName</key><string>Pythonator</string>
<key>CFBundleIdentifier</key><string>io.github.cfunkz.pythonator</string>
<key>CFBundleExecutable</key><string>Pythonator</string>
<key>CFBundleIconFile</key><string>icon</string>
<key>CFBundleShortVersionString</key><string>{version()}</string>
<key>CFBundleVersion</key><string>{version()}</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
""", encoding="utf-8")
    # The app alone, zipped the way Finder does (keeps symlinks and the bundle's attributes).
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", stage / "Pythonator.app",
        OUT / f"Pythonator-{v}-macos-{arch()}.zip")
    (stage / "Applications").symlink_to("/Applications")
    docs_into(stage)
    for attempt in range(3):  # hdiutil sometimes reports "Resource busy" on a fresh runner
        try:
            run("hdiutil", "create", "-volname", "Pythonator", "-srcfolder", stage, "-ov", "-format", "UDZO",
                OUT / f"Pythonator-{v}-macos-{arch()}.dmg")
            break
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(10)


# Stop it for every signed-in user first (their services stop too), so no daemon runs from deleted files.
PRERM = """#!/bin/sh
set -e
systemctl --global disable pythonatord.service >/dev/null 2>&1 || true
for u in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $2}'); do
    systemctl --user -M "$u@" stop pythonatord.service >/dev/null 2>&1 || true
done
"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=Pythonator
Comment=Run and supervise services in isolated environments
Exec={exec}
Icon={icon}
Categories=Development;System;
Terminal=false
"""


def linux(v: str) -> None:
    deb_arch = {"x64": "amd64", "arm64": "arm64"}[arch()]
    root = BUILD / "deb"
    shutil.rmtree(root, ignore_errors=True)
    opt = root / "opt" / "pythonator"
    opt.mkdir(parents=True)
    bundled_python(opt / "python")
    for name, module in (("pythonator", "pythonator"), ("pythonatorctl", "pythonator.ctl.main")):
        script(root / "usr" / "bin" / name, f'#!/bin/sh\nexec /opt/pythonator/python/bin/python3 -m {module} "$@"\n')
    icons = root / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    icons.mkdir(parents=True)
    shutil.copy2(ICON_PNG, icons / "pythonator.png")
    (root / "usr" / "share" / "applications").mkdir(parents=True)
    (root / "usr" / "share" / "applications" / "pythonator.desktop").write_text(
        DESKTOP.format(exec="/usr/bin/pythonator", icon="pythonator"), encoding="utf-8")
    unit = root / "usr" / "lib" / "systemd" / "user" / "pythonatord.service"
    unit.parent.mkdir(parents=True)
    sys.path.insert(0, str(ROOT))
    from pythonator.service.osservice import systemd_unit  # noqa: PLC0415 - the same unit `service install` writes

    unit.write_text(systemd_unit("/opt/pythonator/python/bin/python3"), encoding="utf-8")
    doc = root / "usr" / "share" / "doc" / "pythonator"
    doc.mkdir(parents=True)
    docs_into(doc)
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) // 1024
    debian = root / "DEBIAN"
    debian.mkdir()
    (debian / "control").write_text(f"""Package: pythonator
Version: {v.replace('-', '~')}
Architecture: {deb_arch}
Maintainer: cfunkz <hello@routezero.tech>
Installed-Size: {size}
Depends: libc6, libegl1, libgl1, libxkbcommon0, libxkbcommon-x11-0, libfontconfig1, libdbus-1-3, libxcb-cursor0
Recommends: bubblewrap, passt, libseccomp2
Suggests: podman
Section: devel
Priority: optional
Homepage: https://cfunkz.github.io/Pythonator/
Description: service manager for web apps, APIs, workers and scheduled jobs
 Pythonator runs each service in an enforced isolation environment and applies changes only with your
 approval. It carries its own Python in /opt/pythonator.
""", encoding="utf-8")
    # Every user's background service starts at login (each as that user); their own unit, if any, takes precedence.
    script(debian / "postinst", "#!/bin/sh\nset -e\nsystemctl --global enable pythonatord.service >/dev/null 2>&1 || true\n")
    script(debian / "prerm", PRERM)
    run("dpkg-deb", "--root-owner-group", "-Zxz", "--build", root, OUT / f"pythonator_{v}_{deb_arch}.deb")
    # The portable folder: the same Python and app, started from wherever it is unpacked.
    portable = BUILD / "Pythonator"
    shutil.rmtree(portable, ignore_errors=True)
    shutil.copytree(opt, portable, symlinks=True)
    for name, module in (("pythonator", "pythonator"), ("pythonatorctl", "pythonator.ctl.main")):
        script(portable / name, f'#!/bin/sh\nexec "$(dirname "$0")/python/bin/python3" -m {module} "$@"\n')
    docs_into(portable)
    tar_gz(portable, OUT / f"Pythonator-{v}-linux-{arch()}.tar.gz")


def main() -> None:
    # The release's name: the tag without its v (0.0.1, or 0.0.1-alpha for a prerelease), else __version__.
    v = os.environ.get("RELEASE_LABEL", "").removeprefix("v") or version()
    OUT.mkdir(exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    {"win32": windows, "darwin": macos}.get(sys.platform, linux)(v)
    for p in sorted(OUT.iterdir()):
        print(f"{p.name}  {p.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
