"""pythonatorctl is only the version and uninstall; the window does everything else."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from pythonator import __version__
from pythonator.ctl import main as ctl


def test_version(capsys):
    assert ctl.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"pythonator {__version__}"


@pytest.mark.parametrize("old", ["list", "add", "start", "logs", "plugins", "check", "daemon"])
def test_the_old_commands_are_gone(old):
    with pytest.raises(SystemExit):
        ctl.main([old])


@pytest.mark.skipif(sys.platform == "win32", reason="removes the folders the Linux installer makes")
@pytest.mark.parametrize("remove_data", [False, True])
def test_uninstall_takes_back_grants_and_removes_what_the_installer_made(tmp_path, monkeypatch, remove_data):
    from pythonator.service import osservice

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    data = tmp_path / "share" / "Pythonator"
    monkeypatch.setenv("PYTHONATOR_DATA", str(data))
    services = tmp_path / "services" / "api"
    app, links = ctl.install_dirs()
    for d in (app / "venv/bin", data, services, *(p.parent for p in links)):
        d.mkdir(parents=True, exist_ok=True)
    for p in links:
        p.write_text("x")
    (services / "main.py").write_text("print(1)")
    calls = []
    monkeypatch.setattr(ctl, "_call_daemon", lambda method, start, **kw: calls.append((method, start, kw)) or True)
    monkeypatch.setattr(osservice, "remove", lambda: (calls.append(("service.remove",)) or (True, "removed")))
    argv = ["uninstall"] + (["--remove-data"] if remove_data else [])
    assert ctl.main(argv) == 0
    assert calls[0] == ("shutdown", True, {"uninstall": True}), "folder grants are taken back before anything is deleted"
    assert ("service.remove",) in calls
    assert not app.exists() and not any(p.exists() for p in links)
    assert data.exists() is not remove_data
    assert (services / "main.py").exists(), "a service's own folder is never touched"


def test_uninstall_from_the_deb_stops_everything_and_leaves_the_files_to_apt(tmp_path, monkeypatch, capsys):
    from pythonator.service import osservice

    monkeypatch.setattr(sys, "prefix", "/opt/pythonator/python")
    monkeypatch.setenv("PYTHONATOR_DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    calls = []
    monkeypatch.setattr(ctl, "_call_daemon", lambda method, start, **kw: calls.append(method) or True)
    monkeypatch.setattr(osservice, "remove", lambda: (calls.append("service.remove") or (True, "removed")))
    monkeypatch.setattr(ctl, "_remove", lambda p: calls.append(("rm", p)))
    assert ctl.main(["uninstall"]) == 0
    assert calls == ["shutdown", "service.remove"], "nothing apt owns is deleted"
    assert "sudo apt remove pythonator" in capsys.readouterr().out


ROOT = Path(__file__).parent.parent


def test_the_website_links_to_this_version():
    site = re.search(r"^version: (\S+)", (ROOT / "_config.yml").read_text(encoding="utf-8"), re.M)
    assert site and re.fullmatch(re.escape(__version__) + r"(-[0-9A-Za-z.]+)?", site[1])


def test_the_windows_installer_takes_back_grants_and_matches_the_uninstaller():
    iss = (ROOT / "packaging" / "pythonator.iss").read_text(encoding="utf-8")
    assert iss.isascii()
    assert "PrivilegesRequired=lowest" in iss and r"DefaultDirName={localappdata}\Programs\Pythonator" in iss
    assert iss.index("shutdown --uninstall") < iss.index("service remove"), "grants are taken back first"
    assert r'Name: "{userprograms}\Pythonator"' in iss  # the shortcut pythonatorctl uninstall removes


def test_workflows_use_actions_pinned_by_commit():
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in wf.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                ref = line.split("uses:", 1)[1].split("#")[0].strip()
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{wf.name}: {ref}"
