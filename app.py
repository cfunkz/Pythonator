"""Launcher kept for ``python app.py`` and PyInstaller builds. Prefer ``python -m pythonator``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pythonator.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
