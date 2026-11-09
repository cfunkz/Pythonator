"""Pythonator plugin SDK. Standard library only; runs inside the plugin's jail.

    from pythonator_sdk import plugin

    @plugin.on("service.state")          # events you have permission for
    def changed(event): ...

    @plugin.hook("policy.check")         # needs the policy.check permission
    def review(change):
        return {"allow": True}

    plugin.call("services.list")         # any host call; refused calls raise PermissionError
    plugin.notify("Backup finished")     # needs ui.notify
    plugin.request("https://api.example.com/v1/x", body="...")   # needs net.connect for that host

Talks to Pythonator with one JSON object per line on stdin/stdout. print() goes to the plugin's log, not the
protocol. Run it as: python pythonator_sdk.py main.py (Pythonator does this for you, inside the jail).
"""

import json
import os
import runpy
import sys
import threading
import traceback

_PROTO_OUT = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
_PROTO_IN = os.fdopen(os.dup(0), "r", encoding="utf-8")
sys.stdout = sys.stderr  # print() is logging; the protocol has its own descriptor


class Plugin:
    def __init__(self):
        self._handlers = {}
        self._hooks = {}
        self._next = 1
        self._queue = []
        self.settings = {}
        self._lock = threading.Lock()
        self._waiters = {}
        self._loop_thread = None

    # decorators -----------------------------------------------------------------------------------------------------
    def on(self, event):
        def deco(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn

        return deco

    def hook(self, name):
        def deco(fn):
            self._hooks[name] = fn
            return fn

        return deco

    # calls ----------------------------------------------------------------------------------------------------------
    def _send(self, obj):
        with self._lock:
            _PROTO_OUT.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def _route(self, msg):
        """A response for a call waiting on another thread: hand it over. True when consumed."""
        w = self._waiters.pop(msg.get("id"), None) if "method" not in msg else None
        if w is None:
            return False
        w[1] = msg
        w[0].set()
        return True

    def call(self, method, /, **params):
        """Ask Pythonator for something. Safe from any thread: calls from your own threads wait for the main loop
        to hand them their answer."""
        with self._lock:
            rid = self._next
            self._next += 1
        loop = self._loop_thread
        if loop is not None and threading.current_thread() is not loop:
            w = [threading.Event(), None]
            self._waiters[rid] = w
            self._send({"id": rid, "method": method, "params": params})
            w[0].wait()
            msg = w[1]
        else:
            self._send({"id": rid, "method": method, "params": params})
            while True:
                line = _PROTO_IN.readline()
                if not line:
                    raise SystemExit(0)
                msg = json.loads(line)
                if msg.get("id") == rid and "method" not in msg:
                    break
                if not self._route(msg):
                    self._queue.append(msg)
        if "error" in msg:
            err = msg["error"]
            if err.get("code") == "PLG-PERM":
                raise PermissionError(err.get("message", "not granted"))
            raise RuntimeError(err.get("message", "host error"))
        return msg.get("result")

    def notify(self, text, level="info"):
        return self.call("ui.notify", text=str(text)[:500], level=level)

    def view(self, service, rows):
        return self.call("ui.view", service=service, rows=rows)

    def status(self, text):
        """One short text in the window's status bar (needs ui.statusbar)."""
        return self.call("ui.status", text=str(text)[:60])

    def probe(self, service, port=None, kind="tcp", path="/"):
        """TCP connect or HTTP GET to one of a service's own ports, made by Pythonator (needs net.service).
        Returns {ok, detail, ms}."""
        return self.call("services.probe", service=service, port=port, kind=kind, path=path)

    def request(self, url, method="POST", headers=None, body="", timeout=60):
        """HTTPS through Pythonator, to a host you were granted. Put {secret:NAME} in a header for a key you
        declared; Pythonator fills it in on the way out. Returns {status, headers, body}."""
        return self.call("net.request", url=url, method=method, headers=headers or {}, body=body, timeout=timeout)

    # loop -----------------------------------------------------------------------------------------------------------
    def _dispatch(self, msg):
        method = msg.get("method")
        if method == "event":
            for fn in self._handlers.get(msg["params"].get("topic"), []):
                try:
                    fn(msg["params"].get("data", {}))
                except Exception:
                    traceback.print_exc()
            return
        if method == "hello":
            self.settings = msg["params"].get("settings", {})
            self._send({"id": msg["id"], "result": {"hooks": sorted(self._hooks), "events": sorted(self._handlers)}})
            return
        fn = self._hooks.get(method)
        if fn is None:
            self._send({"id": msg.get("id"), "error": {"code": "PLG-NOHOOK", "message": f"no hook {method}"}})
            return
        try:
            self._send({"id": msg.get("id"), "result": fn(msg.get("params", {}))})
        except Exception as e:
            traceback.print_exc()
            self._send({"id": msg.get("id"), "error": {"code": "PLG-HOOK", "message": repr(e)[:300]}})

    def run(self):
        self._loop_thread = threading.current_thread()
        while True:
            while self._queue:
                self._dispatch(self._queue.pop(0))
            line = _PROTO_IN.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not self._route(msg):
                self._dispatch(msg)


plugin = Plugin()

if __name__ == "__main__":
    sys.modules["pythonator_sdk"] = sys.modules["__main__"]
    entry = sys.argv[1]
    sys.path.insert(0, os.path.dirname(entry))
    runpy.run_path(entry, run_name="plugin")
    plugin.run()
