"""``python -m pythonator``"""

from __future__ import annotations

import os
import sys


def main() -> int:
    args = sys.argv[1:]
    if any(a in ("-V", "--version") for a in args):
        from . import __version__  # noqa: PLC0415 - keep ``--version`` fast and Qt-free

        if sys.stdout is not None:  # a windowed build may have no stdout
            print(f"Pythonator {__version__}")
        return 0
    if "--home" in args:  # pythonator --home DIR: same as PYTHONATOR_HOME=DIR (used by "Start at login")
        i = args.index("--home")
        if i + 1 >= len(args):
            if sys.stderr is not None:
                print("--home needs a folder", file=sys.stderr)
            return 2
        os.environ["PYTHONATOR_HOME"] = args[i + 1]
        del sys.argv[i + 1 : i + 3]
    from .ui.app import run  # noqa: PLC0415

    return run()


if __name__ == "__main__":
    sys.exit(main())
