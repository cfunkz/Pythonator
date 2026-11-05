"""The frozen app's entry point (PyInstaller runs a script, not a package)."""

import sys

from pythonator.__main__ import main

sys.exit(main())
