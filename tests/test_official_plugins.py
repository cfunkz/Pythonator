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
    hooks = plugin_module("webhooks")
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

    health = plugin_module("health")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, ms = health.probe(server.server_address[1], "/", 2.0)
        assert status.startswith(("200", "OK", "HTTP")) or "200" in status, status
        assert ms >= 0
    finally:
        server.shutdown()
