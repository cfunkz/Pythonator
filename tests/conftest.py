"""Shared fixtures. Every test gets its own private data folder, so nothing touches the real one."""

from __future__ import annotations

import gc
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYTHONATOR_NO_KEYCHAIN"] = "1"  # tests never write to this computer's real keychain

from PyQt6.QtCore import QCoreApplication, QEvent

from pythonator.paths import DataDirs


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DataDirs:
    home = tmp_path / "data"
    monkeypatch.setenv("PYTHONATOR_HOME", str(home))
    d = DataDirs(home)
    d.ensure()
    return d


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def write_plugin(base: Path, pid: str, body: str, permissions: list[str] | None = None) -> Path:
    """A third-party plugin folder with ``body`` as plugin.py."""
    folder = base / pid
    folder.mkdir(parents=True)
    perms = ", ".join(f'"{p}"' for p in (permissions or []))
    (folder / "plugin.toml").write_text(
        f'[plugin]\nid = "{pid}"\nname = "{pid}"\nversion = "1.0"\napi = 1\ndescription = "test"\n'
        f"permissions = [{perms}]\n", encoding="utf-8")
    (folder / "plugin.py").write_text(body, encoding="utf-8")
    return folder


class DaemonHarness:
    """The real background service, in this process, driven through its socket by the real client."""

    def __init__(self, dirs: DataDirs, qtbot: Any) -> None:
        from pythonator.daemon.client import DaemonClient
        from pythonator.daemon.server import Daemon

        self.qtbot = qtbot
        self.daemon = Daemon(dirs)
        assert self.daemon.listen()
        self.daemon.run()
        self.client = DaemonClient(dirs)
        self.events: list[dict[str, Any]] = []
        self.client.message.connect(self.events.append)
        with qtbot.waitSignal(self.client.connected, timeout=5000):
            self.client.connect_daemon()

    def call(self, message: dict[str, Any], timeout: int = 5000) -> tuple[bool, Any, str]:
        box: list[tuple[bool, Any, str]] = []
        self.client.request(message, lambda ok, data, err: box.append((ok, data, err)))
        self.qtbot.waitUntil(lambda: bool(box), timeout=timeout)
        return box[0]

    def ok(self, message: dict[str, Any], timeout: int = 5000) -> Any:
        ok, data, err = self.call(message, timeout)
        assert ok, err
        return data

    def wait(self, pred: Callable[[], bool], timeout: int = 10_000) -> None:
        self.qtbot.waitUntil(pred, timeout=timeout)

    def states(self, sid: str) -> list[str]:
        return [e["state"] for e in self.events if e.get("ev") == "state" and e.get("id") == sid]

    def output(self, sid: str) -> str:
        return "".join(e["text"] for e in self.events if e.get("ev") == "output" and e.get("id") == sid)

    def add(self, **service: Any) -> str:
        self.ok({"op": "add", "service": service})
        self.wait(lambda: any(e.get("ev") == "services" and any(s["name"] == service["name"] for s in e["services"])
                              for e in self.events))
        last = [e for e in self.events if e.get("ev") == "services"][-1]
        return str(next(s["id"] for s in last["services"] if s["name"] == service["name"]))

    def close(self) -> None:
        self.client.close()
        self.daemon.quit(stop_services=True)
        # Delete the sockets now and collect the daemon here, while it is whole, not in a later test.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.client.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        gc.collect()


@pytest.fixture
def harness(dirs: DataDirs, qtbot: Any) -> Iterator[DaemonHarness]:
    from pythonator.core.models import Settings
    from pythonator.core.store import Store

    Store(dirs).save_settings(Settings(plugins_enabled=[]))  # plugins only where a test turns them on
    h = DaemonHarness(dirs, qtbot)
    yield h
    h.close()


PY = sys.executable
