from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pythonator.core.launch import (
    LaunchError,
    build_create_venv,
    build_install,
    build_launch,
    build_shell,
    expand_vars,
    find_venv,
    parse_env,
    split_args,
    venv_python,
)
from pythonator.core.models import LaunchMode, ServiceConfig

WINDOWS = os.name == "nt"


def make_venv(root: Path) -> Path:
    venv = root / ".venv"
    py = venv_python(venv)
    py.parent.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    if WINDOWS:
        py.write_bytes(Path(sys.executable).read_bytes())
    else:
        py.symlink_to(sys.executable)
    return venv


def test_split_args() -> None:
    assert split_args('--name "hello world" -v') == ["--name", "hello world", "-v"]
    assert split_args("") == []
    if not WINDOWS:
        with pytest.raises(LaunchError):
            split_args('"unterminated')


def test_parse_env_and_expand() -> None:
    env = parse_env("# comment\nexport A=1\nB = 'two words'\nbad line\nC=x=y\n")
    assert env == {"A": "1", "B": "two words", "C": "x=y"}
    assert expand_vars("$A-${B}-%C%-$MISSING", {"A": "1", "B": "2", "C": "3"}) == "1-2-3-$MISSING"


def test_script_uses_workdir_venv(tmp_path: Path) -> None:
    script = tmp_path / "bot.py"
    script.write_text("print('hi')\n")
    venv = make_venv(tmp_path)
    assert find_venv(tmp_path) == venv
    spec = build_launch(ServiceConfig(name="bot", target=str(script), args="--x 1", env={"TOKEN": "$HOME"}),
                        {"PATH": "/usr/bin", "HOME": "/home/me", "PYTHONHOME": "/bad"})
    assert spec.argv == [str(venv_python(venv)), "-u", str(script), "--x", "1"]
    assert spec.cwd == tmp_path
    assert spec.env["VIRTUAL_ENV"] == str(venv)
    assert spec.env["PATH"].startswith(str(venv_python(venv).parent))
    assert spec.env["TOKEN"] == "/home/me"
    assert spec.env["PYTHONUNBUFFERED"] == "1"
    assert "PYTHONHOME" not in spec.env


def test_module_mode(tmp_path: Path) -> None:
    cfg = ServiceConfig(name="web", mode=LaunchMode.MODULE, target="http.server", args="8000",
                        workdir=str(tmp_path), python=sys.executable)
    spec = build_launch(cfg)
    assert spec.argv[1:] == ["-u", "-m", "http.server", "8000"]


def test_command_mode_resolves_on_venv_path(tmp_path: Path) -> None:
    venv = make_venv(tmp_path)
    tool = venv_python(venv).parent / ("mytool.bat" if WINDOWS else "mytool")
    tool.write_text("@echo off\n" if WINDOWS else "#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    cfg = ServiceConfig(name="cmd", mode=LaunchMode.COMMAND, target="mytool --flag", workdir=str(tmp_path))
    spec = build_launch(cfg, {"PATH": os.defpath, "PATHEXT": ".BAT;.EXE"})
    assert Path(spec.argv[0]).name.lower() == tool.name.lower()
    assert spec.argv[1:] == ["--flag"]
    with pytest.raises(LaunchError, match="not found"):
        build_launch(cfg.copy(target="definitely-not-a-real-tool-xyz"), {"PATH": os.defpath})


def test_errors(tmp_path: Path) -> None:
    with pytest.raises(LaunchError, match="No script"):
        build_launch(ServiceConfig(name="x", workdir=str(tmp_path)))
    with pytest.raises(LaunchError, match="Script not found"):
        build_launch(ServiceConfig(name="x", target=str(tmp_path / "missing.py")))
    with pytest.raises(LaunchError, match="Working directory"):
        build_launch(ServiceConfig(name="x", target="a.py", workdir=str(tmp_path / "nope")))
    with pytest.raises(LaunchError, match="interpreter not found"):
        build_launch(ServiceConfig(name="x", mode=LaunchMode.MODULE, target="m", workdir=str(tmp_path),
                                   python=str(tmp_path / "nopython")))


def test_tasks(tmp_path: Path) -> None:
    cfg = ServiceConfig(name="x", workdir=str(tmp_path), target="a.py")
    create = build_create_venv(cfg)
    assert create.argv[1:] == ["-m", "venv", str(tmp_path / ".venv")]
    with pytest.raises(LaunchError, match="No virtual environment"):
        build_install(cfg, tmp_path / "req.txt")
    make_venv(tmp_path)
    with pytest.raises(LaunchError, match="already exists"):
        build_create_venv(cfg)
    with pytest.raises(LaunchError, match="Nothing to install"):
        build_install(cfg, tmp_path / "req.txt")
    spec = build_install(cfg.copy(requirements="requests\nrich"), tmp_path / "req" / "x.txt")
    assert spec.argv[-2:] == ["-r", str(tmp_path / "req" / "x.txt")]
    assert (tmp_path / "req" / "x.txt").read_text() == "requests\nrich\n"
    shell = build_shell(cfg, "pip list")
    assert shell.shell and shell.argv[-1] == "pip list"
