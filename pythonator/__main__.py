"""`python -m pythonator` opens the window; `python -m pythonator ctl uninstall` / `version` is pythonatorctl."""

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "ctl":
        from pythonator.ctl.main import main as ctl

        sys.exit(ctl(sys.argv[2:]))
    from pythonator.ui.app import main as ui

    sys.exit(ui())

if __name__ == "__main__":
    main()
