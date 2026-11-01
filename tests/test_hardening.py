"""Failure handling: nothing the outside world does should crash Pythonator or lose data."""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

import pytest

from pythonator.core.fixes import (
    DependencyFix,
    VersionProbe,
    apply_to_requirements,
    closest_version,
    fix_from_probe,
    suggest_fix,
)
from pythonator.core.launch import LaunchSpec
from pythonator.core.logs import MAX_LINE_CHARS, LineKind, LogStore
from pythonator.core.models import AppConfig, Runtime, ServiceConfig
from pythonator.core.paths import DataPaths
from pythonator.core.store import ConfigStore
from pythonator.supervisor import Status, Supervisor

from .conftest import wait_until

PIP_NUMPY = [
    "ERROR: Ignored the following yanked versions: 2.4.0",
    "ERROR: Could not find a version that satisfies the requirement numpy==1.26.0 (from versions: 2.1.0, 2.1.1, "
    "2.4.0rc1, 2.4.1, 2.5.4)",
    "ERROR: No matching distribution found for numpy==1.26.0",
]


# -- dependency fixes -----------------------------------------------------------------------------


def test_pip_missing_version_suggests_closest() -> None:
    fix = suggest_fix(Runtime.PYTHON, PIP_NUMPY)
    assert isinstance(fix, DependencyFix)
    assert (fix.old, fix.new) == ("numpy==1.26.0", "numpy==2.1.0")
    assert "Type y or n" in fix.question


@pytest.mark.parametrize(
    ("wanted", "available", "best"),
    [
        ("1.26.0", ["2.1.0", "2.1.1", "2.5.4"], "2.1.0"),  # smallest upgrade
        ("3.0", ["1.0", "2.9.1", "2.10"], "2.10"),  # nothing newer: newest older one
        ("1.0", ["1.1rc1", "1.2"], "1.2"),  # skip pre-releases when there's a final one
        ("1.0", ["1.1rc1"], "1.1rc1"),
        ("2.1", ["2.1.0", "2.2"], "2.1.0"),  # 2.1 == 2.1.0
        ("1.0", [], None),
    ],
)
def test_closest_version(wanted: str, available: list[str], best: str | None) -> None:
    assert closest_version(wanted, available) == best


def test_pip_build_failure_asks_pip_for_wheels() -> None:
    out = ["Collecting numpy==1.26.0 (from -r requirements.txt (line 1))", "  Downloading numpy-1.26.0.tar.gz",
           "  error: metadata-generation-failed", "× Encountered error while generating package metadata."]
    probe = suggest_fix(Runtime.PYTHON, out)
    assert probe == VersionProbe("numpy", "numpy==1.26.0", "1.26.0")
    assert "--only-binary=:all:" in probe.pip_args()
    fix = fix_from_probe(probe, ["ERROR: Could not find a version that satisfies the requirement numpy==0.0.0.dev0 "
                                 "(from versions: 1.26.0, 2.1.0, 2.2.0)"])
    assert fix is not None and fix.new == "numpy==2.1.0"  # 1.26.0 itself is the one that failed
    wheel = ["Collecting uvloop==0.17.0", "  Failed building wheel for uvloop"]
    assert suggest_fix(Runtime.PYTHON, wheel) == VersionProbe("uvloop", "uvloop==0.17.0", "0.17.0")


@pytest.mark.parametrize(
    "line",
    [
        "npm error notarget No matching version found for express@99.0.0.",
        "npm ERR! notarget No matching version found for express@99.0.0.",
        'error Couldn\'t find any versions for "express" that matches "99.0.0"',
        'error: No version matching "99.0.0" found for specifier "express" (but package exists)',
    ],
)
def test_js_missing_version_suggests_latest(line: str) -> None:
    fix = suggest_fix(Runtime.NODE, [line])
    assert fix == DependencyFix("express", "express@99.0.0", "express@latest", "express has no version 99.0.0.")


def test_apply_fix_keeps_extras_markers_and_comments() -> None:
    fix = DependencyFix("numpy", "numpy==1.26.0", "numpy==2.1.0", "")
    text = "requests==2.0\nNumPy[extra] == 1.26.0 ; python_version > '3.8'  # pinned\n-r other.txt\n"
    assert apply_to_requirements(text, fix, Runtime.PYTHON) == (
        "requests==2.0\nNumPy[extra]==2.1.0 ; python_version > '3.8'  # pinned\n-r other.txt\n")
    assert apply_to_requirements("flask\n", fix, Runtime.PYTHON) is None
    js = DependencyFix("@discordjs/rest", "@discordjs/rest@9", "@discordjs/rest@latest", "")
    assert apply_to_requirements("discord.js@14\n@discordjs/rest@9", js, Runtime.NODE) == (
        "discord.js@14\n@discordjs/rest")


def _fake_install(sup: Supervisor, monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Make 'Install dependencies' print ``lines`` and fail, like pip would."""
    script = "import sys\nfor line in sys.argv[1:]:\n    print(line)\nsys.exit(1)\n"

    def fake(cfg: ServiceConfig, _req: Path, _env: object = None) -> LaunchSpec:
        return LaunchSpec([sys.executable, "-c", script, *lines], Path(cfg.workdir), {})

    monkeypatch.setattr("pythonator.supervisor.build_install", fake)


def test_install_failure_asks_and_fixes_requirements_txt(
    tmp_path: Path, qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "requirements.txt").write_text("requests\nnumpy==1.26.0\n")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="np", workdir=str(tmp_path), target="app.py"))
    _fake_install(sup, monkeypatch, PIP_NUMPY)
    assert sup.install_requirements(svc.id)
    assert wait_until(lambda: svc.question is not None, qt=True)
    assert "numpy==2.1.0" in svc.log.snapshot()[-1].text

    def state() -> tuple[bool, bool]:  # (asking?, task running?), read fresh each time
        return svc.question is not None, svc.task is not None

    assert sup.answer(svc.id, "maybe")  # not y/n: asked again, nothing changes
    assert state() == (True, False)
    assert sup.answer(svc.id, "y")
    assert (tmp_path / "requirements.txt").read_text() == "requests\nnumpy==2.1.0\n"
    assert state() == (False, True) and svc.task_label == "Install dependencies"  # retried right away
    # The fake pip fails the same way again, so it asks again rather than looping on its own.
    assert wait_until(lambda: state() == (True, False), qt=True)
    sup.shutdown()


def test_install_failure_fixes_the_service_package_list(
    tmp_path: Path, qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="np", workdir=str(tmp_path), target="app.py", requirements="numpy==1.26.0"))
    _fake_install(sup, monkeypatch, PIP_NUMPY)
    sup.install_requirements(svc.id)
    assert wait_until(lambda: svc.question is not None, qt=True)
    assert sup.answer(svc.id, "n")
    assert svc.question is None and svc.config.requirements == "numpy==1.26.0"
    sup.install_requirements(svc.id)
    assert wait_until(lambda: svc.question is not None, qt=True)
    sup.answer(svc.id, "yes")
    assert svc.config.requirements == "numpy==2.1.0"
    assert sup.get(svc.id) is svc
    sup.shutdown()


# -- logs -----------------------------------------------------------------------------------------


def test_runaway_lines_are_split(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "x.log")
    stream = store.stream()
    stream.write("x" * (MAX_LINE_CHARS * 2 + 10))  # no newline at all
    lines = store.drain()
    assert [len(ln.text) for ln in lines] == [MAX_LINE_CHARS, MAX_LINE_CHARS]
    stream.write("tail\n")
    assert store.drain()[0].text == "x" * 10 + "tail"
    stream.write("y" * (MAX_LINE_CHARS + 1) + "\nnext\n")
    assert [len(ln.text) for ln in store.drain()] == [MAX_LINE_CHARS, 1, 4]
    stream.close()
    store.close()


def test_progress_bars_still_collapse_when_long(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "x.log")
    stream = store.stream()
    for i in range(5000):  # 5000 \r updates ≈ 70k chars without a newline
        stream.write(f"\rprogress {i:05d}/5000 [{'#' * 0}]")
    stream.write("\n")
    texts = [ln.text for ln in store.drain()]
    assert texts[-1] == "progress 04999/5000 []"
    assert all(len(t) <= MAX_LINE_CHARS for t in texts)
    stream.close()
    store.close()


def test_log_file_errors_are_reported_once(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("not a folder")  # the log folder can't be created
    store = LogStore(blocker / "svc.log")
    store.system("one")
    store.system("two")
    errors = [ln for ln in store.snapshot() if ln.kind is LineKind.ERROR]
    assert len(errors) == 1 and "Could not write the log file" in errors[0].text
    assert store.file_error
    assert [ln.text for ln in store.snapshot() if ln.kind is LineKind.SYSTEM] == ["one", "two"]
    store.close()


# -- config ---------------------------------------------------------------------------------------


def test_corrupt_config_is_restored_from_backup(tmp_path: Path) -> None:
    path = tmp_path / "pythonator.json"
    store = ConfigStore(path)
    store.save(AppConfig(services=[ServiceConfig(name="first")]))
    store.save(AppConfig(services=[ServiceConfig(name="first"), ServiceConfig(name="second")]))
    path.write_text('{"services": [ {"name": "trunc')  # e.g. a crash mid-write by another tool
    again = ConfigStore(path)
    config = again.load()
    assert [s.name for s in config.services] == ["first"]  # the copy from before the last save
    assert again.recovered_from == path.with_name("pythonator.json.bak")
    assert path.with_suffix(".corrupt.json").exists()


def test_unreadable_config_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "data" / "pythonator.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"services": [{"name": "keep me"}]}))
    real_read = Path.read_text

    def deny(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", deny)
    sup = Supervisor(DataPaths(tmp_path / "data"))
    assert sup.config_warning.startswith("Settings are read-only")
    sup.add(ServiceConfig(name="new"))  # works in memory ...
    monkeypatch.setattr(Path, "read_text", real_read)
    assert "keep me" in path.read_text()  # ... but the file the user had is untouched
    sup.shutdown()


def test_save_failure_is_reported_not_raised(tmp_path: Path, qapp: object, monkeypatch: pytest.MonkeyPatch) -> None:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    notes: list[tuple[str, str]] = []
    sup.notify.connect(lambda t, m: notes.append((t, m)))

    def full(_config: AppConfig) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sup._store, "save", full)
    sup.add(ServiceConfig(name="a"))
    sup.add(ServiceConfig(name="b"))
    assert len(notes) == 1 and "No space left" in notes[0][1]  # said once, not on every change
    assert "couldn't be saved" in sup.config_warning
    sup.shutdown()


# -- processes ------------------------------------------------------------------------------------


def test_bad_values_fail_the_service_not_the_app(tmp_path: Path, qapp: object) -> None:
    (tmp_path / "a.py").write_text("print(1)")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="nul", target=str(tmp_path / "a.py"), python=sys.executable,
                                env={"TOKEN": "abc\x00def"}))  # Popen rejects NUL bytes
    assert not sup.start(svc.id)
    assert svc.status is Status.FAILED
    assert "Could not start" in svc.log.snapshot()[-1].text
    sup.shutdown()


def test_stats_thread_survives_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.core.stats import StatsSampler

    sampler = StatsSampler(interval=0.05)
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("psutil had a bad day")

    monkeypatch.setattr(sampler, "_sample_all", boom)
    sampler.start()
    assert wait_until(lambda: calls["n"] >= 3, timeout=5)  # still looping after the errors
    sampler.close()


def test_error_reporter_logs_instead_of_crashing(
    tmp_path: Path, qapp: object, caplog: pytest.LogCaptureFixture
) -> None:
    from pythonator.ui import errors

    reporter = errors.ErrorReporter(tmp_path / "pythonator.log")
    shown: list[str] = []
    reporter._report.disconnect()
    reporter._report.connect(lambda summary, _details: shown.append(summary))
    reporter.install()
    try:
        with caplog.at_level(logging.ERROR, logger="pythonator"):
            try:
                raise ValueError("slot blew up")
            except ValueError:
                sys.excepthook(*sys.exc_info())  # what PyQt calls for an exception in a slot
            worker = threading.Thread(target=lambda: 1 / 0, name="worker")
            worker.start()
            worker.join()
            assert wait_until(lambda: len(shown) == 2, qt=True)
    finally:
        reporter.uninstall()
    assert shown[0] == "ValueError: slot blew up" and shown[1].startswith("ZeroDivisionError")
    assert reporter.count == 2
    assert "slot blew up" in caplog.text and "worker" in caplog.text
