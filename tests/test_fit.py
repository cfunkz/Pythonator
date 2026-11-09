"""Fit check: reads a folder, never runs it, and suggests a mode."""

from pythonator.policy.fit import scan


def test_python_api(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="api"\nrequires-python=">=3.12"\ndependencies=["httpx", "lxml>=5"]\n')
    (tmp_path / "main.py").write_text('import httpx\nhttpx.get("https://api.stripe.com/v1/x")\nimport uvicorn\nuvicorn.run(app, port=8000)\n')
    f = scan(str(tmp_path))
    assert (f.runtime, f.manifest, f.packages, f.native) == ("python", "pyproject", 2, ["lxml"])
    assert f.network and f.hosts == ["api.stripe.com"] and f.listens and f.port == 8000
    assert f.command == ["main.py"] and f.mode == "sandbox"


def test_node_worker(tmp_path):
    (tmp_path / "package.json").write_text('{"main": "worker.js", "dependencies": {"bullmq": "5"}}')
    (tmp_path / "worker.js").write_text("console.log('hi')\n")
    f = scan(str(tmp_path))
    assert (f.runtime, f.command, f.network, f.listens) == ("node", ["worker.js"], False, False)


def test_postgres_compose_suggests_container(tmp_path):
    (tmp_path / "compose.yaml").write_text("services:\n  db:\n    image: postgres:17\n")
    f = scan(str(tmp_path))
    assert (f.database, f.mode, f.image, f.port) == ("postgres", "container", "postgres:17", 5432)


def test_links_are_not_followed(tmp_path):
    import os

    import pytest

    secret = tmp_path / "secret.py"
    secret.write_text('x = "https://evil.example.org/steal"\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    try:
        os.symlink(secret, proj / "main.py")
    except OSError:
        pytest.skip("no symlinks here")
    f = scan(str(proj))
    assert f.hosts == [] and f.files == 0


def test_a_pyqt_app_is_a_window_app(tmp_path, monkeypatch):
    import sys

    (tmp_path / "main.py").write_text("import sys\nfrom PyQt6.QtWidgets import QApplication\n")
    for platform, mode in (("linux", "full"), ("win32", "sandbox")):
        monkeypatch.setattr(sys, "platform", platform)
        f = scan(str(tmp_path))
        assert (f.gui, f.mode, f.command) == (True, mode, ["main.py"])
    (tmp_path / "main.py").write_text("# uses tkinter later\nprint('hi')\n")
    assert not scan(str(tmp_path)).gui
