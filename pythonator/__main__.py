"""Entry point. Internal helper modes are dispatched before Qt widgets are imported, so helpers start fast."""

from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    flag = args[0] if args else ""
    if flag == "--sandbox-exec":  # the stdlib-only sandbox launcher
        from .launcher import main as launcher_main

        sys.exit(launcher_main(args[1:]))
    if flag == "--send-ctrl-c":  # Windows: stop a service politely
        from .daemon.winproc import helper_main

        sys.exit(helper_main(args[1:]))
    if flag == "--plugin-worker":
        from .plugins.worker import main as worker_main

        sys.exit(worker_main(args[1:]))
    if flag == "--daemon":
        from .daemon.server import main as daemon_main

        sys.exit(daemon_main())
    if flag in ("shutdown", "--uninstall"):  # the installers: stop the background service (and clean up)
        from .daemon.installer import main as installer_main

        sys.exit(installer_main(flag))
    if flag in ("--version", "-V"):
        from . import __version__

        print(f"Pythonator {__version__}")
        return 0
    from .ui.app import main as ui_main

    return ui_main(args)


if __name__ == "__main__":
    sys.exit(main())
