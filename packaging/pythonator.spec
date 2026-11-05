# PyInstaller build: dist/Pythonator/ (Windows, Linux) or dist/Pythonator.app (macOS).
#   pip install --require-hashes -r packaging/requirements-build.lock && pip install --no-deps .
#   pyinstaller --noconfirm packaging/pythonator.spec
# ruff: noqa
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
sys.path.insert(0, str(ROOT))
from pythonator import __version__  # noqa: E402

ICON = str(ROOT / "pythonator" / "resources" / "icon.ico")
# Plugins are loaded at run time, so PyInstaller can't see what they import: bundle the standard library they may
# use, minus GUI toolkits and test suites.
_SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "pydoc_data", "antigravity",
         "this", "_tkinter", "msilib", "nis", "spwd", "crypt", "ossaudiodev"}
STDLIB = sorted(m for m in sys.stdlib_module_names if not m.startswith("_") and m not in _SKIP)

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "pythonator" / "resources"), "pythonator/resources"),
           (str(ROOT / "pythonator" / "plugins" / "official.json"), "pythonator/plugins"),
           (str(ROOT / "LICENSE"), "."),
           # The official plugins, checked against official.json at run time like in a source install.
           *[(str(p), str(p.parent.relative_to(ROOT))) for p in sorted((ROOT / "plugins").rglob("*"))
             if p.is_file() and not {"__pycache__", ".mypy_cache", ".pytest_cache"} & set(p.parts)]],
    hiddenimports=["pythonator.sdk", "pythonator.plugins.worker", "pythonator.launcher", *STDLIB],
    excludes=["tkinter", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml", "PyQt6.QtQuick",
              "PyQt6.QtMultimedia", "PyQt6.QtPdf", "PyQt6.QtBluetooth", "PyQt6.QtDesigner"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Pythonator", console=False, icon=ICON)  # noqa: F821
coll = COLLECT(exe, a.binaries, a.datas, name="Pythonator")  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll, name="Pythonator.app", icon=ICON, bundle_identifier="io.github.cfunkz.pythonator", version=__version__,
        info_plist={"CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
                    "LSApplicationCategoryType": "public.app-category.developer-tools",
                    "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
                    "NSRequiresAquaSystemAppearance": False, "NSAppSleepDisabled": True},
    )
