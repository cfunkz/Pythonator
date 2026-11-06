"""The logic of the three official plugins, without a process around them."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from pythonator import sdk
from pythonator.paths import official_plugins_dir


def plugin_module(name: str) -> ModuleType:
    path = official_plugins_dir() / name / "plugin.py"
    spec = importlib.util.spec_from_file_location(f"official_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    monkeypatch.setattr(sdk, "_send", out.append)
    return out


def info(**kw: Any) -> sdk.ServiceInfo:
    base = {"id": "a1", "name": "api", "runtime": "python", "mode": "sandbox", "state": "running"}
    return sdk.ServiceInfo.of({**base, **kw})


def test_guard_rules(sent: list[dict[str, Any]]) -> None:
    guard = plugin_module("guard")
    assert guard.parse_ranges("3000-3999, 443, junk, 9-1") == [(3000, 3999), (443, 443)]
    g = guard.Guard(settings={"block_full": True, "block_lan": True, "allowed_ports": "8000-8100",
                              "max_memory_mb": 512})
    g.on_start()
    assert g.check_start(info(mode="full"))
    assert g.check_start(info(ports=[8000], expose="lan", network=True))
    assert g.check_start(info(ports=[22], network=True))
    assert g.check_start(info(memory_limit_mb=4096))
    assert g.check_start(info(ports=[8050], network=True)) is None
    assert any(m.get("t") == "panel" for m in sent)


def test_guard_acts_on_the_service_overloading_the_computer(sent: list[dict[str, Any]]) -> None:
    guard = plugin_module("guard")
    g = guard.Guard(settings={"system_cpu": 90, "system_ram": 0, "action": "stop"})
    g.on_start()
    gb = 1024 ** 3
    g.system = sdk.SystemInfo(cpu=99, memory=50, memory_total=16 * gb, cores=8)
    g.services = {"a1": info(id="a1", cpu=700.0), "b2": info(id="b2", name="web", cpu=20.0)}
    for _ in range(guard.OVERLOAD_SAMPLES - 1):
        g.on_stats()
    assert not [m for m in sent if m.get("t") == "control"], "not before 30 s"
    g.on_stats()
    assert [m for m in sent if m.get("t") == "control"] == [{"t": "control", "op": "stop", "id": "a1"}]
    # Busy because of something else: the biggest service uses little of it, so nothing is stopped.
    sent.clear()
    g.services = {"b2": info(id="b2", name="web", cpu=40.0)}
    for _ in range(guard.OVERLOAD_SAMPLES * 2):
        g.on_stats()
    assert not [m for m in sent if m.get("t") == "control"]
    assert guard.heaviest([info(memory=4 * gb)], sdk.SystemInfo(memory=95, memory_total=16 * gb), "memory")
    assert not guard.heaviest([info(memory=gb)], sdk.SystemInfo(memory=95, memory_total=16 * gb), "memory")
    assert sdk.SystemInfo.of({"cpu": "x"}) == sdk.SystemInfo()


def test_webhook_payloads_cannot_ping_everyone() -> None:
    hooks = plugin_module("notify")
    body, kind = hooks.payload("https://discord.com/api/webhooks/1/x", "api crashed", "@everyone look")
    data = json.loads(body)
    assert data["allowed_mentions"] == {"parse": []} and kind == "application/json"
    assert json.loads(hooks.payload("https://hooks.slack.com/services/x", "t", "b")[0])["text"].startswith("*t*")
    assert hooks.payload("https://ntfy.sh/topic", "t", "b")[1].startswith("text/plain")
    assert hooks.allowed("https://example.com/hook")
    assert hooks.allowed("http://127.0.0.1:9000/hook")
    for bad in ("http://example.com/hook", "file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"):
        assert not hooks.allowed(bad)


def test_health_probe_against_a_real_server(tmp_path: Path) -> None:
    import http.server
    import threading

    notify = plugin_module("notify")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, ms = notify.probe(server.server_address[1], "/", 2.0)
        assert status.startswith(("200", "OK", "HTTP")) or "200" in status, status
        assert ms >= 0
    finally:
        server.shutdown()


def test_health_asks_the_port_the_server_really_listens_on() -> None:
    notify = plugin_module("notify")
    assert notify.target_port(info(ports=[8764], listening=[8000])) == 8000  # started with --port 8000
    assert notify.target_port(info(ports=[8764], listening=[8764, 9000])) == 8764
    assert notify.target_port(info(ports=[8764])) == 8764  # not listening yet
    assert notify.target_port(info(listening=[5000])) == 5000  # no port set up
    assert notify.target_port(info()) == 0


def notify_plugin(tmp_path: Path, **settings: Any) -> Any:
    notify = plugin_module("notify")
    plugin = notify.Notify(settings=settings, permissions=frozenset({"ui", "inspect", "read_logs"}),
                           data_dir=tmp_path)
    plugin.on_start()
    plugin.services = {"a1": info(), "b2": info(id="b2", name="web")}
    return plugin


def test_notify_tracks_crashes_recoveries_and_blocks(sent: list[dict[str, Any]], tmp_path: Path) -> None:
    plugin = notify_plugin(tmp_path)
    plugin.on_event(sdk.ServiceEvent("crashed", info(), 1, "Restarting"))
    plugin.on_event(sdk.ServiceEvent("started", info()))
    plugin.on_event(sdk.ServiceEvent("blocked", info(id="b2", name="web", state="failed"), None, "Guard: port 22"))
    assert [e.kind for e in plugin.events] == ["crashed", "recovered", "blocked"]
    notices = [m["text"] for m in sent if m.get("t") == "notify"]
    assert notices == ["api crashed: exit code 1 · Restarting", "api is running again",
                       "web was blocked from starting: Guard: port 22"]
    panel = [m for m in sent if m.get("t") == "panel"][-1]["panel"]
    assert panel["title"] == "Notify"
    plugin.on_tick()  # the timeline is kept for the next start
    assert [e.kind for e in notify_plugin(tmp_path).events] == ["crashed", "recovered", "blocked"]


def test_notify_watches_only_the_chosen_services(sent: list[dict[str, Any]], tmp_path: Path) -> None:
    plugin = notify_plugin(tmp_path, services="web\n")
    plugin.on_event(sdk.ServiceEvent("crashed", info(), 1))
    plugin.on_event(sdk.ServiceEvent("crashed", info(id="b2", name="web"), 1))
    assert [m["text"] for m in sent if m.get("t") == "notify"] == ["web crashed: exit code 1"]
    assert len(plugin.events) == 2  # the timeline still has both
    plugin.view = "web"
    plugin.panel(list(plugin.events))
    plugin.on_action("mute", "")
    plugin.on_event(sdk.ServiceEvent("crashed", info(id="b2", name="web"), 1))
    assert len([m for m in sent if m.get("t") == "notify"]) == 1


def test_notify_spots_anomalies(sent: list[dict[str, Any]], tmp_path: Path) -> None:
    notify = plugin_module("notify")
    mb = 2 ** 20
    leak = [(t * 10.0, 5.0, (200 + t * 2) * mb) for t in range(61)]  # 200 MB to 320 MB in ten minutes
    assert "grew from" in notify.memory_grew(leak, 50)
    assert not notify.memory_grew(leak, 80)
    spike = [(t * 10.0, 5.0, (200 if t != 30 else 900) * mb) for t in range(61)]  # one big allocation, freed
    assert not notify.memory_grew(spike, 50)
    busy = [(t * 2.0, 99.0, mb) for t in range(160)]
    assert "CPU over 95%" in notify.cpu_stuck(busy, 95)
    assert not notify.cpu_stuck([*busy[:-1], (busy[-1][0], 20.0, mb)], 95)
    assert notify.count_errors(["ok", "Traceback (most recent call last):", "ERROR db down", "errors=0x"]) == 2
    plugin = notify_plugin(tmp_path, restart_loop=3, error_burst=3)
    for _ in range(3):
        plugin.on_event(sdk.ServiceEvent("crashed", info(), 1))
    plugin.on_output(info(), ["Error: one", "error two", "fatal three"])
    anomalies = [e.detail for e in plugin.events if e.kind == "anomaly"]
    assert anomalies == ["crashed 3 times in 10 minutes", "3 error lines in a minute"]


def test_builder_only_asks_for_templates(sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_

    builder = plugin_module("builder")
    assert builder.suggest("my-react", {"my-react", "my-react-2"}) == "my-react-3"
    plugin = builder.Builder(permissions=frozenset({"ui", "create_services"}))
    asked: list[tuple[Any, ...]] = []
    deletions: list[str] = []
    projects = [{"id": "abc", "name": "shop", "template": "flask", "folder": "/p/shop", "state": "stopped"}]

    def create(*args: Any) -> dict[str, Any]:
        asked.append(args)
        return {"folder": "/p/x", "port": 5050}

    monkeypatch.setattr(plugin, "project_templates", lambda: {"templates": [
        {"key": "flask", "label": "Flask", "tool": "pip", "port": 5050, "kind": "web", "ready": True, "needs": ""},
        {"key": "discord-js", "label": "Discord bot (Node.js)", "tool": "npm", "port": 0, "kind": "bot",
         "ready": False, "needs": "Node.js (npm)"}], "folder": "/p"})
    monkeypatch.setattr(plugin, "list_projects", lambda: projects)
    monkeypatch.setattr(plugin, "create_project", create)
    monkeypatch.setattr(plugin, "delete_project", deletions.append)

    def settle() -> None:
        deadline = time_.monotonic() + 5
        while (plugin.loading or plugin.busy) and time_.monotonic() < deadline:
            time_.sleep(0.01)

    plugin.on_start()
    settle()
    assert plugin.projects == projects
    plugin.on_action("create", "x")
    assert "Choose a template" in plugin.status[0]
    plugin.on_action("template", "Discord bot (Node.js)")
    plugin.on_action("create", "bot")
    assert "needs Node.js" in plugin.status[0] and not asked
    plugin.on_action("template", "Flask")
    plugin.on_action("create", "Bad Name")
    assert "lowercase" in plugin.status[0]
    plugin.on_action("create", "shop")
    settle()
    assert asked == [("flask", "shop", "", True)]
    plugin.on_action("delete", "nope")
    assert "Choose a project" in plugin.status[0] and not deletions
    plugin.on_action("delete", "shop")
    assert deletions == ["abc"] and "confirm" in plugin.status[0]  # Pythonator asks the user; nothing else
