"""Plugin API 2: what a plugin may read of a service's folder, the changes it may suggest, and secrets kept out of
settings.json. The rules are tested directly, then end to end through the real background service."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from pythonator.core.keychain import IN_FILE, Vault
from pythonator.plugins.files import FileRefused, ServiceFiles, clean_rel, is_secret
from tests.conftest import DaemonHarness, write_plugin


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("PORT = 80\nprint('hi')\n" + "x = 1\n" * 30, encoding="utf-8")
    (root / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=supersecretvalue\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "secrets.yaml").write_text("a: b\n", encoding="utf-8")
    (root / "id_rsa").write_text("key\n", encoding="utf-8")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "i.js").write_text("", encoding="utf-8")
    (root / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (root / "tokenizer.py").write_text("pass\n", encoding="utf-8")
    return root


def view(folder: Path) -> ServiceFiles:
    return ServiceFiles(folder, ".env", {"API_TOKEN": "live-value-123456"})


def test_listing_hides_secrets_and_dependency_folders(folder: Path) -> None:
    listing = view(folder).listing()
    paths = {f["path"] for f in listing["files"]}
    assert paths == {"src/app.py", "requirements.txt", "run.sh", "tokenizer.py"}
    assert listing["hidden"] == 3


@pytest.mark.parametrize("path", [".env", "config/secrets.yaml", "id_rsa", "../outside.txt", "/etc/passwd",
                                  "C:/Windows/win.ini", "node_modules/x/i.js", "src/../../x", ""])
def test_reading_secrets_or_outside_is_refused(folder: Path, path: str) -> None:
    (folder.parent / "outside.txt").write_text("no", encoding="utf-8")
    with pytest.raises(FileRefused):
        view(folder).read(path)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_links_out_of_the_folder_are_refused(folder: Path, tmp_path: Path) -> None:
    (tmp_path / "private.txt").write_text("private", encoding="utf-8")
    (folder / "link.txt").symlink_to(tmp_path / "private.txt")
    assert "link.txt" not in {f["path"] for f in view(folder).listing()["files"]}
    with pytest.raises(FileRefused):
        view(folder).read("link.txt")


def test_environment_values_are_hidden(folder: Path) -> None:
    (folder / "src" / "log.txt").write_text("using live-value-123456 now", encoding="utf-8")
    assert view(folder).read("src/log.txt")["text"] == "using [API_TOKEN hidden] now"


def test_secret_names() -> None:
    assert is_secret("prod.env") and is_secret("certs/server.pem") and is_secret("tokens.json")
    assert not is_secret("tokenizer.py") and not is_secret("secrets_manager.py") and not is_secret("README.md")
    assert clean_rel("src\\app.py") == "src/app.py"


# -- proposals -------------------------------------------------------------------------------------------------


def test_a_good_change_is_applied_with_a_backup(folder: Path, tmp_path: Path) -> None:
    files = view(folder)
    proposal = files.propose("ai", "svc", "src/app.py", [{"old": "PORT = 80", "new": "PORT = 8000"}], "port")
    assert proposal.warnings == []
    assert (folder / "src" / "app.py").read_text().startswith("PORT = 80\n"), "nothing written yet"
    backup = files.apply(proposal, tmp_path / "backups")
    assert (folder / "src" / "app.py").read_text().startswith("PORT = 8000\n")
    assert Path(backup).read_text().startswith("PORT = 80\n")


def test_a_file_changed_since_is_not_overwritten(folder: Path, tmp_path: Path) -> None:
    files = view(folder)
    proposal = files.propose("ai", "svc", "src/app.py", [{"old": "PORT = 80", "new": "PORT = 8000"}], "")
    (folder / "src" / "app.py").write_text("edited by the user\n", encoding="utf-8")
    with pytest.raises(FileRefused, match="changed"):
        files.apply(proposal, tmp_path / "backups")
    assert (folder / "src" / "app.py").read_text() == "edited by the user\n"


@pytest.mark.parametrize(("path", "edits", "why"), [
    ("src/app.py", [{"old": "print('hi')", "new": "import subprocess; subprocess.run('rm -rf ~', shell=True)"}],
     "runs programs"),
    ("src/app.py", [{"old": "print('hi')", "new": "os.system('format c:')"}], "runs programs"),
    ("src/app.py", [{"old": "print('hi')", "new": "shutil.rmtree('/')"}], "deletes"),
    ("src/app.py", [{"old": "print('hi')", "new": "eval(input())"}], "evaluates"),
    ("src/app.py", [{"old": "print('hi')", "new": "import ctypes"}], "operating system"),
    ("run.sh", [{"old": "echo hi", "new": "echo bye"}], "script"),
    (".env", [{"old": "TOKEN", "new": "X"}], "secrets"),
    ("../evil.py", [{"old": "", "new": "x"}], "inside"),
    ("src/app.py", [{"old": "not there", "new": "x"}], "exactly once"),
    ("src/app.py", [{"old": "x = 1\n", "new": ""}], "exactly once"),
    ("new.py", [{"old": "", "new": "   "}], "empty"),
])
def test_dangerous_or_broken_changes_are_refused(folder: Path, path: str, edits: list[dict[str, str]],
                                                  why: str) -> None:
    with pytest.raises(FileRefused, match=why):
        view(folder).propose("ai", "svc", path, edits, "")


def test_removing_most_of_a_file_is_refused(folder: Path) -> None:
    text = (folder / "src" / "app.py").read_text()
    with pytest.raises(FileRefused, match="most of the file"):
        view(folder).propose("ai", "svc", "src/app.py", [{"old": text[10:], "new": ""}], "")


def test_risky_but_allowed_changes_carry_warnings(folder: Path) -> None:
    p = view(folder).propose("ai", "svc", "requirements.txt", [{"old": "flask", "new": "flask\nrequests"}], "")
    assert any("installed" in w for w in p.warnings)
    p = view(folder).propose("ai", "svc", "src/new_client.py",
                             [{"old": "", "new": "import requests\nrequests.get('https://example.com')\n"}], "")
    assert p.before is None and any("network" in w for w in p.warnings)


def test_windows_line_ends_still_match(folder: Path) -> None:
    (folder / "src" / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    p = view(folder).propose("ai", "svc", "src/win.py", [{"old": "a = 1\nb = 2", "new": "a = 1\nb = 3"}], "")
    assert p.after == "a = 1\r\nb = 3\r\n"


# -- secrets out of settings.json ------------------------------------------------------------------------------


def test_vault_file_fallback_is_private(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "secrets.json", use_keychain=False)
    assert vault.store("plugin.x.key", "abc123") == IN_FILE
    assert vault.load("plugin.x.key", IN_FILE) == "abc123"
    if os.name != "nt":
        assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0
    vault.delete("plugin.x.key")
    assert vault.load("plugin.x.key", IN_FILE) == ""
    assert vault.load("plugin.x.key", "an-old-plain-value") == "an-old-plain-value"


@pytest.mark.skipif(os.environ.get("PYTHONATOR_TEST_KEYCHAIN") != "1", reason="set PYTHONATOR_TEST_KEYCHAIN=1 "
                    "to try the real keychain of this computer")
def test_real_keychain_round_trip(tmp_path: Path) -> None:
    from pythonator.core import keychain

    keychain.set_secret("pythonator-test", "value-1")
    assert keychain.get_secret("pythonator-test") == "value-1"
    keychain.delete_secret("pythonator-test")
    assert keychain.get_secret("pythonator-test") is None


# -- end to end: a plugin reads, proposes, and the user decides ----------------------------------------------

BODY = """
import threading
from pythonator.sdk import Plugin, PluginError


class Reader(Plugin):
    def on_action(self, action, value):
        threading.Thread(target=self.go, args=(action, value), daemon=True).start()

    def go(self, action, value):
        sid = next(iter(self.services))
        try:
            if action == "list":
                self.log("LIST " + ",".join(sorted(f["path"] for f in self.list_files(sid)["files"])))
            elif action == "read":
                self.log("READ " + self.read_file(sid, value)["text"].replace(chr(10), "|"))
            elif action == "propose":
                self.log("PROPOSED " + self.propose_change(sid, "app.py", [("PORT = 80", "PORT = 81")], "fix"))
        except PluginError as exc:
            self.log("REFUSED " + str(exc))

    def on_proposal(self, proposal_id, status, note):
        self.log("ANSWER " + status)
"""


def _activity(h: DaemonHarness, pid: str) -> str:
    return "\n".join(e["text"] for e in h.events if e.get("ev") == "plugin_activity" and e.get("plugin") == pid)


def _install(h: DaemonHarness, tmp_path: Path, perms: list[str], live: bool) -> None:
    folder = write_plugin(tmp_path / "src", "reader", BODY, perms)
    toml = (folder / "plugin.toml").read_text().replace("api = 1", "api = 2")
    (folder / "plugin.toml").write_text(toml)
    h.ok({"op": "plugin_install", "source": str(folder)})
    h.ok({"op": "plugin_grant", "plugin": "reader", "permissions": perms, "live": live})
    h.ok({"op": "plugin_enable", "plugin": "reader", "enabled": True})
    h.wait(lambda: any(e.get("ev") == "plugins" and any(p["id"] == "reader" and p["running"] for p in e["plugins"])
                       for e in h.events), timeout=20_000)


def _act(h: DaemonHarness, action: str, value: str = "") -> None:
    h.ok({"op": "plugin_action", "plugin": "reader", "action": action, "value": value})


def test_plugin_reads_files_and_the_user_applies_a_change(harness: DaemonHarness, tmp_path: Path,
                                                            project: Path) -> None:
    (project / "app.py").write_text("PORT = 80\n", encoding="utf-8")
    (project / ".env").write_text("SECRET=topsecret99\n", encoding="utf-8")
    harness.add(name="web", folder=str(project), runtime="python", entry="app.py", env={"KEY": "hidden-value-1"})
    _install(harness, tmp_path, ["read_files", "propose_changes"], live=True)
    _act(harness, "list")
    harness.wait(lambda: "LIST " in _activity(harness, "reader"), timeout=20_000)
    assert "LIST app.py" in _activity(harness, "reader")
    _act(harness, "read", ".env")
    harness.wait(lambda: "REFUSED" in _activity(harness, "reader"), timeout=20_000)
    _act(harness, "propose")
    harness.wait(lambda: any(e.get("ev") == "proposal" for e in harness.events), timeout=20_000)
    proposal: dict[str, Any] = next(e["proposal"] for e in harness.events if e.get("ev") == "proposal")
    assert "+PORT = 81" in proposal["diff"] and proposal["path"] == "app.py"
    assert (project / "app.py").read_text() == "PORT = 80\n", "nothing is written before the user applies"
    note = harness.ok({"op": "plugin_proposal", "plugin": "reader", "proposal": proposal["id"], "apply": True})
    assert "Saved app.py" in note
    assert (project / "app.py").read_text() == "PORT = 81\n"
    harness.wait(lambda: "ANSWER applied" in _activity(harness, "reader"), timeout=20_000)
    ok, _data, err = harness.call({"op": "plugin_proposal", "plugin": "reader", "proposal": proposal["id"],
                                   "apply": True})
    assert not ok and "no longer waiting" in err


def test_without_permissions_or_in_test_mode_nothing_happens(harness: DaemonHarness, tmp_path: Path,
                                                             project: Path) -> None:
    (project / "app.py").write_text("PORT = 80\n", encoding="utf-8")
    harness.add(name="web", folder=str(project), runtime="python", entry="app.py")
    _install(harness, tmp_path, ["propose_changes"], live=False)
    _act(harness, "list")
    harness.wait(lambda: "REFUSED Needs the read_files" in _activity(harness, "reader"), timeout=20_000)
    _act(harness, "propose")
    harness.wait(lambda: "REFUSED Test mode" in _activity(harness, "reader"), timeout=20_000)
    assert not any(e.get("ev") == "proposal" for e in harness.events)


def test_secret_settings_stay_out_of_settings_json(harness: DaemonHarness, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    harness.daemon.plugins.vault.use_keychain = False  # the test machine's real keychain stays untouched
    folder = write_plugin(tmp_path / "src", "keyed", "from pythonator.sdk import Plugin\n\n\nclass K(Plugin):\n"
                          "    pass\n")
    with (folder / "plugin.toml").open("a", encoding="utf-8") as fh:
        fh.write('\n[[plugin.settings]]\nkey = "api_key"\nlabel = "Key"\nkind = "secret"\n')
    harness.ok({"op": "plugin_install", "source": str(folder)})
    harness.ok({"op": "plugin_settings", "plugin": "keyed", "values": {"api_key": "sk-test-1234567890"}})
    settings = harness.daemon.dirs.settings_file.read_text(encoding="utf-8")
    assert "sk-test-1234567890" not in settings
    assert json.loads(settings)["plugin_settings"]["keyed"]["api_key"] == IN_FILE
    plugin = harness.daemon.plugins.plugins["keyed"]
    assert harness.daemon.plugins._values(plugin)["api_key"] == "sk-test-1234567890"
    described = next(p for p in harness.daemon.plugins.describe() if p["id"] == "keyed")
    assert described["values"]["api_key"] == "••••••" and described["secrets_stored"]["api_key"] == "file"


# -- the panel's new widgets -----------------------------------------------------------------------------------


def test_panel_text_box_keeps_the_draft_and_sends_on_enter(qtbot: Any) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QComboBox, QPlainTextEdit

    from pythonator.sdk import ui
    from pythonator.ui.panels import PanelPage

    sent: list[tuple[str, str, str]] = []
    dock = PanelPage("ai", lambda plugin, action, value: sent.append((plugin, action, value)))
    qtbot.addWidget(dock)
    panel = ui.panel("AI", ui.select("pick", ["a", "b"], "b"), ui.code("x = 1"), ui.input("ask", "Ask…"))
    dock.show_panel(panel)
    box = dock.findChild(QPlainTextEdit, options=Qt.FindChildOption.FindChildrenRecursively)
    edits = [w for w in dock.findChildren(QPlainTextEdit) if not w.isReadOnly()]
    assert box is not None and len(edits) == 1
    edits[0].setPlainText("half a question")
    dock.show_panel(panel)  # the plugin redraws while the user types
    edits = [w for w in dock.findChildren(QPlainTextEdit) if not w.isReadOnly() and w.toPlainText()]
    assert [w.toPlainText() for w in edits] == ["half a question"]
    qtbot.keyClick(edits[0], Qt.Key.Key_Return)
    assert sent == [("ai", "ask", "half a question")]
    combo = dock.findChild(QComboBox)
    assert combo is not None and combo.currentText() == "b"
    combo.activated.emit(0)
    assert sent[-1] == ("ai", "pick", "a")
