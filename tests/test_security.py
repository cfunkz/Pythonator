"""Security timeline store and Guard's switchable rules."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from pythonator.store.events import SecurityLog

ROOT = Path(__file__).resolve().parent.parent / "pythonator" / "resources"


def test_fold_reload_resolve(tmp_path: Path) -> None:
    log = SecurityLog(tmp_path / "security.jsonl")
    a = log.add("blocked", "api", "Blocked x.example:443", "not on allow-list", {"kind": "allow", "service": "api", "host": "x.example"}, ts=1000)
    log.add("blocked", "api", "Blocked x.example:443", ts=1030)  # folded: same thing within a minute
    log.add("blocked", "api", "Blocked x.example:443", ts=1200)  # a new row: the last one is older than a minute
    log.add("healed", "ledger", "Container c, left by an interrupted start.", ts=1300)
    assert [r["count"] for r in log.rows] == [2, 1, 1]
    log.resolve(a["id"], "allowed")
    again = SecurityLog(tmp_path / "security.jsonl")
    assert len(again.rows) == 0  # older than 30 days (ts=1000), dropped on load
    now = SecurityLog(tmp_path / "now.jsonl")
    r = now.add("refused", "web", "Guard refused: ports")
    now.add("refused", "web", "Guard refused: ports")
    now.resolve(r["id"], "seen")
    back = SecurityLog(tmp_path / "now.jsonl")
    assert len(back.rows) == 1 and back.rows[0]["count"] == 2 and back.rows[0]["action"]["done"] == "seen"
    assert back.since(0) == list(back.rows)


def test_unknown_kind_refused(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        SecurityLog(tmp_path / "s.jsonl").add("whatever", "x", "y")


def _guard() -> types.ModuleType:
    sdk = types.ModuleType("pythonator_sdk")

    class _P:
        settings: dict = {}

        def hook(self, _n):  # noqa: ANN001, ANN202
            return lambda f: f

        on = hook

    sdk.plugin = _P()  # type: ignore[attr-defined]
    sys.modules["pythonator_sdk"] = sdk
    spec = importlib.util.spec_from_file_location("guard_main", ROOT / "plugins" / "guard" / "main.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guard_rules_switch() -> None:
    g = _guard()
    base = {"full_access_only_inside": ["/h/scripts"], "port_range": [1024, 9999], "lan_ports": False,
            "max_exposure": 6.0}
    full = {"name": "b", "mode": "full", "folder": "/h/other", "ports": [], "exposure": 6.0, "widens": ["x"]}
    assert not g._review(full, {**base, "full_access_rule": True}, "b")["allow"]
    assert g._review(full, {**base, "full_access_rule": False}, "b")["allow"]
    lan = {"name": "w", "ports": [[3000, "lan"]], "exposure": 2}
    assert not g._review(lan, {**base, "port_rule": True}, "w")["allow"]
    assert g._review(lan, {**base, "port_rule": False}, "w")["allow"]
    wide = {"name": "s", "ports": [], "exposure": 7.2, "widens": ["Traffic: any host"]}
    assert not g._review(wide, {**base, "exposure_rule": True}, "s")["allow"]
    assert g._review({**wide, "widens": []}, {**base, "exposure_rule": True}, "s")["allow"]  # narrowing is allowed
    assert g._review(wide, {**base, "exposure_rule": False}, "s")["allow"]
    net = {"name": "n", "ports": [], "lan": True}
    assert not g._review(net, {**base, "block_lan": True}, "n")["allow"]
    assert g._review(net, base, "n")["allow"]
