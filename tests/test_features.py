"""Folder tree, console links / server URLs, schedules and .env files."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from pythonator.core.launch import LaunchError, build_launch, format_env, parse_env
from pythonator.core.logs import LineKind, LogLine, LogStore
from pythonator.core.models import RestartPolicy, ServiceConfig
from pythonator.core.paths import DataPaths
from pythonator.core.schedule import Cron, Daily, Interval, ScheduleError, parse_schedule, upcoming
from pythonator.core.urls import local_url
from pythonator.supervisor import Status, Supervisor, format_next_run

from .conftest import wait_until

BASE = datetime(2026, 10, 31, 14, 7, 30)  # a Saturday


# -- schedules ------------------------------------------------------------------------------------


def fmt(times: list[datetime]) -> list[str]:
    return [t.strftime("%a %d %H:%M:%S") for t in times]


def test_interval_schedule() -> None:
    s = parse_schedule("every 15m")
    assert isinstance(s, Interval) and s.seconds == 900
    assert fmt(upcoming(s, 2, BASE)) == ["Sat 31 14:22:30", "Sat 31 14:37:30"]
    assert parse_schedule("Every 2 hours") == Interval("Every 2 hours", 7200)
    assert parse_schedule("every 30 sec").describe() == "every 30s"  # type: ignore[union-attr]
    assert parse_schedule("  ") is None


def test_daily_schedule() -> None:
    s = parse_schedule("18:30, 9:00")
    assert isinstance(s, Daily) and s.times == ((9, 0), (18, 30))
    assert fmt(upcoming(s, 3, BASE)) == ["Sat 31 18:30:00", "Sun 01 09:00:00", "Sun 01 18:30:00"]
    assert isinstance(parse_schedule("daily 03:05"), Daily)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("*/5 * * * *", ["Sat 31 14:10:00", "Sat 31 14:15:00"]),
        ("0 3 * * mon-fri", ["Mon 02 03:00:00", "Tue 03 03:00:00"]),
        ("@weekly", ["Sun 01 00:00:00", "Sun 08 00:00:00"]),
        ("0 9-17/2 * * *", ["Sat 31 15:00:00", "Sat 31 17:00:00"]),
        ("15 14 1 * *", ["Sun 01 14:15:00", "Tue 01 14:15:00"]),
        ("0 0 13 * fri", ["Fri 06 00:00:00", "Fri 13 00:00:00"]),  # day-of-month OR day-of-week
        ("0 12 * * 7", ["Sun 01 12:00:00", "Sun 08 12:00:00"]),  # 7 is Sunday too
        ("0 0 29 feb *", ["Tue 29 00:00:00", "Sun 29 00:00:00"]),  # leap days only
    ],
)
def test_cron_schedule(expr: str, expected: list[str]) -> None:
    s = parse_schedule(expr)
    assert isinstance(s, Cron)
    assert fmt(upcoming(s, 2, BASE)) == expected


@pytest.mark.parametrize("bad", ["every 5x", "every 0s", "25:00", "* * *", "61 * * * *", "*/0 * * * *",
                                 "0 0 30 2 *", "5-1 * * * *", "0 0 * * funday"])
def test_bad_schedules_explain_themselves(bad: str) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(bad)


def test_format_next_run() -> None:
    now = datetime(2026, 10, 31, 9, 0).timestamp()
    assert format_next_run(datetime(2026, 10, 31, 14, 30).timestamp(), now) == "14:30"
    assert format_next_run(datetime(2026, 11, 1, 3, 0).timestamp(), now) == "tomorrow 03:00"
    assert format_next_run(datetime(2026, 11, 5, 3, 0).timestamp(), now) == "Thu 05 Nov 03:00"


def test_supervisor_runs_on_schedule(tmp_path: Path, qapp: object) -> None:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    body = "import sys, time\nprint('job', flush=True)\ntime.sleep(float(sys.argv[1]))\n"
    (tmp_path / "job.py").write_text(body)
    svc = sup.add(ServiceConfig(name="job", target=str(tmp_path / "job.py"), args="0", python=sys.executable,
                                restart=RestartPolicy.NEVER, schedule="every 1h"))
    assert svc.next_run == pytest.approx(time.time() + 3600, abs=5)
    due = svc.next_run
    def status() -> Status:  # a call, so mypy doesn't narrow the attribute between checks
        return svc.status

    sup._on_clock(now=due - 1)
    assert status() is Status.STOPPED  # not yet
    sup._on_clock(now=due + 0.5)
    assert status() is Status.RUNNING
    assert svc.next_run == pytest.approx(due + 3600)  # continues from the planned time: no drift
    assert wait_until(lambda: svc.status is Status.EXITED, qt=True)
    text = "\n".join(ln.text for ln in svc.log.snapshot())
    assert "⏱ Scheduled run (every 1h)" in text and "job" in text

    # Asleep for a day: one catch-up run, then back on the hourly grid from now.
    late = svc.next_run + 86400
    sup._on_clock(now=late)
    assert svc.next_run == pytest.approx(late + 3600)
    assert wait_until(lambda: svc.status is Status.EXITED, qt=True)

    # A run that's still going is skipped, not started twice.
    sup.update(svc.config.copy(args="30"))
    sup.start(svc.id)
    sup._on_clock(now=svc.next_run)
    assert "skipped" in svc.log.snapshot()[-1].text
    assert svc.status is Status.RUNNING

    sup.update(svc.config.copy(schedule=""))
    assert svc.next_run == 0.0
    sup.shutdown()


def test_invalid_saved_schedule_is_reported_not_fatal(tmp_path: Path, qapp: object) -> None:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="x", target="x.py", schedule="every banana"))
    assert svc.next_run == 0.0
    last = svc.log.snapshot()[-1]
    assert last.kind is LineKind.ERROR and "Schedule ignored" in last.text
    sup.shutdown()


# -- .env files -----------------------------------------------------------------------------------


def test_parse_env_like_dotenv() -> None:
    text = (
        "A=1 # comment\n"
        'B="two words" # c\n'
        "C='lit\\n'\n"
        'D="x\\ny"\n'
        "E=http://h/#frag\n"
        "export F = 'q'\n"
        "G=\n"
        "# whole-line comment\n"
    )
    assert parse_env(text) == {"A": "1", "B": "two words", "C": "lit\\n", "D": "x\ny", "E": "http://h/#frag",
                               "F": "q", "G": ""}


def test_format_env_round_trips() -> None:
    env = {"a": "x #y", "b": " pad", "c": "it's # x", "d": "l1\nl2", "e": "C:\\new", "f": "plain", "g": "'q'"}
    assert parse_env(format_env(env)) == env


def test_env_file_is_loaded_and_service_vars_win(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=from-file\nPORT=8000\nURL=http://localhost:${PORT}\n")
    script = tmp_path / "bot.py"
    script.write_text("")
    cfg = ServiceConfig(name="bot", target=str(script), python=sys.executable, env_file=".env",
                        env={"PORT": "9000"})
    env = build_launch(cfg).env
    assert env["TOKEN"] == "from-file"
    assert env["PORT"] == "9000"
    assert env["URL"] == "http://localhost:8000"  # expanded while the file was read

    with pytest.raises(LaunchError, match="env file"):
        build_launch(cfg.copy(env_file="missing.env"))


def test_env_file_survives_config_round_trip() -> None:
    cfg = ServiceConfig(name="x", env_file=".env.prod", schedule="0 3 * * *")
    again = ServiceConfig.from_dict(cfg.to_dict())
    assert (again.env_file, again.schedule) == (".env.prod", "0 3 * * *")
    old = ServiceConfig.from_dict({"name": "old"})
    assert (old.env_file, old.schedule) == ("", "")


# -- URLs -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "url"),
    [
        (" * Running on http://127.0.0.1:5000/ (Press CTRL+C to quit)", "http://127.0.0.1:5000/"),
        ("INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)", "http://localhost:8000"),
        ("  ➜  Local:   \x1b[36mhttp://localhost:\x1b[1m5173\x1b[22m/\x1b[39m", "http://localhost:5173/"),
        ("Serving HTTP on :: port 8000 (http://[::]:8000/) ...", "http://localhost:8000/"),
        ("Listening at http://localhost:3000.", "http://localhost:3000"),
        ("POST https://discord.com/api/v10/channels 200", None),
        ("no url here", None),
    ],
)
def test_local_url(line: str, url: str | None) -> None:
    assert local_url(line) == url


def test_log_store_remembers_first_local_url(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "x.log")
    stream = store.stream()
    stream.write("GET https://example.com/a\nStarted http://127.0.0.1:8080/\n")
    stream.write("also http://localhost:9999/\n")
    assert store.url == "http://127.0.0.1:8080/"
    store.reset_url()
    assert not store.url
    stream.close()
    store.close()


def test_console_links_open(qapp: object) -> None:
    from pythonator.ui.console import ConsoleView

    view = ConsoleView()
    view.resize(800, 200)
    view.set_show_timestamps(True)
    view.append_lines([LogLine(0, "🚀 \x1b[36mhttp://localhost:\x1b[1m5173\x1b[22m/\x1b[39m ready")])
    doc = view.document()
    assert doc is not None
    block = doc.firstBlock()
    anchors: list[tuple[str, str]] = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.charFormat().isAnchor():
            anchors.append((frag.text(), frag.charFormat().anchorHref()))
        it += 1
    # The whole URL is one link, although the port is in a different colour, and the emoji
    # (two UTF-16 units) before it doesn't shift the range.
    assert "".join(t for t, _ in anchors) == "http://localhost:5173/"
    assert {href for _, href in anchors} == {"http://localhost:5173/"}
    assert block.text().endswith("🚀 http://localhost:5173/ ready")


# -- folder tree ----------------------------------------------------------------------------------


def test_file_tree_actions(tmp_path: Path, qapp: object) -> None:
    from pythonator.ui.file_tree import FileTree

    project = tmp_path / "proj"
    (project / "__pycache__").mkdir(parents=True)
    (project / "bot.py").write_text("print(1)\n")
    tree = FileTree()
    opened: list[str] = []
    renamed: list[tuple[str, str]] = []
    tree.open_file.connect(opened.append)
    tree.renamed.connect(lambda a, b: renamed.append((a, b)))

    tree.set_root(project)
    assert tree.root == project and tree.view.isVisibleTo(tree)

    def names() -> list[str]:
        root = tree.view.rootIndex()
        return [str(tree.proxy.index(r, 0, root).data()) for r in range(tree.proxy.rowCount(root))]

    assert wait_until(lambda: names() == ["bot.py"], qt=True)  # caches are hidden

    made = tree.new_file(project, "src/app.js")
    assert made == project / "src" / "app.js" and made.is_file()
    assert opened == [str(made)]
    assert tree.new_folder(project, "data") == project / "data"
    assert wait_until(lambda: names() == ["data", "src", "bot.py"], qt=True)  # folders first

    moved = tree.rename(project / "bot.py", "main.py")
    assert moved == project / "main.py" and moved.is_file()
    assert renamed == [(str(project / "bot.py"), str(project / "main.py"))]
    assert tree.rename(project / "main.py", "") is None

    assert tree.delete(project / "data", confirm=False)
    assert not (project / "data").exists()

    tree.set_root(tmp_path / "missing")
    assert not tree.view.isVisibleTo(tree)
    assert not tree.root


def test_editor_follows_renamed_files(tmp_path: Path, qapp: object) -> None:
    from pythonator.ui.editor_panel import EditorPanel

    folder = tmp_path / "pkg"
    folder.mkdir()
    (folder / "a.py").write_text("x = 1\n")
    panel = EditorPanel()
    tab = panel.open_file(str(folder / "a.py"))
    assert tab is not None
    (folder / "a.py").rename(folder / "b.js")
    panel.path_moved(str(folder / "a.py"), str(folder / "b.js"))
    assert tab.path == str((folder / "b.js").resolve()) and tab.language == "javascript"
    # A folder rename moves every open file inside it. (Not renamed on disk here: Windows refuses to
    # rename a folder while a file watcher holds it open, which isn't what this test is about.)
    panel.path_moved(str(folder), str(tmp_path / "lib"))
    assert tab.path == str((tmp_path / "lib" / "b.js").resolve())
