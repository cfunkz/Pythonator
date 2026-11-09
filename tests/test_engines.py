"""Engines page backend: parsing engine output, and Prune touching only ledger rows."""

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

import pytest

from pythonator.ledger.ledger import Ledger
from pythonator.work import engines


def test_json_rows_both_shapes() -> None:
    assert engines._json_rows('[{"Names":["a"]},{"Names":["b"]}]') == [{"Names": ["a"]}, {"Names": ["b"]}]
    assert engines._json_rows('{"Names":"a"}\n{"Names":"b"}\nnot json\n') == [{"Names": "a"}, {"Names": "b"}]
    assert engines._json_rows("") == []


def test_labels_and_names() -> None:
    assert engines._labels({"Labels": "pythonator.owner=1,x=y"}) == {"pythonator.owner": "1", "x": "y"}
    assert engines._labels({"Labels": {"a": "b"}}) == {"a": "b"}
    assert engines._name({"Names": ["/web"]}) == "web"


@pytest.mark.parametrize(("state", "status", "want"), [
    ("running", "Up 6 days", "up 6d"),
    ("running", "Up About an hour (healthy)", "up 1h"),
    ("running", "Up Less than a second", "up 0s"),
    ("exited", "Exited (0) 3 days ago", "exited 3d"),
    ("restarting", "Restarting (1) 2 seconds ago", "restarting"),
])
def test_state(state: str, status: str, want: str) -> None:
    assert engines._state({"State": state, "Status": status})[0] == want


def test_size() -> None:
    assert engines._size("2.1GB") == 2_100_000_000
    assert engines._size("12.2 MB") == 12_200_000
    assert engines._size(500) == 500


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake engine")
def test_prune_only_touches_ledger_rows(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    fake = tmp_path / "docker"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    led = Ledger(tmp_path / "ledger.jsonl")
    led.done(led.intend("container", "gone", "pythonator-gone", engine="docker"))
    led.done(led.intend("container", "live", "pythonator-live", engine="docker"))
    led.done(led.intend("container", "pod", "pythonator-pod", engine="podman"))
    led.done(led.intend("image", "gone", "redis:7", engine="docker"))
    led.done(led.intend("image", "live", "node:22", engine="docker"))
    res = asyncio.run(engines.prune("docker", str(fake), led, {"live"}, {"node:22"}))
    calls = log.read_text().splitlines()
    assert calls == ["rm -f pythonator-gone", "rmi redis:7"]
    assert res["removed"] == ["pythonator-gone", "redis:7"]
    assert {e.ref for e in led.live()} == {"pythonator-live", "pythonator-pod", "node:22"}
