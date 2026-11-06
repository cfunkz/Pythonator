"""End to end, on a real computer: a React (Vite) project made by the Project builder starts in the sandbox and
answers on its port. Off by default (it downloads packages and takes minutes); CI's node-sandbox job runs it with
PYTHONATOR_E2E=1, on Windows, where Node.js is installed for all users in C:\\Program Files as on most PCs.

When the server doesn't answer, it says where it is stuck: a watchdog preloaded into Node.js pauses the main
thread from a worker (the inspector, no network) and prints its JavaScript stack, and the console's shell runs a
few checks next to it (which Node.js the sandbox finds, whether it may start it, the folders' permissions)."""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("PYTHONATOR_E2E") != "1", reason="set PYTHONATOR_E2E=1 to run it")

# Three samples of the main thread's stack, 20, 30 and 40 seconds in. A pause that gets no answer means the main
# thread is in native code (an addon), not JavaScript.
WATCHDOG = r"""
const { isMainThread, Worker } = require('worker_threads');
if (isMainThread && !globalThis.__pythonatorWatchdog) {
  globalThis.__pythonatorWatchdog = true;
  const code = `
    const inspector = require('inspector'); const fs = require('fs'); const path = require('path');
    const log = path.join(process.cwd(), 'watchdog.txt');
    const out = (t) => { fs.writeSync(2, t + '\\n'); fs.appendFileSync(log, t + '\\n'); };
    const s = new inspector.Session(); s.connectToMainThread();
    let answered = 0; const urls = new Map();
    s.on('Debugger.scriptParsed', (m) => urls.set(m.params.scriptId, m.params.url));
    s.on('Debugger.paused', (m) => {
      answered++;
      const at = (f) => '  at ' + (f.functionName || '<anonymous>') + ' ' +
        (f.url || urls.get(f.location.scriptId) || '?') + ':' + (f.location.lineNumber + 1) + ':' +
        (f.location.columnNumber + 1);
      out('WATCHDOG stack ' + answered + ' (' + m.params.reason + '):\\n' +
        m.params.callFrames.slice(0, 40).map(at).join('\\n'));
      s.post('Debugger.resume');
    });
    s.post('Debugger.enable', (err) => out('WATCHDOG on' + (err ? ': ' + err.message : '')));
    for (const [i, ms] of [[1, 20000], [2, 30000], [3, 40000]]) {
      setTimeout(() => { out('WATCHDOG pausing ' + i); s.post('Debugger.pause'); }, ms);
      const late = () => {
        if (answered < i) out('WATCHDOG no answer to pause ' + i + ': the main thread is in native code');
      };
      setTimeout(late, ms + 4000);
    }
  `;
  new Worker(code, { eval: true }).unref();
}
"""


def _get(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            return f"HTTP {r.status}"
    except Exception as exc:
        return str(exc)


def test_a_vite_app_answers_in_the_sandbox(dirs: Any, qtbot: Any, tmp_path: Path) -> None:
    from pythonator.core import runtimes
    from tests.conftest import DaemonHarness

    print("node on PATH:", runtimes.which("node"))
    h = DaemonHarness(dirs, qtbot)
    sup = h.daemon.sup
    try:
        parent = tmp_path / "projects"
        made = sup.create_project("react", "e2e-react", parent, start=False)
        h.wait(lambda: sup.slots[made.id].state.value in ("stopped", "failed"), timeout=900_000)
        assert sup.slots[made.id].state.value == "stopped", sup.tail(made.id)[-6000:]
        folder = parent / "e2e-react"
        (folder / "watchdog.cjs").write_text(WATCHDOG, encoding="utf-8")
        service = sup.slots[made.id].service
        sup.update(service.model_copy(update={"env": {
            **service.env, "NODE_OPTIONS": f'--require "{(folder / "watchdog.cjs").as_posix()}"'}}))
        sup.start(made.id, confirmed=True)
        port = sup.slots[made.id].service.network.ports[0]
        answer = ""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            h.qtbot.wait(2000)
            answer = _get(port)
            if answer.startswith("HTTP 200") or sup.slots[made.id].state.value in ("failed", "stopped"):
                break
        h.qtbot.wait(25_000 if not answer.startswith("HTTP 200") else 0)  # the watchdog's later samples
        print(f"\n===== the service: {answer} =====\n{sup.tail(made.id)}")
        if (folder / "watchdog.txt").is_file():
            print("===== watchdog.txt =====\n" + (folder / "watchdog.txt").read_text(encoding="utf-8"))
        sup.stop(made.id)
        h.wait(lambda: sup.slots[made.id].runner is None, timeout=30_000)

        private = sorted((dirs.root / "runtimes").glob("node-*"))
        node = str(private[-1] / "node.exe") if private else "node"
        checks = ["where node", "node -v", f'"{node}" -v', f'"{node}" -e "console.log(process.execPath)"',
                  f'icacls "{node}"', f'icacls "{folder}"',
                  "echo PATH=%PATH% NODE_OPTIONS=%NODE_OPTIONS% TEMP=%TEMP% USERPROFILE=%USERPROFILE%"]
        if os.name == "nt":
            checks.insert(0, f'"{os.environ.get("SYSTEMROOT", "C:/Windows")}\\System32\\whoami.exe" /groups')
        for line in checks:
            sup.clear(made.id)
            sup.shell(made.id, line)
            h.wait(lambda: sup.slots[made.id].shell is None, timeout=60_000)
            print(f"===== shell: {line} =====\n{sup.tail(made.id)}")
        assert answer.startswith("HTTP 200"), "the Vite app didn't answer in the sandbox: see the output above"
    finally:
        for sid in list(sup.slots):
            sup.stop(sid)
        h.qtbot.wait(2000)
        h.close()
