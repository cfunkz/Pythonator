"""Start a built Pythonator and check it keeps running (CI smoke test for the PyInstaller builds).

    python packaging/smoke_test.py dist/Pythonator/Pythonator
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    program = sys.argv[1]
    version = subprocess.run([program, "--version"], capture_output=True, text=True, timeout=60, check=False)
    print(f"--version: exit {version.returncode} {version.stdout.strip()}")
    if "unknown" in version.stdout:  # .env.example, which holds the version, didn't make it into the build
        print("The build doesn't know its version.")
        return 1
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "PYTHONATOR_HOME": home, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.Popen([program, "--minimized"], env=env)
        try:
            code = proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            print("The app started and kept running.")
            return 0
        print(f"The app exited early with code {code}.")
        log = Path(home, "logs", "pythonator.log")
        if log.exists():
            print(log.read_text(encoding="utf-8", errors="replace"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
