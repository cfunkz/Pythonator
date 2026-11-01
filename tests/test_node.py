"""Node.js runtime support. Tests that need a real ``node`` / ``npm`` are skipped when absent."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import psutil
import pytest

from pythonator.core.launch import (
    LaunchError,
    build_create_venv,
    build_install,
    build_launch,
    build_shell,
    node_bin_dirs,
)
from pythonator.core.models import LaunchMode, Runtime, ServiceConfig, runtime_for_path
from pythonator.core.paths import DataPaths
from pythonator.supervisor import Status, Supervisor

from .conftest import wait_until

HAS_NODE = shutil.which("node") is not None and shutil.which("npm") is not None
needs_node = pytest.mark.skipif(not HAS_NODE, reason="node/npm not installed")


def fake_tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        tool = directory / f"{name}.cmd"
        tool.write_text("@echo off\n")
    else:
        tool = directory / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    return tool


def test_runtime_detection() -> None:
    assert runtime_for_path("bot.js") is Runtime.NODE
    assert runtime_for_path("/srv/app/server.MJS") is Runtime.NODE
    assert runtime_for_path("main.ts") is Runtime.NODE
    assert runtime_for_path("main.py") is Runtime.PYTHON
    from pythonator.core.runtimes import runtime_info

    assert runtime_info(Runtime.NODE).mode_label(LaunchMode.MODULE).startswith("package.json")


def test_config_roundtrip_keeps_runtime() -> None:
    cfg = ServiceConfig(name="bot", runtime=Runtime.NODE, node="/opt/node/bin/node")
    again = ServiceConfig.from_dict(cfg.to_dict())
    assert again.runtime is Runtime.NODE and again.node == "/opt/node/bin/node"
    assert ServiceConfig.from_dict({"name": "old"}).runtime is Runtime.PYTHON


def test_node_script_and_npm_script(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    node = fake_tool(bin_dir, "node")
    npm = fake_tool(bin_dir, "npm")
    fake_tool(bin_dir, "pnpm")
    env = {"PATH": str(bin_dir), "PATHEXT": ".CMD;.EXE"}
    script = tmp_path / "bot.js"
    script.write_text("console.log('hi')\n")
    local_bin = fake_tool(tmp_path / "node_modules" / ".bin", "nodemon").parent

    spec = build_launch(ServiceConfig(name="bot", runtime=Runtime.NODE, target=str(script), args="--x 1"), env)
    assert Path(spec.argv[0]).stem == node.stem and spec.argv[1:] == [str(script), "--x", "1"]
    assert spec.env["PATH"].split(os.pathsep)[0] == str(local_bin)
    assert spec.env["FORCE_COLOR"] == "1"

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "node bot.js"}}))
    npm_cfg = ServiceConfig(name="web", runtime=Runtime.NODE, mode=LaunchMode.MODULE, target="start",
                            args="--port 3000", workdir=str(tmp_path))
    spec = build_launch(npm_cfg, env)
    assert Path(spec.argv[0]).stem == npm.stem
    assert spec.argv[1:] == ["run", "start", "--", "--port", "3000"]

    (tmp_path / "pnpm-lock.yaml").write_text("")
    spec = build_launch(npm_cfg, env)
    assert Path(spec.argv[0]).stem == "pnpm" and spec.argv[1:] == ["run", "start", "--port", "3000"]

    cmd = build_launch(npm_cfg.copy(mode=LaunchMode.COMMAND, target="nodemon bot.js", args=""), env)
    assert Path(cmd.argv[0]).parent == local_bin


def test_node_errors_and_install(tmp_path: Path) -> None:
    empty_env = {"PATH": str(tmp_path / "nothing")}
    script = tmp_path / "bot.js"
    script.write_text("")
    cfg = ServiceConfig(name="bot", runtime=Runtime.NODE, target=str(script))
    with pytest.raises(LaunchError, match=r"Node\.js was not found"):
        build_launch(cfg, empty_env)
    with pytest.raises(LaunchError, match=r"No package\.json"):
        build_launch(cfg.copy(mode=LaunchMode.MODULE, target="start"), {"PATH": os.defpath})
    with pytest.raises(LaunchError, match=r"don.t use a Python venv"):
        build_create_venv(cfg)

    bin_dir = tmp_path / "bin"
    fake_tool(bin_dir, "npm")
    fake_tool(bin_dir, "yarn")
    env = {"PATH": str(bin_dir), "PATHEXT": ".CMD;.EXE"}
    with pytest.raises(LaunchError, match="Nothing to install"):
        build_install(cfg, tmp_path / "req.txt", env)
    spec = build_install(cfg.copy(requirements="discord.js\n# comment\nexpress@5"), tmp_path / "r.txt", env)
    assert spec.argv[1:] == ["install", "discord.js", "express@5"]
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "yarn.lock").write_text("")
    assert Path(build_install(cfg, tmp_path / "r.txt", env).argv[0]).stem == "yarn"
    assert build_install(cfg.copy(requirements="chalk"), tmp_path / "r.txt", env).argv[1:] == ["add", "chalk"]


def test_node_bin_dirs_include_parents(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    child = tmp_path / "packages" / "api"
    (child / "node_modules" / ".bin").mkdir(parents=True)
    dirs = node_bin_dirs(child)
    assert dirs[:2] == [child / "node_modules" / ".bin", tmp_path / "node_modules" / ".bin"]


def test_shell_has_node_bins(tmp_path: Path) -> None:
    local_bin = fake_tool(tmp_path / "node_modules" / ".bin", "tsc").parent
    spec = build_shell(ServiceConfig(name="x", runtime=Runtime.NODE, workdir=str(tmp_path)), "tsc -v")
    assert str(local_bin) in spec.env["PATH"]


@needs_node
def test_supervisor_runs_node_and_npm(tmp_path: Path, qapp: object) -> None:
    (tmp_path / "server.js").write_text(
        "process.stdin.on('data', d => console.log('echo:', d.toString().trim()));\n"
        "console.log('\\x1b[32mnode up\\x1b[0m', process.version);\n"
    )
    hello = "node -e \"console.log('from npm', process.argv.slice(1).join(' '))\""
    (tmp_path / "package.json").write_text(json.dumps({"name": "t", "scripts": {"hello": hello}}))
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="node", runtime=Runtime.NODE, target=str(tmp_path / "server.js")))

    def text(sid: str) -> str:
        s = sup.get(sid)
        assert s is not None
        return "\n".join(ln.text for ln in s.log.snapshot())

    assert sup.start(svc.id)
    assert wait_until(lambda: "node up" in text(svc.id), timeout=30, qt=True), text(svc.id)  # cold start on CI
    assert sup.send_input(svc.id, "ping")
    assert wait_until(lambda: "echo: ping" in text(svc.id), qt=True)
    sup.stop(svc.id)
    assert wait_until(lambda: svc.status is Status.STOPPED, qt=True)

    npm = sup.add(ServiceConfig(name="npm", runtime=Runtime.NODE, mode=LaunchMode.MODULE, target="hello",
                                args='a b "x&y"', workdir=str(tmp_path)))
    assert sup.start(npm.id)
    assert wait_until(lambda: npm.status is Status.EXITED, timeout=30, qt=True), text(npm.id)
    assert "from npm a b x&y" in text(npm.id)  # on Windows npm is npm.cmd: & must not split the command
    sup.shutdown()


def test_ui_highlighting_and_comments(qapp: object) -> None:
    from pythonator.core.locations import find_location
    from pythonator.ui.code_editor import CodeEditor
    from pythonator.ui.highlighter import JsHighlighter, language_for

    assert language_for("a.ts") == "javascript" and language_for("a.py") == "python"
    editor = CodeEditor()
    editor.set_language("javascript")
    assert isinstance(editor._highlighter, JsHighlighter)
    editor.setPlainText("const a = `multi\nline`;\n/* block\ncomment */ let b = 1;")
    editor.selectAll()
    editor.toggle_comment()
    assert all(line.startswith("// ") for line in editor.toPlainText().splitlines())
    editor.selectAll()
    editor.toggle_comment()
    assert editor.toPlainText().startswith("const a")

    assert find_location("    at Object.<anonymous> (/srv/bot/index.js:12:5)") == ("/srv/bot/index.js", 12)
    assert find_location("    at main (file:///C:/bots/app.mjs:3:1)") == ("C:/bots/app.mjs", 3)


@needs_node
@pytest.mark.parametrize("via", ["service", "shell"])
def test_npm_run_stops_with_its_whole_tree(via: str, tmp_path: Path, qapp: object) -> None:
    """npm → (cmd / sh) → node: Stop must leave none of them running, on Windows too."""
    from .test_runtimes import _alive, _process_tree

    (tmp_path / "server.js").write_text("console.log('dev server up');\nsetInterval(() => {}, 1000);\n")
    (tmp_path / "package.json").write_text(json.dumps({"name": "t", "scripts": {"dev": "node server.js"}}))
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="dev", runtime=Runtime.NODE, mode=LaunchMode.MODULE, target="dev",
                                workdir=str(tmp_path)))

    def text() -> str:
        return "\n".join(ln.text for ln in svc.log.snapshot())

    if via == "service":
        assert sup.start(svc.id), text()
    else:
        assert sup.run_shell(svc.id, "npm run dev"), text()
    assert wait_until(lambda: "dev server up" in text(), timeout=60, qt=True), text()
    runner = svc.runner if via == "service" else svc.task
    assert runner is not None
    tree = _process_tree(runner.pid)
    assert any(_is_node(p) for p in tree[1:]), [p.name() for p in tree]
    sup.stop(svc.id)
    assert wait_until(lambda: svc.task is None and not svc.status.active, timeout=20, qt=True), text()
    assert wait_until(lambda: not _alive(tree), timeout=10), f"left running: {_alive(tree)}"
    sup.shutdown()


def _is_node(proc: psutil.Process) -> bool:
    """Whether ``proc`` is node. Newer Node versions on Linux name their main thread "MainThread",
    which is what the process name shows, so also look at the executable and command line."""
    try:
        names = [proc.name(), Path(proc.exe() or "").name, *(Path(a).name for a in proc.cmdline()[:1])]
    except psutil.Error:
        return False
    return any("node" in n.lower() for n in names)
