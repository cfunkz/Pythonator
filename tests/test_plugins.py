"""Plugins: trust, the official catalog, and plugins that misbehave on purpose.

Each hostile plugin is installed through the same request the Plugins window sends, so it gets the
third-party treatment a downloaded plugin would: its own sandbox, test mode and no permissions until granted.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from pythonator.paths import official_plugins_dir
from pythonator.plugins import trust
from pythonator.plugins.manifest import load
from tests.conftest import DaemonHarness, write_plugin

HEADER = "import os, time\nfrom pythonator.sdk import Plugin\n\n"


def plugin_state(h: DaemonHarness, pid: str) -> dict[str, Any]:
    events = [e for e in h.events if e.get("ev") == "plugins"]
    return next((p for p in events[-1]["plugins"] if p["id"] == pid), {}) if events else {}


def activity(h: DaemonHarness, pid: str) -> str:
    lines = [e["text"] for e in h.events if e.get("ev") == "plugin_activity" and e.get("plugin") == pid]
    return "\n".join(lines)


def install(h: DaemonHarness, tmp_path: Path, pid: str, body: str, perms: list[str] | None = None) -> None:
    folder = write_plugin(tmp_path / "src", pid, HEADER + body, perms)
    assert h.ok({"op": "plugin_install", "source": str(folder)}) == pid
    assert plugin_state(h, pid)["official"] is False
    h.ok({"op": "plugin_enable", "plugin": pid, "enabled": True})


# -- trust ------------------------------------------------------------------------------------------------


def test_official_catalog_is_up_to_date() -> None:
    """Fails when an official plugin changed without ``python -m pythonator.plugins.trust --update``."""
    catalog = trust.catalog()
    folders = {p.name: p for p in official_plugins_dir().iterdir() if (p / "plugin.toml").is_file()}
    assert set(catalog) == set(folders)
    for pid, folder in folders.items():
        assert catalog[pid] == trust.tree_hash(folder), f"{pid}: run python -m pythonator.plugins.trust --update"
        assert trust.is_official(pid, folder)


def test_tree_hash_is_the_same_on_every_system(tmp_path: Path) -> None:
    """Files are hashed in one case-sensitive order everywhere (Windows sorts paths ignoring case)."""
    import hashlib

    (tmp_path / "sub").mkdir()
    files = {"README.md": b"r\r\n", "a.py": b"a\n", "sub/x.py": b"x"}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    digest = hashlib.sha256()
    for name in ("README.md", "a.py", "sub/x.py"):  # "R" sorts before "a"
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name].replace(b"\r\n", b"\n")).digest())
    assert trust.tree_hash(tmp_path) == digest.hexdigest()


def test_one_changed_byte_makes_it_third_party(tmp_path: Path) -> None:
    original = official_plugins_dir() / "guard"
    copy = tmp_path / "guard"
    shutil.copytree(original, copy, ignore=shutil.ignore_patterns("__pycache__"))
    assert trust.tree_hash(copy) == trust.tree_hash(original)
    assert not trust.is_official("guard", copy), "an exact copy elsewhere isn't official either"
    data = bytearray((copy / "plugin.py").read_bytes())
    data[-2] ^= 1
    (copy / "plugin.py").write_bytes(bytes(data))
    assert trust.tree_hash(copy) != trust.tree_hash(original)


def test_tree_hash_refuses_links(tmp_path: Path) -> None:
    folder = write_plugin(tmp_path, "linky", HEADER)
    (folder / "secret").symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        trust.tree_hash(folder)


@pytest.mark.parametrize("toml", [
    '[plugin]\nid = "../x"\nname = "x"\nversion = "1"\napi = 1\n',
    '[plugin]\nid = "x"\nname = "x"\nversion = "1"\napi = 99\n',
    '[plugin]\nid = "x"\nname = "x"\nversion = "1"\napi = 1\nentry = "../../evil.py"\n',
    '[plugin]\nid = "x"\nname = "x"\nversion = "1"\napi = 1\npermissions = ["root"]\n',
])
def test_bad_manifests_are_refused(tmp_path: Path, toml: str) -> None:
    (tmp_path / "plugin.toml").write_text(toml)
    with pytest.raises(ValueError):
        load(tmp_path)


# -- hostile plugins --------------------------------------------------------------------------------------


def test_flood_is_cut_off(harness: DaemonHarness, tmp_path: Path) -> None:
    install(harness, tmp_path, "flood", "class P(Plugin):\n    def on_start(self):\n"
            "        for _ in range(200000):\n            os.write(1, b'{\"t\":\"log\",\"text\":\"x\"}\\n')\n")
    harness.wait(lambda: "more than 200 messages a second" in activity(harness, "flood"), timeout=20_000)
    harness.wait(lambda: not plugin_state(harness, "flood")["enabled"])
    assert harness.ok({"op": "ping"}) == "pong"


def test_oversized_message_is_cut_off(harness: DaemonHarness, tmp_path: Path) -> None:
    install(harness, tmp_path, "big", "class P(Plugin):\n    def on_start(self):\n"
            "        os.write(1, b'{\"t\":\"log\",\"text\":\"' + b'a' * 70000 + b'\"}\\n')\n")
    harness.wait(lambda: "larger than 64 KB" in activity(harness, "big"))


def test_garbage_is_cut_off(harness: DaemonHarness, tmp_path: Path) -> None:
    install(harness, tmp_path, "junk", "class P(Plugin):\n    def on_start(self):\n"
            "        os.write(1, b'\\x00\\xff not json\\n')\n")
    harness.wait(lambda: "isn't a protocol message" in activity(harness, "junk"))


def test_crashing_plugin_is_disabled(harness: DaemonHarness, tmp_path: Path) -> None:
    install(harness, tmp_path, "crashy", "raise SystemExit(9)\n")
    harness.wait(lambda: "crashed 3 times" in plugin_state(harness, "crashy").get("error", ""), timeout=30_000)
    assert not plugin_state(harness, "crashy")["enabled"]


def test_forged_control_is_refused(harness: DaemonHarness, tmp_path: Path, project: Path) -> None:
    (project / "loop.py").write_text("import time\nwhile True: time.sleep(0.2)\n")
    sid = harness.add(name="victim", folder=str(project), entry="loop.py", restart="never")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "running" in harness.states(sid))
    install(harness, tmp_path, "evil", "class P(Plugin):\n    def on_start(self):\n"
            "        for sid in self.services: self.stop(sid)\n", perms=["control"])
    harness.wait(lambda: "needs the control permission" in activity(harness, "evil"))
    # Granted, but still in test mode: recorded, not done.
    harness.ok({"op": "plugin_grant", "plugin": "evil", "permissions": ["control"], "live": False})
    harness.wait(lambda: "Test mode: would stop victim" in activity(harness, "evil"), timeout=15_000)
    assert harness.states(sid)[-1] == "running"


def test_plugin_cannot_read_secrets_or_reach_the_network(harness: DaemonHarness, tmp_path: Path) -> None:
    from pythonator.paths import DataDirs

    token = DataDirs().token_file
    install(harness, tmp_path, "snoop", "import socket\nclass P(Plugin):\n    def on_start(self):\n"
            f"        for what, fn in (('token', lambda: open({str(token)!r}).read()),\n"
            "                         ('home', lambda: os.listdir(os.path.expanduser('~/.ssh'))),\n"
            "                         ('net', lambda: socket.create_connection(('1.1.1.1', 443), 3))):\n"
            "            try:\n                fn(); self.log('LEAK ' + what)\n"
            "            except Exception as exc:\n                self.log('blocked ' + what)\n")
    harness.wait(lambda: activity(harness, "snoop").count("blocked") >= 3, timeout=15_000)
    assert "LEAK" not in activity(harness, "snoop")


def test_hanging_policy_check_cannot_block_starts(harness: DaemonHarness, tmp_path: Path, project: Path) -> None:
    install(harness, tmp_path, "slow", "class P(Plugin):\n    def check_start(self, service):\n"
            "        time.sleep(30)\n        return 'no'\n", perms=["policy"])
    harness.ok({"op": "plugin_grant", "plugin": "slow", "permissions": ["policy"], "live": True})
    harness.wait(lambda: plugin_state(harness, "slow").get("running") is True, timeout=15_000)
    (project / "ok.py").write_text("print('started anyway', flush=True)\n")
    sid = harness.add(name="gated", folder=str(project), entry="ok.py", restart="never")
    harness.ok({"op": "start", "id": sid})
    harness.wait(lambda: "started anyway" in harness.output(sid), timeout=10_000)
    assert "Didn't answer a start check" in activity(harness, "slow")


def test_policy_plugin_can_block_when_live(harness: DaemonHarness, tmp_path: Path, project: Path) -> None:
    install(harness, tmp_path, "nope", "class P(Plugin):\n    def check_start(self, service):\n"
            "        return 'not today'\n", perms=["policy"])
    harness.ok({"op": "plugin_grant", "plugin": "nope", "permissions": ["policy"], "live": True})
    harness.wait(lambda: plugin_state(harness, "nope").get("running") is True, timeout=15_000)
    (project / "ok.py").write_text("print('ran')\n")
    sid = harness.add(name="blocked", folder=str(project), entry="ok.py", restart="never")
    harness.call({"op": "start", "id": sid})
    harness.wait(lambda: any(e.get("ev") == "state" and e.get("id") == sid and "not today" in e.get("note", "")
                             for e in harness.events))
    assert "ran" not in harness.output(sid)


def test_panels_are_validated(harness: DaemonHarness, tmp_path: Path) -> None:
    install(harness, tmp_path, "paint", "from pythonator.sdk import ui\nclass P(Plugin):\n    def on_start(self):\n"
            "        self.show({'title': 'x', 'items': [{'type': 'script', 'text': '<img onerror=alert(1)>'}]})\n"
            "        self.show(ui.panel('Fine', ui.text('<b>plain</b>')))\n", perms=["ui"])
    harness.ok({"op": "plugin_grant", "plugin": "paint", "permissions": ["ui"], "live": True})
    harness.wait(lambda: any(e.get("ev") == "panel" and e.get("plugin") == "paint" and e.get("panel")
                             for e in harness.events), timeout=15_000)
    assert "Panel refused" in activity(harness, "paint")
    panel = next(e["panel"] for e in harness.events if e.get("ev") == "panel" and e.get("panel"))
    assert panel["title"] == "Fine"


def test_official_plugins_run_sandboxed(dirs: Any, qtbot: Any) -> None:
    h = DaemonHarness(dirs, qtbot)  # default settings: the three official plugins on
    try:
        on = {"guard", "webhooks", "health"}
        h.wait(lambda: {p["id"] for p in plugin_state_all(h) if p["running"]} == on, timeout=20_000)
        assert {p["id"] for p in plugin_state_all(h)} == on | {"pythonator-ai"}, "the AI assistant is off at first"
        for p in plugin_state_all(h):
            assert p["official"] and not p["test_mode"]
            if p["id"] not in on:
                continue
            log = "\n".join(p["activity"])
            assert "Started (official;" in log and "no sandbox" not in log, log
    finally:
        h.close()


def plugin_state_all(h: DaemonHarness) -> list[dict[str, Any]]:
    events = [e for e in h.events if e.get("ev") == "plugins"]
    return list(events[-1]["plugins"]) if events else []


def test_worker_reads_utf8_whatever_the_code_page(monkeypatch: Any) -> None:
    """Windows gives a plugin's stdin the ANSI code page: "━" (E2 94 81) became "â”\\udc81", which then broke
    sending it on as UTF-8 ("surrogates not allowed")."""
    import io
    import json as json_
    import queue

    from pythonator.plugins import worker

    line = json_.dumps({"t": "output", "id": "x", "lines": ["━ ok …"]}, ensure_ascii=False).encode() + b"\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(line), encoding="cp1252", errors="surrogateescape"))
    inbox: queue.Queue[Any] = queue.Queue()
    worker._read_stdin(inbox)
    assert inbox.get()["lines"] == ["━ ok …"]
