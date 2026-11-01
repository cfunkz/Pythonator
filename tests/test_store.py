from __future__ import annotations

import json
from pathlib import Path

from pythonator.core.models import AppConfig, CloseAction, LaunchMode, RestartPolicy, ServiceConfig
from pythonator.core.store import ConfigStore, migrate_legacy


def test_roundtrip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "pythonator.json")
    cfg = AppConfig(services=[
        ServiceConfig(name="api", mode=LaunchMode.MODULE, target="uvicorn", args="app:app",
                      env={"PORT": "8000"}, restart=RestartPolicy.ALWAYS, autostart=True),
    ])
    cfg.settings.close_action = CloseAction.TRAY
    store.save(cfg)
    loaded = store.load()
    assert loaded.services == cfg.services
    assert loaded.settings.close_action is CloseAction.TRAY


def test_tolerates_bad_values(tmp_path: Path) -> None:
    path = tmp_path / "pythonator.json"
    path.write_text(json.dumps({
        "services": [
            {"name": "a", "id": "x", "mode": "bogus", "restart": 3, "stop_timeout": "soon", "env": [1]},
            {"name": "b", "id": "x"},
            "garbage",
        ],
        "settings": {"console_lines": -5, "log_backups": True},
    }))
    cfg = ConfigStore(path).load()
    a, b = cfg.services
    assert a.mode is LaunchMode.SCRIPT and a.restart is RestartPolicy.ON_FAILURE
    assert a.stop_timeout == 10.0 and a.env == {}
    assert b.id != "x"  # duplicate ids are re-issued
    assert cfg.settings.console_lines == 500
    assert cfg.settings.log_backups == 3


def test_corrupt_file_is_kept_aside(tmp_path: Path) -> None:
    path = tmp_path / "pythonator.json"
    path.write_text("{not json")
    assert ConfigStore(path).load().services == []
    assert (tmp_path / "pythonator.corrupt.json").read_text() == "{not json"


def test_legacy_migration(tmp_path: Path) -> None:
    legacy = {
        "bot": {"name": "bot", "entry": "/srv/bot/main.py", "reqs": "discord.py", "flags": "--debug",
                "custom_cmd": False, "python_path": ""},
        "web": {"name": "web", "entry": "/srv/web/main.py", "flags": "uvicorn main:app --port 8000",
                "custom_cmd": True},
        "pyweb": {"name": "pyweb", "entry": "/srv/x/a.py", "flags": "python -m flask run", "custom_cmd": True},
    }
    (tmp_path / "bots.json").write_text(json.dumps(legacy))
    store = ConfigStore(tmp_path / "pythonator.json", tmp_path / "bots.json")
    cfg = store.load()
    assert (tmp_path / "pythonator.json").exists()
    bot, web, pyweb = cfg.services
    assert (bot.mode, bot.target, bot.args, bot.requirements) == (LaunchMode.SCRIPT, "/srv/bot/main.py", "--debug",
                                                                  "discord.py")
    assert (web.mode, web.target, web.args) == (LaunchMode.MODULE, "uvicorn", "main:app --port 8000")
    assert Path(web.workdir) == Path("/srv/web")
    assert (pyweb.mode, pyweb.target, pyweb.args) == (LaunchMode.MODULE, "flask", "run")
    assert migrate_legacy({"x": "not a dict"}).services == []
