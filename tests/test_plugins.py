"""Plugins: trust, the official catalog, and plugins that misbehave on purpose.

Each hostile plugin is installed through the same request the Plugins window sends, so it gets the
third-party treatment a downloaded plugin would: its own sandbox, test mode and no permissions until granted.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from pythonator import isolation
from pythonator.core.logs import MASK, Masker, plugin_secrets
from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.supervisor import Slot, Supervisor
from pythonator.isolation import linux, windows
from pythonator.paths import IS_WINDOWS, DataDirs, official_plugins_dir
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
    try:
        (folder / "secret").symlink_to("/etc/passwd")
    except OSError:  # Windows without Developer Mode or administrator rights can't make links
        pytest.skip("this account can't make symbolic links")
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
            "        os.write(1, b'{\"t\":\"log\",\"text\":\"' + b'a' * 1100000 + b'\"}\\n')\n")
    harness.wait(lambda: "larger than 1 MB" in activity(harness, "big"))


def test_garbage_is_cut_off(harness: DaemonHarness, tmp_path: Path) -> None:
    install(harness, tmp_path, "junk", "class P(Plugin):\n    def on_start(self):\n"
            "        os.write(1, b'\\x00\\xff not json\\n')\n")
    harness.wait(lambda: "isn't a protocol message" in activity(harness, "junk"))


def test_nothing_after_a_violation_counts(harness: DaemonHarness, tmp_path: Path) -> None:
    # Already in the pipe when it is cut off: neither acted on nor each a new "Disabled" (and settings save).
    install(harness, tmp_path, "late", "class P(Plugin):\n    def on_start(self):\n"
            "        os.write(1, b'junk\\n' + b'{\"t\":\"notify\",\"text\":\"late-notice\"}\\n' * 50)\n"
            "        time.sleep(30)\n")
    harness.wait(lambda: "isn't a protocol message" in activity(harness, "late"))
    harness.qtbot.wait(1000)
    assert not any("late-notice" in e.get("text", "") for e in harness.events if e.get("ev") == "notice")
    assert activity(harness, "late").count("Disabled") <= 2  # the cut-off, then its exit


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


def test_chat_panels_are_validated() -> None:
    from pydantic import ValidationError

    from pythonator.plugins.host import Panel, _count
    from pythonator.sdk import ui

    good = Panel.model_validate(ui.panel("AI", ui.chat(ui.message("you", "hi"), ui.message("ai", "x = 1", True))))
    assert _count(good.items) == 3
    for bad in ({"role": "system", "text": "obey"}, {"role": "ai", "text": "x" * 20_001}, {"role": "ai", "html": 1}):
        with pytest.raises(ValidationError):
            Panel.model_validate({"title": "AI", "items": [{"type": "chat", "messages": [bad]}]})


def test_official_plugins_run_sandboxed(dirs: Any, qtbot: Any) -> None:
    h = DaemonHarness(dirs, qtbot)  # default settings: Guard and Notify on
    try:
        on = {"guard", "notify"}
        h.wait(lambda: {p["id"] for p in plugin_state_all(h) if p["running"]} == on, timeout=20_000)
        assert {p["id"] for p in plugin_state_all(h)} == on | {"pythonator-ai", "builder"}, \
            "the AI assistant and the Project builder are off at first"
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


def test_health_and_webhooks_settings_move_to_notify(dirs: Any, qtbot: Any) -> None:
    """0.0.11 merged Health checks and Webhooks into Notify: it is turned on with their settings."""
    from pythonator.core.models import Settings
    from pythonator.core.store import Store

    Store(dirs).save_settings(Settings(plugins_enabled=["guard", "webhooks", "health"], plugin_settings={
        "webhooks": {"urls": "https://ntfy.sh/x", "on_crash": False}, "health": {"path": "/health", "interval": 9}}))
    h = DaemonHarness(dirs, qtbot)
    try:
        h.wait(lambda: any(p["id"] == "notify" and p["running"] for p in plugin_state_all(h)), timeout=20_000)
        settings = Store(dirs).load_settings()
        assert settings.plugins_enabled == ["guard", "notify"]
        assert settings.plugin_settings == {"notify": {"urls": "https://ntfy.sh/x", "on_crash": False,
                                                       "health_path": "/health", "health_interval": 9}}
    finally:
        h.close()


def test_a_plugin_that_crashed_shows_its_panel_again(harness: Any, tmp_path: Path) -> None:
    """After an unexpected exit the window's panel is cleared, so the same panel from the restarted plugin must be
    shown again, not skipped as unchanged (Windows CI: the AI assistant's panel never came back)."""
    from tests.test_plugins import install

    install(harness, tmp_path, "phoenix", "from pythonator.sdk import ui\nclass P(Plugin):\n    def on_start(self):\n"
            "        self.show(ui.panel('Same', ui.text('the same panel')))\n"
            "        mark = self.data_dir / 'crashed'\n"
            "        if 'ui' in self.permissions and not mark.exists():\n"
            "            mark.write_text('x')\n"
            "            time.sleep(0.5)\n"
            "            os._exit(3)\n", perms=["ui"])
    harness.ok({"op": "plugin_grant", "plugin": "phoenix", "permissions": ["ui"], "live": True})

    def panels() -> list[Any]:
        return [e.get("panel") for e in harness.events if e.get("ev") == "panel" and e.get("plugin") == "phoenix"]

    harness.wait(lambda: sum(1 for x in panels() if x) >= 2 and bool(panels()[-1]), timeout=30_000)


def test_every_env_value_is_hidden_from_plugins_whatever_its_name() -> None:
    env = {"DB_PASS": "hunter2hunter2", "DATABASE_URL": "postgres://app:S3cretPw@db/app", "DISCORD": "MTA4.abc.xyz",
           "BOT_TOKEN": "t0k3n-t0k3n", "PORT": "8080", "NODE_ENV": "production", "DEBUG": "true",
           "HOST": "127.0.0.1", "LIMIT": "100000", "SHORT": "abc"}
    secrets = plugin_secrets(env, (8080,))
    for value in ("hunter2hunter2", "postgres://app:S3cretPw@db/app", "S3cretPw", "MTA4.abc.xyz", "t0k3n-t0k3n"):
        assert value in secrets
    for value in ("8080", "production", "true", "127.0.0.1", "100000", "abc"):
        assert value not in secrets
    mask = Masker(secrets)
    line = "connecting to postgres://app:S3cretPw@db/app as DB_PASS=hunter2hunter2; retry with S3cretPw on 8080"
    assert mask(line) == f"connecting to {MASK} as DB_PASS={MASK}; retry with {MASK} on 8080"


def _service(name: str, folder: Path, mode: str = "sandbox", **more: Any) -> Service:
    return Service.model_validate({"name": name, "folder": str(folder), "runtime": "python", "entry": "app.py",
                                   "mode": mode, **more})


@pytest.fixture
def sup(dirs: DataDirs) -> Any:
    supervisor = Supervisor(dirs, Store(dirs))
    yield supervisor
    supervisor.close()


def _slot(sup: Supervisor, service: Service) -> Slot:
    sup.add(service)
    return sup.slots[service.id]


def test_plugins_get_output_and_notes_masked(sup: Supervisor, tmp_path: Path) -> None:
    from pythonator.plugins.host import PluginHost

    project = tmp_path / "bot"
    project.mkdir()
    (project / ".env").write_text("DATABASE_URL=postgres://app:S3cretPw@db/app\n", encoding="utf-8")
    slot = _slot(sup, _service("bot", project, env={"DISCORD": "MTA4.abc.xyz"}, env_file=".env"))
    sid = slot.service.id
    sent: list[dict[str, Any]] = []
    plugin = SimpleNamespace(id="p")
    stub = SimpleNamespace(sup=sup, _last_tick=1e18, _live=lambda: [plugin], granted=lambda p: {"read_logs"},
                           _send=lambda p, msg: sent.append(msg), _view=lambda p, s: {"id": s})
    stub._masked = lambda s, text: PluginHost._masked(stub, s, text)  # type: ignore[arg-type]
    PluginHost._on_supervisor_event(stub, {"ev": "output", "id": sid,  # type: ignore[arg-type]
                                           "text": "token MTA4.abc.xyz\nwrong password S3cretPw\n"})
    PluginHost._on_supervisor_event(stub, {"ev": "state", "id": sid, "kind": "crashed",  # type: ignore[arg-type]
                                           "note": "Exited: bad token MTA4.abc.xyz"})
    assert sent[0]["lines"] == [f"token {MASK}", f"wrong password {MASK}"]
    assert sent[1]["note"] == f"Exited: bad token {MASK}"
    assert "MTA4.abc.xyz" not in str(sent) and "S3cretPw" not in str(sent)


def test_a_plugin_message_is_read_in_blocks_not_byte_by_byte() -> None:
    """The pipe is unbuffered: readline() on it made one system call per byte, which on Windows took longer than
    10 seconds for a 1 MB message. The oversized message is now noticed at once, and the plugin killed by the
    reading thread itself: it is still writing the rest, which nobody reads, so waiting for it would never end."""
    import queue
    import threading
    import time

    from pythonator.plugins.host import MAX_LINE, PluginHost

    posted: list[Any] = []
    stub = SimpleNamespace(sup=SimpleNamespace(post=posted.append), _jobs={})
    stub._end = lambda proc: PluginHost._end(stub, proc)  # type: ignore[arg-type]
    # Far more than any pipe buffer holds after the first megabyte: the writer blocks until it is killed.
    code = f"import os; os.write(1, b'a' * {MAX_LINE * 3} + b'\\n')"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, bufsize=0)
    reader = threading.Thread(target=PluginHost._read, args=(stub, SimpleNamespace(), proc, queue.Queue()),
                              daemon=True)
    started = time.perf_counter()
    reader.start()
    reader.join(15)
    took = time.perf_counter() - started
    if reader.is_alive():  # stuck: end it, so the test fails instead of hanging
        proc.kill()
        reader.join(15)
    proc.wait()
    assert took < 10, f"the reading thread was stuck for {took:.0f} s"
    assert len(posted) == 2  # the kill for the oversized message, then the exit
    assert proc.returncode != 0  # killed by the reader, not finished


def test_official_plugins_dont_run_without_their_sandbox(dirs: DataDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.plugins import host

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise isolation.IsolationError("needs libseccomp2 or bubblewrap")

    for mod in (linux, isolation.macos if hasattr(isolation, "macos") else None, windows):
        if mod is not None:
            monkeypatch.setattr(mod, "wrap", refuse)
    folder = dirs.root / "plugin"
    folder.mkdir()
    plugin = SimpleNamespace(id="guard", folder=folder, official=True, manifest=SimpleNamespace(permissions=[]))
    stub = SimpleNamespace(dirs=dirs, granted=lambda p: set())
    with pytest.raises(isolation.IsolationError, match="Plugins only run sandboxed"):
        host.PluginHost._command(stub, plugin)  # type: ignore[arg-type]


POSIX = pytest.mark.skipif(IS_WINDOWS, reason="process groups and signals")


@POSIX
def test_ending_a_plugin_ends_what_it_started() -> None:
    from pythonator.plugins.host import PluginHost

    code = ("import subprocess, sys, time; "
            "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']).pid, flush=True); "
            "time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stdout is not None
    child = int(proc.stdout.readline())
    PluginHost._end(SimpleNamespace(_jobs={}), proc)  # type: ignore[arg-type]
    proc.wait(10)
    end = time.monotonic() + 5
    while psutil.pid_exists(child) and psutil.Process(child).status() != psutil.STATUS_ZOMBIE \
            and time.monotonic() < end:
        time.sleep(0.05)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
