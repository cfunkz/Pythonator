# PyInstaller build for Windows (.exe folder), macOS (Pythonator.app) and Linux (folder).
#   pip install pyinstaller pillow
#   pyinstaller --noconfirm packaging/pythonator.spec
# Output: dist/Pythonator/ (Windows, Linux) or dist/Pythonator.app (macOS).
# Pillow lets PyInstaller turn icon.ico into the .icns a Mac app needs.
# ruff: noqa
import sys
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
sys.path.insert(0, str(ROOT))
from pythonator import __version__  # noqa: E402

ICON = str(ROOT / "pythonator" / "resources" / "icon.ico")

a = Analysis(  # noqa: F821
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "pythonator" / "resources"), "pythonator/resources"),
           (str(ROOT / ".env.example"), ".")],  # the version: pythonator/__init__.py reads it
    hiddenimports=["jedi"] if find_spec("jedi") else [],  # optional code completion
    excludes=["tkinter", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml", "PyQt6.QtQuick",
              "PyQt6.QtMultimedia", "PyQt6.QtPdf", "PyQt6.QtBluetooth", "PyQt6.QtDesigner"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Pythonator",
    console=False,
    icon=ICON,
    # macOS: build for the Python's own architecture; use a universal2 Python for both.
)
coll = COLLECT(exe, a.binaries, a.datas, name="Pythonator")  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="Pythonator.app",
        icon=ICON,
        bundle_identifier="io.github.cfunkz.pythonator",
        version=__version__,
        info_plist={
            "CFBundleName": "Pythonator",
            "CFBundleDisplayName": "Pythonator",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # Also turned off at run time; this keeps App Nap away before Python has started.
            "NSAppSleepDisabled": True,
        },
    )
