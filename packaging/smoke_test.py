"""Start a built Pythonator's background service, run one sandboxed service through it, and stop.

    python packaging/smoke_test.py dist/Pythonator/Pythonator

Proves the frozen app starts, its socket and token work, and its sandbox launcher runs a program. Standard library
only (it talks to the app with docs/examples/pyctl.py, the documented client).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docs" / "examples"))


def main(program: str) -> int:
    # The killed background service may hold its log open a moment longer: that mustn't hide the result.
    with tempfile.TemporaryDirectory(prefix="pythonator-smoke-", ignore_cleanup_errors=True) as tmp:
        home, project = Path(tmp, "data"), Path(tmp, "project")
        project.mkdir()
        (project / "hello.py").write_text("print('smoke test ok', flush=True)\n", encoding="utf-8")
        os.environ["PYTHONATOR_HOME"] = str(home)
        daemon = subprocess.Popen([program, "--daemon"])
        try:
            deadline = time.monotonic() + 60
            while not (home / "daemon.token").is_file():
                if time.monotonic() > deadline or daemon.poll() is not None:
                    print("the background service didn't start", file=sys.stderr)
                    return 1
                time.sleep(0.2)
            time.sleep(0.5)
            import pyctl  # reads PYTHONATOR_HOME

            app = pyctl.Pythonator()
            app.call("add", service={"name": "smoke", "folder": str(project), "entry": "hello.py",
                                     "runtime": "python", "restart": "never"})
            sid = app.call("snapshot")["services"][0]["id"]  # type: ignore[index]
            app.call("start", id=sid)
            for message in app.events():
                if message.get("ev") == "output" and "smoke test ok" in message["text"]:
                    print("smoke test ok: sandboxed service ran")
                    break
                if message.get("ev") == "state" and message.get("state") == "failed":
                    print(f"failed: {message.get('note')}\n{app.call('tail', id=sid)}", file=sys.stderr)
                    return 1
            # What an installer runs before an update: it must stop the background service.
            subprocess.run([program, "shutdown"], timeout=90, check=False)
            daemon.wait(30)
            print("smoke test ok: 'shutdown' stopped the background service")
            return 0
        finally:
            if daemon.poll() is None:
                daemon.kill()
                daemon.wait(30)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
