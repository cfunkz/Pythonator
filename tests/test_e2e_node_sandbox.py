"""End to end, on a real computer: a React (Vite) project made by the Project builder starts in the sandbox and
answers on its port, and a Node.js app there may start another program. Off by default (it downloads packages and
takes minutes); CI's node-sandbox job runs it with PYTHONATOR_E2E=1, on Windows, where Node.js is installed for all
users in C:\\Program Files as on most PCs.

Both hung in the Windows sandbox until 0.0.15: Node's pipe names (see ``private_node.PIPE_NAME``). When something
doesn't answer, it says where it is stuck:

* a watchdog preloaded into Node.js pauses the main thread from a worker (the inspector, no network) and prints
  its JavaScript stack, or says the main thread is in native code;
* the native stacks of each thread, from the Windows debugger (cdb, non-invasive) when the runner has it;
* the console's shell: which Node.js the sandbox finds, and whether it may start programs."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import urllib.request
from functools import partial
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


SPAWN = """
const { execFileSync } = require('child_process');
console.log('child says', execFileSync(process.execPath, ['-v']).toString().trim());
"""


def say(text: str) -> None:
    """Print whatever the programs wrote (box drawing, emoji) on a Windows console that can't show it."""
    sys.stdout.buffer.write(text.encode("utf-8", "replace") + b"\n")
    sys.stdout.flush()


def _get(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            return f"HTTP {r.status}"
    except Exception as exc:
        return str(exc)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def native_stacks(root_pid: int) -> str:
    """Each node.exe under ``root_pid``: its busiest threads, and every thread's native stack (cdb, when the
    computer has the Windows debugger)."""
    import psutil

    out = []
    cdbs = [str(p) for p in (Path(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"),
                             Path(r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe")) if p.is_file()]
    try:
        nodes = [p for p in psutil.Process(root_pid).children(recursive=True) if p.name().lower().startswith("node")]
    except psutil.Error as exc:
        return f"no process tree: {exc}"
    for proc in nodes:
        with contextlib.suppress(psutil.Error):
            busy = sorted(proc.threads(), key=lambda t: -(t.user_time + t.system_time))[:6]
            out.append(f"{proc.name()} pid {proc.pid}, {len(proc.threads())} threads; busiest: "
                       + ", ".join(f"tid {t.id} {t.user_time + t.system_time:.1f}s" for t in busy))
        if not cdbs:
            out.append("(no cdb.exe on this computer: no native stacks)")
            continue
        env = {**os.environ, "_NT_SYMBOL_PATH": r"srv*C:\symcache*https://msdl.microsoft.com/download/symbols"}
        try:
            done = subprocess.run([cdbs[0], "-pv", "-p", str(proc.pid), "-c", "!runaway 7; ~*kc 50; lm; q"],
                                  capture_output=True, text=True, errors="replace", timeout=400, env=env,
                                  check=False)
            out.append(done.stdout[-60000:] + done.stderr[-2000:])
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(f"cdb failed: {exc}")
    return "\n".join(out)


def make_reachable(h: Any, sup: Any, sid: str, port: int) -> str:
    """Windows: what Make reachable does, without the administrator prompt (CI runners are administrators). A
    sandboxed server is out of reach of this PC until then, and its start says so."""
    from pythonator import launcher
    from pythonator.isolation import windows

    before = windows.loopback_exempt(sid)
    h.wait(lambda: "Make reachable" in sup.tail(sid) or sup.slots[sid].state.value in ("failed", "stopped"),
           timeout=60_000)
    noted = "Make reachable" in sup.tail(sid)
    container = windows.container_name(sid)
    sandbox_sid, _folder = launcher.windows_profile(container)
    script = windows.reach_script(sandbox_sid, container, [port], lan=False)
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", windows.encoded(script)], capture_output=True, text=True,
                          errors="replace", timeout=180, check=False)
    say(f"===== make reachable: exit {done.returncode} =====\n{done.stdout}{done.stderr}")
    after = windows.loopback_exempt(sid)
    if before is not False or after is not True or done.returncode != 0:
        return f"exempt before {before}, after {after}, script exit {done.returncode}"
    return "noted, made reachable" if noted else "made reachable, but the start didn't say it was needed"


def test_a_vite_app_answers_in_the_sandbox(dirs: Any, qtbot: Any, tmp_path: Path) -> None:
    from pythonator.core import runtimes
    from tests.conftest import DaemonHarness

    say(f"node on PATH: {runtimes.which('node')}")
    h = DaemonHarness(dirs, qtbot)
    sup = h.daemon.sup
    answers: dict[str, str] = {}
    logs: dict[str, str] = {}
    reached: dict[str, str] = {}
    try:
        parent = tmp_path / "projects"
        made = sup.create_project("react", "e2e-react", parent, start=False)
        h.wait(lambda: sup.slots[made.id].state.value in ("stopped", "failed"), timeout=900_000)
        assert sup.slots[made.id].state.value == "stopped", sup.tail(made.id)[-6000:]
        folder = parent / "e2e-react"
        for name, text in (("watchdog.cjs", WATCHDOG), ("spawn.cjs", SPAWN)):
            (folder / name).write_text(text, encoding="utf-8")
        diag = f'--require "{(folder / "watchdog.cjs").as_posix()}"'
        vite = "node_modules/vite/bin/vite.js"
        for name, entry in (("starts a program", "spawn.cjs"), ("vite", vite)):
            port = _free_port()
            web = entry == vite
            args = ["--host", "127.0.0.1", "--port", str(port), "--strictPort"] if web else []
            sid = h.add(name=f"v{len(answers)}", folder=str(folder), runtime="node", entry=entry, args=args,
                        mode="sandbox", own_env=True, restart="never", env={"NODE_OPTIONS": diag},
                        network={"ports": [port] if web else []})
            sup.start(sid, confirmed=True)
            if web and os.name == "nt":
                reached[name] = make_reachable(h, sup, sid, port)
            started = time.monotonic()
            answer = ""
            stacks = ""
            while time.monotonic() - started < 240:  # the first start copies Node.js for sandboxes
                h.qtbot.wait(2000)
                slot = sup.slots[sid]
                if web:
                    answer = _get(port)
                    if answer.startswith("HTTP 200"):
                        break
                if slot.state.value in ("failed", "stopped"):
                    answer = f"exited: {slot.state.value} {slot.exit_code}"
                    break
                if (not stacks and "Started" in sup.tail(sid) and time.monotonic() - started > 60
                        and slot.runner is not None and slot.runner.pid):
                    stacks = native_stacks(slot.runner.pid)
                    break
            answers[name] = answer or "still running, no answer"
            logs[name] = sup.tail(sid)
            say(f"\n===== {name}: {answers[name]} =====\n{logs[name]}")
            if stacks:
                say(f"===== {name}: native stacks =====\n{stacks}")
            sup.stop(sid)
            h.wait(partial(lambda s: sup.slots[s].runner is None, sid), timeout=30_000)
            sup.remove(sid)
            h.qtbot.wait(1000)

        checks = ["where node", "node -v", f'node "{folder / "spawn.cjs"}"']
        for line in checks:
            sup.clear(made.id)
            sup.shell(made.id, line)
            h.wait(lambda: sup.slots[made.id].shell is None, timeout=60_000)
            say(f"===== shell: {line} =====\n{sup.tail(made.id)}")
        say("===== summary =====\n" + "\n".join(f"{k}: {v}" for k, v in answers.items()))
        assert answers["starts a program"] == "exited: stopped 0", "Node.js couldn't start a program: see above"
        assert "child says v" in logs["starts a program"]
        assert answers["vite"].startswith("HTTP 200"), "the Vite app didn't answer in the sandbox: see above"
        if os.name == "nt":  # the fixed copy of Node.js ran, not the one in C:\\Program Files
            assert "runtimes" in logs["vite"].split("Started", 1)[1].splitlines()[0]
            assert reached["vite"] == "noted, made reachable", reached
    finally:
        for sid in list(sup.slots):
            sup.stop(sid)
        h.qtbot.wait(2000)
        h.close()
