"""Every runtime: launch commands, installs, discovery, real runs where the tool exists."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from pythonator.core.launch import LaunchError, build_install, build_launch
from pythonator.core.models import ConsoleShell, LaunchMode, Runtime, ServiceConfig, runtime_for_path
from pythonator.core.paths import IS_WINDOWS, DataPaths
from pythonator.core.runtimes import JAVA_DEFAULTS, RUBY_BOOT, RUNTIMES, find_program, runtime_info
from pythonator.supervisor import Status, Supervisor

from .conftest import wait_until
from .test_node import fake_tool


@pytest.fixture
def tools(tmp_path: Path) -> dict[str, str]:
    """A PATH with fake versions of every runtime and package manager."""
    bin_dir = tmp_path / "bin"
    for name in ("go", "java", "dotnet", "ruby", "bundle", "gem", "php", "composer", "bash", "pwsh", "deno",
                 "bun", "python3", "node", "npm"):
        fake_tool(bin_dir, name)
    return {"PATH": str(bin_dir), "PATHEXT": ".CMD;.EXE"}


def argv(cfg: ServiceConfig, env: dict[str, str]) -> list[str]:
    spec = build_launch(cfg, env)
    return [Path(spec.argv[0]).stem if i == 0 and Path(a).is_absolute() else a for i, a in enumerate(spec.argv)]


def script(tmp_path: Path, name: str, body: str = "") -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return str(path)


# -- the table itself -------------------------------------------------------------------------------


def test_every_runtime_is_described() -> None:
    assert set(RUNTIMES) == set(Runtime)
    for runtime, info in RUNTIMES.items():
        assert LaunchMode.SCRIPT in info.modes and LaunchMode.COMMAND in info.modes, runtime
        assert (LaunchMode.MODULE in info.modes) == (info.module is not None), runtime
        assert info.install is not None or info.no_install or runtime is Runtime.PYTHON, runtime
        assert runtime.label


@pytest.mark.parametrize(
    ("path", "runtime"),
    [("main.go", Runtime.GO), ("server.jar", Runtime.JAVA), ("Bot.java", Runtime.JAVA),
     ("Bot.csproj", Runtime.DOTNET),
     ("app.cs", Runtime.DOTNET), ("bot.rb", Runtime.RUBY), ("worker.php", Runtime.PHP), ("run.sh", Runtime.SHELL),
     ("start.BAT", Runtime.SHELL), ("task.ps1", Runtime.POWERSHELL), ("caddy.exe", Runtime.EXECUTABLE),
     ("bot.py", Runtime.PYTHON), ("bot.js", Runtime.NODE), ("README", Runtime.PYTHON),
     ("dir.v2/README", Runtime.PYTHON)],
)
def test_runtime_for_path(path: str, runtime: Runtime) -> None:
    assert runtime_for_path(path) is runtime


# -- launch commands --------------------------------------------------------------------------------


def test_compiled_runtimes(tmp_path: Path, tools: dict[str, str]) -> None:
    go = ServiceConfig(name="g", runtime=Runtime.GO, target=script(tmp_path, "main.go"), args="-v",
                       runtime_args="-race")
    assert argv(go, tools) == ["go", "run", "-race", str(tmp_path / "main.go"), "-v"]
    pkg = go.copy(mode=LaunchMode.MODULE, target="./cmd/bot", workdir=str(tmp_path), runtime_args="")
    assert argv(pkg, tools) == ["go", "run", "./cmd/bot", "-v"]

    jar = ServiceConfig(name="j", runtime=Runtime.JAVA, target=script(tmp_path, "server.jar"), args="nogui",
                        runtime_args="-Xmx2G")
    assert argv(jar, tools) == ["java", *JAVA_DEFAULTS, "-Xmx2G", "-jar", str(tmp_path / "server.jar"), "nogui"]
    src = jar.copy(target=script(tmp_path, "Bot.java"), runtime_args="", args="")
    assert argv(src, tools) == ["java", *JAVA_DEFAULTS, str(tmp_path / "Bot.java")]
    with pytest.raises(LaunchError, match="jar"):
        build_launch(jar.copy(target=script(tmp_path, "notes.txt")), tools)

    dll = ServiceConfig(name="d", runtime=Runtime.DOTNET, target=script(tmp_path, "Bot.dll"), args="--prod")
    assert argv(dll, tools) == ["dotnet", str(tmp_path / "Bot.dll"), "--prod"]
    proj = dll.copy(target=script(tmp_path, "Bot.csproj"), runtime_args="-c Release")
    assert argv(proj, tools) == ["dotnet", "run", "--project", str(tmp_path / "Bot.csproj"), "-c", "Release",
                                 "--", "--prod"]


def test_scripting_runtimes(tmp_path: Path, tools: dict[str, str]) -> None:
    rb = ServiceConfig(name="r", runtime=Runtime.RUBY, target=script(tmp_path, "bot.rb"), args="x")
    assert argv(rb, tools) == ["ruby", "-e", RUBY_BOOT, str(tmp_path / "bot.rb"), "x"]
    bundle = rb.copy(mode=LaunchMode.MODULE, target="puma -p 9292", args="", workdir=str(tmp_path))
    assert argv(bundle, tools) == ["bundle", "exec", "puma", "-p", "9292"]

    php = ServiceConfig(name="p", runtime=Runtime.PHP, target=script(tmp_path, "w.php"))
    assert argv(php, tools) == ["php", str(tmp_path / "w.php")]
    server = php.copy(mode=LaunchMode.MODULE, target="localhost:8000", args="-t public", workdir=str(tmp_path))
    assert argv(server, tools) == ["php", "-S", "localhost:8000", "-t", "public"]

    sh = ServiceConfig(name="s", runtime=Runtime.SHELL, target=script(tmp_path, "run.sh"), runtime_args="-x")
    assert argv(sh, tools) == ["bash", "-x", str(tmp_path / "run.sh")]
    bat = sh.copy(target=script(tmp_path, "start.bat"), args="fast")
    if IS_WINDOWS:
        assert build_launch(bat, tools).argv == [str(tmp_path / "start.bat"), "fast"]
    else:
        with pytest.raises(LaunchError, match=r"only run on Windows"):
            build_launch(bat, tools)

    ps = ServiceConfig(name="ps", runtime=Runtime.POWERSHELL, target=script(tmp_path, "t.ps1"))
    assert argv(ps, tools) == ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(tmp_path / "t.ps1")]


def test_js_runtimes(tmp_path: Path, tools: dict[str, str]) -> None:
    deno = ServiceConfig(name="d", runtime=Runtime.DENO, target=script(tmp_path, "main.ts"))
    assert argv(deno, tools) == ["deno", "run", "-A", str(tmp_path / "main.ts")]
    narrow = deno.copy(runtime_args="--allow-net --allow-env")  # replaces the blanket -A
    assert argv(narrow, tools) == ["deno", "run", "--allow-net", "--allow-env", str(tmp_path / "main.ts")]
    task = deno.copy(mode=LaunchMode.MODULE, target="start", workdir=str(tmp_path))
    with pytest.raises(LaunchError, match=r"deno\.json"):
        build_launch(task, tools)
    (tmp_path / "deno.json").write_text("{}")
    assert argv(task, tools) == ["deno", "task", "start"]

    bun = ServiceConfig(name="b", runtime=Runtime.BUN, target=script(tmp_path, "server.ts"), args="--port 3")
    assert argv(bun, tools) == ["bun", "run", str(tmp_path / "server.ts"), "--port", "3"]
    pkg = bun.copy(mode=LaunchMode.MODULE, target="dev", workdir=str(tmp_path), args="")
    with pytest.raises(LaunchError, match=r"package\.json"):
        build_launch(pkg, tools)
    (tmp_path / "package.json").write_text("{}")
    assert argv(pkg, tools) == ["bun", "run", "dev"]


def test_runtime_options_for_python_and_node(tmp_path: Path, tools: dict[str, str]) -> None:
    py = ServiceConfig(name="p", target=script(tmp_path, "a.py"), python=sys.executable, runtime_args="-X dev")
    assert build_launch(py, tools).argv[1:] == ["-X", "dev", "-u", str(tmp_path / "a.py")]
    node = ServiceConfig(name="n", runtime=Runtime.NODE, target=script(tmp_path, "a.js"),
                         runtime_args="--max-old-space-size=4096")
    assert argv(node, tools) == ["node", "--max-old-space-size=4096", str(tmp_path / "a.js")]


def test_program_runtime(tmp_path: Path) -> None:
    prog = tmp_path / ("server.exe" if IS_WINDOWS else "server")
    prog.write_text("#!/bin/sh\necho hi\n")
    cfg = ServiceConfig(name="x", runtime=Runtime.EXECUTABLE, target=str(prog), args="--port 1")
    if not IS_WINDOWS:
        with pytest.raises(LaunchError, match="chmod"):
            build_launch(cfg)
        prog.chmod(0o755)
    assert build_launch(cfg).argv == [str(prog), "--port", "1"]


def test_unsupported_mode_and_missing_runtime(tmp_path: Path) -> None:
    cfg = ServiceConfig(name="x", runtime=Runtime.SHELL, mode=LaunchMode.MODULE, target="x", workdir=str(tmp_path))
    with pytest.raises(LaunchError, match=r"can't run in module mode"):
        build_launch(cfg)
    php = ServiceConfig(name="p", runtime=Runtime.PHP, target=script(tmp_path, "w.php"))
    with pytest.raises(LaunchError, match=r"PHP was not found.*php\.net"):
        build_launch(php, {"PATH": str(tmp_path / "empty")})
    with pytest.raises(LaunchError, match="PHP not found: /nope/php"):
        build_launch(php.copy(interpreter="/nope/php"), {"PATH": ""})


def test_find_program_looks_in_install_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bun = fake_tool(tmp_path / ".bun" / "bin", "bun")
    found = find_program(runtime_info(Runtime.BUN), "", {"PATH": str(tmp_path / "empty"), "PATHEXT": ".CMD"})
    assert Path(found).parent == bun.parent
    # ... and puts that folder on the service's PATH so `bun` works inside its scripts too.
    target = script(tmp_path, "s.ts")
    spec = build_launch(ServiceConfig(name="b", runtime=Runtime.BUN, target=target),
                        {"PATH": str(tmp_path / "empty"), "PATHEXT": ".CMD"})
    assert str(bun.parent) in spec.env["PATH"]
    explicit = fake_tool(tmp_path / "custom", "deno")
    assert find_program(runtime_info(Runtime.DENO), str(explicit), {"PATH": ""}) == str(explicit)


# -- installs ---------------------------------------------------------------------------------------


def install(cfg: ServiceConfig, env: dict[str, str], tmp_path: Path) -> list[str]:
    spec = build_install(cfg, tmp_path / "req.txt", env)
    return [Path(spec.argv[0]).stem, *spec.argv[1:]]


def test_installs(tmp_path: Path, tools: dict[str, str]) -> None:
    wd = str(tmp_path)
    go = ServiceConfig(name="g", runtime=Runtime.GO, mode=LaunchMode.MODULE, target=".", workdir=wd)
    with pytest.raises(LaunchError, match=r"go\.mod"):
        build_install(go, tmp_path / "r.txt", tools)
    (tmp_path / "go.mod").write_text("module x\n")
    assert install(go, tools, tmp_path) == ["go", "mod", "tidy"]
    assert install(go.copy(requirements="github.com/a/b"), tools, tmp_path) == ["go", "get", "github.com/a/b"]

    rb = ServiceConfig(name="r", runtime=Runtime.RUBY, mode=LaunchMode.MODULE, target="puma", workdir=wd)
    assert install(rb.copy(requirements="discordrb"), tools, tmp_path) == ["gem", "install", "discordrb"]
    (tmp_path / "Gemfile").write_text("")
    assert install(rb, tools, tmp_path) == ["bundle", "install"]
    assert install(rb.copy(requirements="discordrb"), tools, tmp_path) == ["bundle", "add", "discordrb"]

    php = ServiceConfig(name="p", runtime=Runtime.PHP, mode=LaunchMode.MODULE, target="x", workdir=wd)
    assert install(php.copy(requirements="guzzlehttp/guzzle"), tools, tmp_path) == [
        "composer", "require", "guzzlehttp/guzzle"]

    deno = ServiceConfig(name="d", runtime=Runtime.DENO, mode=LaunchMode.MODULE, target="x", workdir=wd)
    assert install(deno.copy(requirements="npm:express"), tools, tmp_path) == ["deno", "add", "npm:express"]

    net = ServiceConfig(name="n", runtime=Runtime.DOTNET, mode=LaunchMode.COMMAND, target="x", workdir=wd)
    assert install(net, tools, tmp_path) == ["dotnet", "restore"]
    chained = build_install(net.copy(requirements="Discord.Net\nSerilog"), tmp_path / "r.txt", tools)
    assert chained.shell and "Discord.Net" in chained.display() and "Serilog" in chained.display()

    java = ServiceConfig(name="j", runtime=Runtime.JAVA, mode=LaunchMode.COMMAND, target="x", workdir=wd)
    with pytest.raises(LaunchError, match="Maven"):
        build_install(java, tmp_path / "r.txt", tools)


# -- real runs --------------------------------------------------------------------------------------

REAL = {
    Runtime.GO: ("main.go", 'package main\n\nimport (\n\t"fmt"\n\t"os"\n\t"time"\n)\n\nfunc main() {\n'
                            '\tfmt.Println("ready go", len(os.Args)-1)\n\ttime.Sleep(60 * time.Second)\n}\n'),
    Runtime.JAVA: ("Bot.java", 'public class Bot { public static void main(String[] a) throws Exception {\n'
                               '  System.out.println("ready java " + a.length); Thread.sleep(60000); } }\n'),
    Runtime.NODE: ("bot.js", 'console.log(`ready node ${process.argv.length - 2}`);\nsetTimeout(() => {}, 60000);\n'),
    Runtime.POWERSHELL: ("task.ps1", 'Write-Output "ready powershell $($args.Count)"\nStart-Sleep -Seconds 60\n'),
    Runtime.RUBY: ("bot.rb", 'puts "ready ruby #{ARGV.size} #{__FILE__ == $0}"\nsleep 60\n'),
    Runtime.PHP: ("w.php", '<?php\necho "ready php ", count($argv) - 1, PHP_EOL;\nsleep(60);\n'),
    Runtime.BUN: ("s.ts", 'console.log(`ready bun ${process.argv.length - 2}`);\nsetTimeout(() => {}, 60000);\n'),
    Runtime.SHELL: ("run.sh", 'echo "ready shell $#"\nsleep 60\n'),
}


def _have(runtime: Runtime) -> bool:
    try:
        find_program(runtime_info(runtime), "", dict(os.environ))
    except LaunchError:
        return False
    return not (runtime is Runtime.SHELL and IS_WINDOWS)


@pytest.mark.parametrize("runtime", list(REAL))
def test_real_runtime_streams_output_and_stops(runtime: Runtime, tmp_path: Path, qapp: object) -> None:
    if not _have(runtime):
        pytest.skip(f"{runtime.label} not installed")
    name, body = REAL[runtime]
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name=runtime.value, runtime=runtime, target=script(tmp_path, name, body), args="a b"))

    def text() -> str:
        return "\n".join(ln.text for ln in svc.log.snapshot())

    assert sup.start(svc.id), text()
    # Output must arrive while the program is still running: no stdout buffering (Ruby, Java, ...).
    expected = f"ready {runtime.value} 2" + (" true" if runtime is Runtime.RUBY else "")
    assert wait_until(lambda: expected in text(), timeout=90, qt=True), text()
    assert svc.status is Status.RUNNING
    assert svc.runner is not None
    tree = _process_tree(svc.runner.pid)  # e.g. `go run` → the compiled program, bun → its worker
    sup.stop(svc.id)
    assert wait_until(lambda: svc.status is Status.STOPPED, timeout=20, qt=True), text()
    assert wait_until(lambda: not _alive(tree), timeout=10), f"left running: {_alive(tree)}"
    sup.shutdown()


def _console_command(argv: list[str], shell: ConsoleShell) -> str:
    """argv as someone would type it in the console's $ mode with ``shell``."""
    if shell is ConsoleShell.CMD:
        return " ".join(f'"{arg}"' if " " in arg or ";" in arg else arg for arg in argv)
    if IS_WINDOWS:  # PowerShell: single quotes keep $ and -Dname.with.dots=value literal
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


@pytest.mark.parametrize("shell", [ConsoleShell.AUTO, ConsoleShell.CMD] if IS_WINDOWS else [ConsoleShell.AUTO])
@pytest.mark.parametrize("runtime", list(REAL))
def test_real_runtime_as_a_shell_command_stops_with_its_tree(runtime: Runtime, shell: ConsoleShell, tmp_path: Path,
                                                             qapp: object) -> None:
    """The same programs started from the console's $ mode (PowerShell or cmd / bash) stop with Stop."""
    if not _have(runtime):
        pytest.skip(f"{runtime.label} not installed")
    name, body = REAL[runtime]
    path = script(tmp_path, name, body)
    sup = Supervisor(DataPaths(tmp_path / "data"))
    sup.config.settings.console_shell = shell
    svc = sup.add(ServiceConfig(name=runtime.value, runtime=runtime, target=path, args="a b"))
    command = _console_command(build_launch(svc.config).argv, shell)

    def text() -> str:
        return "\n".join(ln.text for ln in svc.log.snapshot())

    assert sup.run_shell(svc.id, command), text()
    assert wait_until(lambda: f"ready {runtime.value} 2" in text(), timeout=90, qt=True), text()
    assert svc.task is not None
    tree = _process_tree(svc.task.pid)
    sup.stop(svc.id)
    assert wait_until(lambda: svc.task is None, timeout=20, qt=True), text()
    assert wait_until(lambda: not _alive(tree), timeout=10), f"left running: {_alive(tree)}"
    sup.shutdown()


def _process_tree(pid: int) -> list[psutil.Process]:
    root = psutil.Process(pid)
    return [root, *root.children(recursive=True)]


def _alive(procs: list[psutil.Process]) -> list[str]:
    left = []
    for p in procs:
        try:
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                left.append(f"{p.pid} {p.name()}")
        except psutil.Error:
            pass
    return left


@pytest.mark.skipif(shutil.which("php") is None, reason="php not installed")
def test_real_php_server_url_is_detected(tmp_path: Path, qapp: object) -> None:
    (tmp_path / "index.php").write_text("<?php echo 'hi';")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="web", runtime=Runtime.PHP, mode=LaunchMode.MODULE, target="127.0.0.1:0",
                                workdir=str(tmp_path)))
    assert sup.start(svc.id)
    assert wait_until(lambda: svc.log.url is not None, timeout=20, qt=True), [x.text for x in svc.log.snapshot()]
    assert (svc.log.url or "").startswith("http://127.0.0.1:")
    sup.shutdown()


# -- editor, console and dialog -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("goroutine 1 [running]:\n\t/srv/bot/main.go:42 +0x1d", ("/srv/bot/main.go", 42)),
        ("/srv/app/bot.rb:7:in `block in <main>': boom (RuntimeError)", ("/srv/app/bot.rb", 7)),
        ("PHP Fatal error:  Uncaught Exception: x in /var/www/w.php on line 12", ("/var/www/w.php", 12)),
        ("#0 /var/www/lib.php(33): fail()", ("/var/www/lib.php", 33)),
        ("   at Bot.Program.Main() in C:\\src\\Bot\\Program.cs:line 18", ("C:\\src\\Bot\\Program.cs", 18)),
        ("\tat com.example.Bot.run(Bot.java:25)", ("Bot.java", 25)),
        ("./run.sh: line 3: nope: command not found", ("./run.sh", 3)),
        ("At C:\\jobs\\task.ps1:9 char:1", ("C:\\jobs\\task.ps1", 9)),
        ("plain output 12:30", None),
    ],
)
def test_stack_trace_locations(line: str, found: tuple[str, int] | None) -> None:
    from pythonator.core.locations import find_location

    assert find_location(line.splitlines()[-1]) == found


def test_language_highlighting_and_comments(qapp: object) -> None:
    from pythonator.ui.code_editor import CodeEditor
    from pythonator.ui.highlighter import language_for
    from pythonator.ui.languages import GenericHighlighter

    assert [language_for(p) for p in ("a.go", "a.rb", "a.php", "a.sh", "a.ps1", "a.cs", "a.java", "x.toml")] == [
        "go", "ruby", "php", "shell", "powershell", "csharp", "java", "text"]
    editor = CodeEditor()
    for language, prefix in (("go", "//"), ("ruby", "#"), ("php", "//"), ("powershell", "#"), ("batch", "rem")):
        editor.set_language(language)
        assert editor.comment_prefix == prefix
    editor.set_language("go")
    editor.setPlainText('package main\n\nfunc main() {\n\ts := `multi\nline`\n\t/* block\n\tcomment */ return\n}\n')
    assert isinstance(editor._highlighter, GenericHighlighter)
    doc = editor.document()
    assert doc is not None

    def formats(block_no: int) -> list[tuple[int, int, str]]:
        block = doc.findBlockByNumber(block_no)
        layout = block.layout()
        assert layout is not None
        return [(r.start, r.length, r.format.foreground().color().name()) for r in layout.formats()]

    assert formats(0)[0][:2] == (0, 7)  # "package" is a keyword
    assert formats(4) and formats(4)[0][:2] == (0, 5)  # still inside the raw string: "line`"
    assert formats(6)[0][0] == 0  # still inside the block comment


def test_dialog_switches_runtimes(qapp: object, tmp_path: Path) -> None:
    from pythonator.ui.service_dialog import ServiceDialog, script_runtime

    cfg = ServiceConfig(name="svc", target=script(tmp_path, "main.go"), runtime=Runtime.GO,
                        interpreter="/opt/go/bin/go", python="/usr/bin/python3.12")
    dlg = ServiceDialog(cfg, set())
    assert dlg.interp.text() == "/opt/go/bin/go"
    modes = [dlg.mode.itemData(i) for i in range(dlg.mode.count())]
    assert modes == ["script", "module", "command"]
    dlg.runtime.setCurrentIndex(dlg.runtime.findData("shell"))
    assert [dlg.mode.itemData(i) for i in range(dlg.mode.count())] == ["script", "command"]
    dlg.runtime.setCurrentIndex(dlg.runtime.findData("python"))
    assert dlg.interp.text() == "/usr/bin/python3.12"  # each runtime keeps its own interpreter
    dlg.interp.setText("/usr/bin/python3.13")
    dlg.runtime.setCurrentIndex(dlg.runtime.findData("go"))
    dlg.runtime_args.setText("-race")
    out = dlg.result_config()
    assert (out.runtime, out.interpreter, out.python, out.runtime_args) == (
        Runtime.GO, "/opt/go/bin/go", "/usr/bin/python3.13", "-race")
    assert script_runtime("x.ts", Runtime.DENO) is Runtime.DENO
    assert script_runtime("x.ts", Runtime.GO) is Runtime.NODE
    assert script_runtime("x.rb", Runtime.DENO) is Runtime.RUBY


def test_starter_files_for_each_runtime(tmp_path: Path) -> None:
    import json

    from pythonator.core.starters import project_name, starter_for

    wd = tmp_path / "My Cool_Bot!"
    wd.mkdir()
    assert project_name(wd) == "my-cool_bot"
    assert project_name(tmp_path / "!!!") == "app"

    def make(runtime: Runtime, target: str = "src/bot.js") -> ServiceConfig:
        return ServiceConfig(name="s", runtime=runtime, target=str(wd / target), workdir=str(wd))

    node = starter_for(make(Runtime.NODE), wd)
    assert node is not None and node.name == "package.json"
    assert json.loads(node.content) == {"name": "my-cool_bot", "version": "1.0.0", "private": True,
                                        "scripts": {"start": "node src/bot.js"}, "dependencies": {}}
    bun = starter_for(make(Runtime.BUN, "index.ts"), wd)
    assert bun is not None and json.loads(bun.content)["scripts"] == {"start": "bun run index.ts"}
    npm_script = make(Runtime.NODE).copy(mode=LaunchMode.MODULE, target="start")
    started = starter_for(npm_script, wd)
    assert started is not None and json.loads(started.content)["scripts"] == {}
    deno = starter_for(make(Runtime.DENO, "main.ts"), wd)
    assert deno is not None and json.loads(deno.content) == {"tasks": {"start": "deno run -A main.ts"}, "imports": {}}
    go = starter_for(make(Runtime.GO, "main.go"), wd)
    assert go is not None and go.name == "go.mod" and go.content.startswith("module my-cool_bot\n")
    for runtime, name in ((Runtime.PYTHON, "requirements.txt"), (Runtime.RUBY, "Gemfile"),
                          (Runtime.PHP, "composer.json")):
        found = starter_for(make(runtime, "x"), wd)
        assert found is not None and found.name == name
        assert found.name in runtime_info(runtime).manifests  # the menu then offers "Edit <name>"
    for runtime in (Runtime.JAVA, Runtime.DOTNET, Runtime.SHELL, Runtime.POWERSHELL, Runtime.EXECUTABLE):
        assert starter_for(make(runtime, "x"), wd) is None


def test_go_starter_installs_with_tidy(tmp_path: Path) -> None:
    from pythonator.core.starters import starter_for

    go = shutil.which("go") or "/usr/local/go/bin/go"
    if not Path(go).exists():
        pytest.skip("Go not installed")
    (tmp_path / "main.go").write_text('package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hi") }\n')
    cfg = ServiceConfig(name="g", runtime=Runtime.GO, target=str(tmp_path / "main.go"), workdir=str(tmp_path))
    starter = starter_for(cfg, tmp_path)
    assert starter is not None
    (tmp_path / starter.name).write_text(starter.content)
    done = subprocess.run([go, "mod", "tidy"], check=False, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    run = subprocess.run([go, "run", "."], check=False, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert run.stdout.strip() == "hi", run.stderr
