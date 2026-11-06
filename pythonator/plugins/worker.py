"""The plugin side of a plugin process: load the plugin, then answer the host's messages. Standard library only.

Started as ``Pythonator --plugin-worker <folder> <data folder>`` inside the plugin's sandbox. The protocol runs on
the real stdout; ``print()`` and stderr go to the plugin's log. A handler that raises is reported and the plugin
keeps running; the host decides when a plugin misbehaves enough to stop it.
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import os
import queue
import sys
import tempfile
import threading
import tomllib
import traceback
from pathlib import Path
from typing import Any

from .. import sdk


def _load(folder: Path) -> sdk.Plugin:
    data = tomllib.loads((folder / "plugin.toml").read_text(encoding="utf-8"))
    meta = data.get("plugin", data)
    entry = (folder / str(meta.get("entry", "plugin.py"))).resolve()
    if not entry.is_relative_to(folder.resolve()):
        raise ImportError("the entry file is outside the plugin")
    # Never run bytecode a plugin ships: compile from source into a private cache.
    sys.dont_write_bytecode = True
    sys.pycache_prefix = tempfile.mkdtemp(prefix="pythonator-pyc-")
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(f"pythonator_plugin_{meta.get('id', 'x').replace('-', '_')}", entry)
    if spec is None or spec.loader is None:
        raise ImportError(f"can't load {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    classes = [c for _, c in inspect.getmembers(module, inspect.isclass)
               if issubclass(c, sdk.Plugin) and c is not sdk.Plugin and c.__module__ == module.__name__]
    if len(classes) != 1:
        raise ImportError(f"{entry.name} must define exactly one Plugin subclass (found {len(classes)})")
    return classes[0]()


def _services(items: list[dict[str, Any]]) -> dict[str, sdk.ServiceInfo]:
    return {d["id"]: sdk.ServiceInfo.of(d) for d in items if isinstance(d, dict) and "id" in d}


def _call(name: str, fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:
        sdk._send({"t": "error", "where": name, "text": traceback.format_exc(limit=8)[-4000:]})
        return None


def _read_stdin(inbox: queue.Queue[dict[str, Any] | None]) -> None:
    """Replies to requests go straight to the thread waiting for them, so a handler can wait for one while the
    main loop is busy calling it; everything else is queued for the main loop, in order."""
    for raw in sys.stdin.buffer:  # bytes: the host writes UTF-8, whatever this computer's code page is
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("t") == "reply":
            sdk._deliver(msg)
        else:
            inbox.put(msg)
    inbox.put(None)


def main(args: list[str]) -> int:
    try:
        return _main(args)
    except (BrokenPipeError, ConnectionError, OSError, ValueError):
        sdk._gone()  # the pipe to Pythonator closed under us: nothing left to report to
        return 0


def _main(args: list[str]) -> int:
    if len(args) < 2:
        return 2
    folder, data_dir = Path(args[0]), Path(args[1])
    proto = io.TextIOWrapper(os.fdopen(os.dup(1), "wb", buffering=0), encoding="utf-8", write_through=True)
    sdk._out = proto
    sys.stdout = sys.stderr  # print() goes to the plugin's log
    try:
        plugin = _load(folder)
    except Exception:
        sdk._send({"t": "error", "where": "load", "text": traceback.format_exc(limit=8)[-4000:]})
        return 3
    plugin.data_dir = data_dir
    inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
    threading.Thread(target=_read_stdin, args=(inbox,), daemon=True, name="pythonator-inbox").start()
    sdk._send({"t": "hello"})
    while (msg := inbox.get()) is not None:
        kind = msg.get("t")
        if kind == "init":
            plugin.settings = dict(msg.get("settings", {}))
            plugin.permissions = frozenset(msg.get("permissions", []))
            plugin.test_mode = bool(msg.get("test_mode"))
            plugin.services = _services(msg.get("services", []))
            _call("on_start", plugin.on_start)
        elif kind == "event":
            service = sdk.ServiceInfo.of(msg["service"])
            plugin.services[service.id] = service
            event = sdk.ServiceEvent(str(msg.get("kind", "")), service, msg.get("exit"), str(msg.get("note", "")))
            _call("on_event", plugin.on_event, event)
        elif kind == "services":
            plugin.services = _services(msg.get("services", []))
        elif kind == "output" and msg.get("id") in plugin.services:
            _call("on_output", plugin.on_output, plugin.services[msg["id"]], list(msg.get("lines", [])))
        elif kind == "stats":
            plugin.services = _services(msg.get("services", []))
            plugin.system = sdk.SystemInfo.of(msg.get("system") or {})
            _call("on_stats", plugin.on_stats)
        elif kind == "check":
            verdict = _call("check_start", plugin.check_start, sdk.ServiceInfo.of(msg["service"]))
            sdk._send({"t": "verdict", "req": msg.get("req", 0), "deny": str(verdict or "")[:500]})
        elif kind == "settings":
            plugin.settings = dict(msg.get("settings", {}))
            _call("on_settings", plugin.on_settings)
        elif kind == "action":
            _call("on_action", plugin.on_action, str(msg.get("action", "")), str(msg.get("value", "")))
        elif kind == "tick":
            _call("on_tick", plugin.on_tick)
        elif kind == "proposal":
            _call("on_proposal", plugin.on_proposal, str(msg.get("id", "")), str(msg.get("status", "")),
                  str(msg.get("note", "")))
        elif kind == "stop":
            _call("on_stop", plugin.on_stop)
            break
    return 0
