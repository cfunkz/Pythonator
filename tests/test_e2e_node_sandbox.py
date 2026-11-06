"""End to end, on a real computer: a React (Vite) project made by the Project builder starts in the sandbox and
answers on its port. Off by default (it downloads packages and takes minutes); CI's node-sandbox job runs it with
PYTHONATOR_E2E=1, on Windows, where Node.js is installed for all users in C:\\Program Files as on most PCs.

When the server doesn't answer, it says where it is stuck, from several sides:

* a watchdog preloaded into Node.js pauses the main thread from a worker (the inspector, no network) and prints
  its JavaScript stack, or says the main thread is in native code;
* a trace of each module, import and native addon (``process.dlopen``) Node.js loads, with the time;
* the native stacks of each thread, from the Windows debugger (cdb, non-invasive) when the runner has it;
* the same app with variations: only loading Vite's bundler (rolldown), Vite's other config loader, and Node.js
  without each part of Pythonator's sandbox options;
* the console's shell: which Node.js the sandbox finds, whether it may start it, the folders' permissions."""

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


TRACE = r"""
const fs = require('fs'); const Module = require('module');
if (!globalThis.__pythonatorTrace) {
  globalThis.__pythonatorTrace = true;
  const t0 = Date.now();
  const log = (t) => fs.writeSync(2, 'TRACE +' + (Date.now() - t0) + 'ms ' + t + '\n');
  const dlopen = process.dlopen;
  process.dlopen = function (mod, file, ...rest) {
    log('dlopen ' + file);
    const out = dlopen.call(this, mod, file, ...rest);
    log('dlopen done ' + file);
    return out;
  };
  const load = Module._load;
  Module._load = function (request, parent, isMain) {
    if (!Module.isBuiltin(request)) log('require ' + request);
    return load.apply(this, arguments);
  };
  if (typeof Module.registerHooks === 'function') {
    Module.registerHooks({ resolve(spec, ctx, next) {
      const r = next(spec, ctx);
      if (!r.url.startsWith('node:')) log('import ' + r.url);
      return r;
    } });
  }
  log('trace on: node ' + process.version + ', NODE_OPTIONS=' + process.env.NODE_OPTIONS);
}
"""

PROBE = """
console.log('PROBE start');
const t = Date.now();
const r = await import('rolldown');
console.log('PROBE rolldown loaded in', Date.now() - t, 'ms');
const bundle = await r.rolldown({ input: './probe-in.js' });
await bundle.generate({ format: 'esm' });
console.log('PROBE build ok in', Date.now() - t, 'ms');
process.exit(0);
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


def test_a_vite_app_answers_in_the_sandbox(dirs: Any, qtbot: Any, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.core import runtimes
    from pythonator.isolation import windows
    from tests.conftest import DaemonHarness

    say(f"node on PATH: {runtimes.which('node')}")
    h = DaemonHarness(dirs, qtbot)
    sup = h.daemon.sup
    answers: dict[str, str] = {}
    try:
        parent = tmp_path / "projects"
        made = sup.create_project("react", "e2e-react", parent, start=False)
        h.wait(lambda: sup.slots[made.id].state.value in ("stopped", "failed"), timeout=900_000)
        assert sup.slots[made.id].state.value == "stopped", sup.tail(made.id)[-6000:]
        folder = parent / "e2e-react"
        for name, text in (("watchdog.cjs", WATCHDOG), ("trace.cjs", TRACE), ("probe.mjs", PROBE),
                           ("probe-in.js", "export default 1;\n")):
            (folder / name).write_text(text, encoding="utf-8")
        diag = f'--require "{(folder / "trace.cjs").as_posix()}" --require "{(folder / "watchdog.cjs").as_posix()}"'
        real_node_env = windows.node_env

        def flags_only(env: dict[str, str], shim: Path, roots: Any) -> dict[str, str]:
            return {"NODE_OPTIONS": f"--preserve-symlinks --preserve-symlinks-main {env.get('NODE_OPTIONS', '')}"}

        def nothing(env: dict[str, str], shim: Path, roots: Any) -> dict[str, str]:
            return {}

        vite = "node_modules/vite/bin/vite.js"
        variants = [  # name, entry, extra args, what the sandbox adds to Node.js
            ("rolldown only", "probe.mjs", [], real_node_env),
            ("vite", vite, [], real_node_env),
            ("vite, config loader runner", vite, ["--configLoader", "runner"], real_node_env),
            ("vite, no shim (flags only)", vite, [], flags_only),
            ("vite, no Pythonator options", vite, [], nothing),
            ("rolldown only, no Pythonator options", "probe.mjs", [], nothing),
        ]
        for name, entry, extra, node_env in variants:
            monkeypatch.setattr(windows, "node_env", node_env)
            port = _free_port()
            web = entry == vite
            args = ["--host", "127.0.0.1", "--port", str(port), "--strictPort", *extra] if web else []
            sid = h.add(name=f"v{len(answers)}", folder=str(folder), runtime="node", entry=entry, args=args,
                        mode="sandbox", own_env=True, restart="never",
                        env={"NODE_OPTIONS": diag, "DEBUG": "vite:*" if web else ""},
                        network={"ports": [port] if web else []})
            sup.start(sid, confirmed=True)
            started = time.monotonic()
            answer = ""
            stacks = ""
            while time.monotonic() - started < 50:
                h.qtbot.wait(2000)
                slot = sup.slots[sid]
                if web:
                    answer = _get(port)
                    if answer.startswith("HTTP 200"):
                        break
                if slot.state.value in ("failed", "stopped"):
                    answer = answer or f"exited: {slot.state.value} {slot.exit_code}"
                    break
                if not stacks and time.monotonic() - started > 42 and slot.runner is not None and slot.runner.pid:
                    stacks = native_stacks(slot.runner.pid)
            answers[name] = answer or "still running, no answer"
            say(f"\n===== {name}: {answers[name]} =====\n{sup.tail(sid)}")
            if stacks:
                say(f"===== {name}: native stacks =====\n{stacks}")
            sup.stop(sid)
            h.wait(partial(lambda s: sup.slots[s].runner is None, sid), timeout=30_000)
            sup.remove(sid)
            h.qtbot.wait(1000)
        monkeypatch.setattr(windows, "node_env", real_node_env)

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
            say(f"===== shell: {line} =====\n{sup.tail(made.id)}")
        say("===== summary =====\n" + "\n".join(f"{k}: {v}" for k, v in answers.items()))
        assert answers["vite"].startswith("HTTP 200"), "the Vite app didn't answer in the sandbox: see above"
    finally:
        for sid in list(sup.slots):
            sup.stop(sid)
        h.qtbot.wait(2000)
        h.close()
