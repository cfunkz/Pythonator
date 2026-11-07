"""Models, the store, runtimes, schedules, ANSI colours and log masking."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pythonator.core import ansi, runtimes, schedule
from pythonator.core.logs import Masker, ServiceLog, secrets_of
from pythonator.core.models import Mode, Service, Settings
from pythonator.core.store import Store, import_old, write_atomic
from pythonator.paths import IS_WINDOWS, DataDirs


def svc(folder: Path, **kw: object) -> Service:
    return Service.model_validate({"name": "bot", "folder": str(folder), "entry": "main.py", **kw})


# -- models -----------------------------------------------------------------------------------------------


def test_cpu_limits(project: Path) -> None:
    from pythonator.core import limits

    assert svc(project).cpu_percent == 0
    with pytest.raises(ValidationError):
        svc(project, cpu_percent=-1)
    n = limits.cores()
    assert limits.effective(50) == (50 if n > 0.5 else 0)
    assert limits.effective(n * 100) == 0, "all cores is no limit"
    assert limits.job_cpu_rate(100) == round(10_000 / n)
    assert limits.job_cpu_rate(1) >= 1 and limits.job_cpu_rate(10**6) == 10_000
    assert limits.describe(150) == "1.5 cores (150% of one core)"
    assert limits.describe(100) == "1 core (100% of one core)"


def test_typed_limits_are_checked() -> None:
    from pythonator.core import limits

    total = 16 * 1024
    for text, mb in (("", 0), ("no limit", 0), ("512M", 512), ("2G", 2048), ("1.5 GB", 1536), ("1,5g", 1536),
                     ("800", 800), ("0.5 GiB", 512), ("65536k", 64)):
        assert limits.parse_memory(text, total) == (mb, ""), text
    assert limits.parse_memory("3T", total) == (total, "lowered to 16 GB, all the memory this computer has")
    for text, percent in (("", 0), ("1.5", 150), ("1.5 cores", 150), ("150%", 150), ("50 %", 50), (".5", 50)):
        assert limits.parse_cpu(text, 4) == (percent, ""), text
    assert limits.parse_cpu("9", 4) == (0, "all 4 cores is no limit")
    for bad in ("2 gigs", "-1G", "8", "1e9", "512M; rm -rf /", "nan"):
        with pytest.raises(limits.LimitError):
            limits.parse_memory(bad, total)
    for bad in ("abc", "0.01", "-2", "1.5 GB", "inf"):
        with pytest.raises(limits.LimitError):
            limits.parse_cpu(bad, 4)
    for mb in (512, 1536, 2048, 800):  # what the box shows reads back the same
        assert limits.parse_memory(limits.format_memory(mb), total)[0] == mb
    assert limits.parse_cpu(limits.format_cpu(150), 4)[0] == 150


def test_service_defaults_are_safe(project: Path) -> None:
    s = svc(project)
    assert s.mode is Mode.SANDBOX
    assert not s.network.enabled or s.network.expose.value == "local"
    assert not s.full_access_ok


def test_service_refuses_unknown_fields_and_bad_values(project: Path) -> None:
    assert not hasattr(svc(project, surprise=1), "surprise")  # files from newer versions: unknown fields dropped
    with pytest.raises(ValidationError):
        svc(project, runtime="cobol")
    with pytest.raises(ValidationError):
        svc(project, entry="--version")
    with pytest.raises(ValidationError):
        svc(project, env={"LD_PRELOAD": "/tmp/x.so"})
    with pytest.raises(ValidationError):
        svc(project, schedule="every 1s")
    with pytest.raises(ValidationError):
        svc(project, runtime="custom", command=[])


# -- store ------------------------------------------------------------------------------------------------


def test_store_round_trip_and_private_files(dirs: DataDirs, project: Path) -> None:
    store = Store(dirs)
    store.save_services([svc(project)])
    assert [s.name for s in store.load_services()] == ["bot"]
    if not IS_WINDOWS:  # Windows has no mode bits; the per-user profile folder's ACL keeps it private
        assert dirs.services_file.stat().st_mode & 0o077 == 0


def test_store_skips_bad_entries_and_keeps_a_copy(dirs: DataDirs, project: Path) -> None:
    good = svc(project).model_dump(mode="json")
    dirs.services_file.write_text(json.dumps({"version": 1, "services": [good, {"name": "\x00"}, good]}))
    store = Store(dirs)
    loaded = store.load_services()
    assert len(loaded) == 1  # the bad one skipped, the duplicate id dropped
    assert store.problems
    assert list(dirs.root.glob("services.json.bad*"))


def test_store_survives_garbage(dirs: DataDirs) -> None:
    dirs.services_file.write_text("{not json")
    dirs.settings_file.write_text('{"theme": "neon"}')
    store = Store(dirs)
    assert store.load_services() == []
    assert store.load_settings() == Settings()


def test_write_atomic_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    write_atomic(target, "1")
    write_atomic(target, "2")
    assert target.read_text() == "2"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_import_from_0_0_9(dirs: DataDirs, tmp_path: Path, project: Path) -> None:
    old = tmp_path / "pythonator.json"
    old.write_text(json.dumps({"services": [
        {"id": "AB12", "name": "old bot", "target": str(project / "bot.py"), "mode": "script", "args": "--x 1",
         "sandbox": True},
        {"name": "\x00broken"},
    ]}))
    services = import_old(dirs, old)
    assert [s.name for s in services] == ["old bot"]
    assert services[0].args == ["--x", "1"]
    assert services[0].mode is Mode.SANDBOX


# -- runtimes ---------------------------------------------------------------------------------------------


def test_detect(project: Path) -> None:
    (project / "go.mod").write_text("module x\n")
    rt, _entry = runtimes.detect(project)
    assert rt is not None and rt.key == "go"
    assert runtimes.for_entry("server.js") is runtimes.RUNTIMES["node"]
    assert runtimes.for_entry("app.jar") is runtimes.RUNTIMES["java"]


def test_run_commands() -> None:
    r = runtimes.RUNTIMES
    assert r["python"].run("/py", "main.py", ["-v"]) == ["/py", "-u", "main.py", "-v"]
    assert r["java"].run("/java", "app.jar", []) == ["/java", "-jar", "app.jar"]
    assert r["go"].run("/go", "", [])[:2] == ["/go", "run"]


def test_which_ignores_the_current_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert runtimes.which("python3") is None


def test_python_prefers_the_project_venv(project: Path) -> None:
    import venv

    venv.create(project / ".venv", with_pip=False)
    prog = runtimes.program(runtimes.RUNTIMES["python"], project)
    assert prog is not None and Path(prog).is_relative_to(project / ".venv")



def test_own_environment_needs_and_folders(project: Path) -> None:
    assert runtimes.env_missing("python", project) == "it has no virtual environment yet"
    assert runtimes.env_folders("python", project) == []
    (project / "package.json").write_text('{"dependencies": {"left-pad": "1"}}')
    assert runtimes.env_missing("node", project) == "node_modules is missing"
    (project / "node_modules").mkdir()
    assert runtimes.env_missing("node", project) == ""
    assert runtimes.env_folders("node", project) == [project / "node_modules"]
    assert runtimes.env_folders("python", project, container=True) == [project / ".venv-container"]
    assert "global packages" in runtimes.env_summary("python", project, own=False)


def test_own_environment_gets_a_private_cache(dirs: DataDirs, project: Path) -> None:
    from pythonator.isolation.base import env_cache, own_env_vars

    go = Service(name="g", folder=str(project), runtime="go", entry="main.go")
    env = own_env_vars(go, dirs)
    assert Path(env["GOMODCACHE"]).is_relative_to(env_cache(dirs, go)) and "-modcacherw" in env["GOFLAGS"]
    assert own_env_vars(go.model_copy(update={"own_env": False}), dirs) == {}
    assert own_env_vars(go.model_copy(update={"env": {"GOMODCACHE": "/mine"}}), dirs) == {}


def test_recreate_never_deletes_outside_the_project(tmp_path: Path, project: Path) -> None:
    from pythonator.daemon.supervisor import _remove_env

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(OSError):
        _remove_env(outside, [project.resolve()])
    with pytest.raises(OSError):
        _remove_env(project, [project.resolve()])
    assert outside.is_dir() and project.is_dir()
    inside = project / "node_modules" / "pkg"
    inside.mkdir(parents=True)
    (inside / "f.js").write_text("x")
    (inside / "f.js").chmod(0o400)
    _remove_env(project / "node_modules", [project.resolve()])
    assert not (project / "node_modules").exists()

# -- schedules, ANSI, logs --------------------------------------------------------------------------------


def test_schedules() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert (schedule.next_run("every 15m", now) - now).total_seconds() == 900
    assert schedule.next_run("0 3 * * *", now) == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
    assert schedule.next_run("@hourly", now).hour == 13
    for bad in ("every 5s", "every", "61 * * * *", "every 0m"):
        with pytest.raises(ValueError):
            schedule.check(bad)


def test_ansi_keeps_colours_and_drops_everything_else() -> None:
    runs = ansi.Parser().feed("\x1b[1;31merror\x1b[0m plain\x1b]0;evil title\x07\x1b[2J!")
    assert [t for t, _ in runs] == ["error", " plain", "!"]
    assert runs[0][1].bold and runs[0][1].fg


def test_ansi_across_chunks() -> None:
    p = ansi.Parser()
    first = p.feed("a\x1b[3")
    second = p.feed("2mb")
    assert "".join(t for t, _ in first + second) == "ab"
    assert second[-1][1].fg


def test_secrets_are_masked(tmp_path: Path) -> None:
    secrets = secrets_of({"DISCORD_TOKEN": "abc.def.ghi", "API_KEY": "sk-123456", "PORT": "8000", "PASSWORD": "x"})
    assert "8000" not in secrets and "x" not in secrets
    mask = Masker(secrets)
    assert "abc.def.ghi" not in mask("logged in with abc.def.ghi and sk-123456")
    log = ServiceLog(tmp_path / "s.log", max_bytes=200, backups=1)
    for i in range(50):
        log.write(f"line {i}\n")
    log.flush()
    assert "line 49" in log.tail()
    assert (tmp_path / "s.log.1").exists()
    assert sum(1 for _ in tmp_path.glob("s.log*")) == 2  # rotated, no more than one backup
    log.close()


def test_custom_commands_and_services_use_the_projects_own_environment(tmp_path: Path, monkeypatch: Any) -> None:
    """Like an activated venv: a custom command's program comes from the project's venv (and node_modules/.bin)
    first, PATH has them first, and Pythonator's own venv never leaks into a service."""
    import os

    from pythonator.isolation import base, service_argv

    bins = tmp_path / ".venv" / ("Scripts" if IS_WINDOWS else "bin")
    bins.mkdir(parents=True)
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /nowhere\n")
    tool = bins / ("uvicorn.exe" if IS_WINDOWS else "uvicorn")
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    svc = Service(name="c", folder=str(tmp_path), runtime="custom", command=["uvicorn", "app:app"])
    assert os.path.normcase(service_argv(svc)[0]) == os.path.normcase(str(tool))
    env = runtimes.activated({"PATH": "/usr/bin"}, tmp_path)
    assert env["PATH"].split(os.pathsep)[0] == str(bins) and env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "C:/Pythonator/.venv/Scripts/python.exe")
    assert "__PYVENV_LAUNCHER__" not in base.base_env(svc, sandboxed=False)


def test_windows_full_access_runs_a_venvs_python_without_its_redirector(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(runtimes, "IS_WINDOWS", True)
    home = tmp_path / "Python313"
    home.mkdir()
    (home / "python.exe").write_text("")
    scripts = tmp_path / "proj" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "pyvenv.cfg").write_text(f"home = {home}\n")
    venv_py = str(scripts / "python.exe")
    argv, env = runtimes.direct_python([venv_py, "-u", "app.py"], {"A": "1"})
    assert argv == [str(home / "python.exe"), "-u", "app.py"]
    assert env == {"A": "1", "__PYVENV_LAUNCHER__": venv_py}
    assert runtimes.direct_python(["node", "x.js"], {}) == (["node", "x.js"], {})
    (home / "python.exe").unlink()  # the base Python is gone: leave it to the venv's own launcher to say so
    assert runtimes.direct_python([venv_py], {})[0] == [venv_py]


def test_a_cron_line_with_seconds_is_refused() -> None:
    for text in ("* * * * * *", "*/1 * * * * *", "0 0 1 1 * 2027"):
        with pytest.raises(ValueError, match="Not a schedule"):
            schedule.check(text)
    for text in ("*/5 * * * *", "@daily", "every 15m", "0 9 * * 1-5"):
        assert schedule.check(text) == text


def test_a_six_field_schedule_saved_before_still_loads_without_its_seconds(project: Path) -> None:
    service = Service.model_validate({"name": "job", "folder": str(project), "runtime": "python", "entry": "a.py",
                                      "schedule": "0 */5 * * * *"})
    assert service.schedule == "*/5 * * * *"
    assert Service.model_validate({**service.model_dump(), "schedule": "every 30s"}).schedule == "every 30s"


def test_a_limit_too_big_for_a_number_is_an_error_not_a_crash() -> None:
    from pythonator.core import limits

    for parse in (limits.parse_memory, limits.parse_cpu):
        with pytest.raises(limits.LimitError):
            parse("9" * 400)
