"""0.0.18, the review fixes: Make reachable never starts a planted PowerShell, a git pull can't be raced by another
sandboxed service in the folder, Landlock alone is never taken for a sandbox, every env value is masked in what
plugins get, a cron line can't run every second, project templates start from patched versions and fail loudly,
and the lock files have something that refreshes them."""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pythonator.core import git, schedule
from pythonator.core.logs import MASK, Masker, plugin_secrets
from pythonator.core.models import Service
from pythonator.core.store import Store
from pythonator.daemon.supervisor import Slot, State, Supervisor
from pythonator.isolation import base, linux
from pythonator.paths import DataDirs

ROOT = Path(__file__).resolve().parent.parent
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")


# -- S1: Windows, elevated PowerShell by its full path -----------------------------------------------------------

_SPAWN = {"run", "Popen", "call", "check_call", "check_output"}


def _bare(node: ast.expr) -> str | None:
    """The program name when ``node`` is a string constant with no folder in it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and not re.search(r"[\\/]", node.value):
        return node.value
    return None


def test_no_program_is_started_by_a_bare_name() -> None:
    """Windows looks for a bare program name in the current folder first, which can be a project a sandboxed service
    writes. Every program Pythonator starts is named by its full path (or found with which(), which skips it)."""
    found: list[str] = []
    for path in sorted((ROOT / "pythonator").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                name = func.id if isinstance(func, ast.Name) and func.id == "Popen" else \
                    func.attr if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id == "subprocess" else ""
                first = node.args[0]
                if name in _SPAWN and isinstance(first, ast.List) and first.elts and (bare := _bare(first.elts[0])):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}([{bare!r}, ...])")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "lpFile" and (bare := _bare(node.value)):
                        found.append(f"{path.relative_to(ROOT)}:{node.lineno} lpFile = {bare!r}")
    assert not found, found


def test_powershell_is_windows_own() -> None:
    from pythonator.isolation import windows

    ps = windows.powershell()
    assert ps.parts[-4:] == ("System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    assert ps.is_absolute() or sys.platform != "win32"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' System32")
def test_powershell_is_found_through_windows_on_windows() -> None:
    from pythonator.isolation import windows

    system = windows.system32()
    assert system.is_dir() and (system / "kernel32.dll").is_file()
    assert os.path.normcase(str(system)) == os.path.normcase(str(Path(os.environ["SYSTEMROOT"]) / "System32"))
    assert windows.powershell().is_file()


def test_the_window_and_the_service_leave_the_folder_they_were_started_in(
        dirs: DataDirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.paths import leave_current_folder

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    leave_current_folder(dirs)
    assert Path.cwd().resolve() == dirs.root.resolve()
    assert os.environ["NoDefaultCurrentDirectoryInExePath"] == "1"  # noqa: SIM112 - the name Windows reads
    app = (ROOT / "pythonator" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "leave_current_folder(dirs)" in app  # the window does it too, not only the background service


# -- S2: git pull race ------------------------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/x.git"], check=True)
    return repo


@needs_git
def test_git_pins_sign_in_programs_to_your_own_config(dirs: DataDirs, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout's config changed after the check (the race) still can't name a program git runs as you."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    repo = _repo(tmp_path)
    git.check_checkout(repo)
    marker = tmp_path / "ran"
    # What a sandboxed service in the same folder writes between the check and the pull.
    subprocess.run(["git", "-C", str(repo), "config", "core.sshCommand", "planted-ssh"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "credential.helper", f"!echo x > '{marker.as_posix()}'"],
                   check=True)

    assert git.run(["config", "core.sshCommand"], repo).strip() == "ssh"

    def fill(flags: list[str]) -> None:
        subprocess.run([git.git_path(), *git.HARDENING, *flags, "credential", "fill"], cwd=repo, env=git._env(),
                       input="protocol=https\nhost=example.com\n\n", capture_output=True, text=True, timeout=60,
                       check=False)

    fill(git._your_settings(git.git_path(), git._env(), repo))
    assert not marker.exists()
    fill([])  # the same without the pins: the planted helper runs, so the check above means something
    assert marker.exists()

    # Your own settings still count: a global helper and SSH command are passed through.
    gitconfig.write_text('[credential]\n\thelper = store\n[core]\n\tsshCommand = ssh -i ~/.ssh/work\n',
                         encoding="utf-8")
    flags = git._your_settings(git.git_path(), git._env(), repo)
    assert flags[:2] == ["-c", "credential.helper="]
    assert "credential.helper=store" in flags and "core.sshCommand=ssh -i ~/.ssh/work" in flags
    assert git.run(["config", "core.sshCommand"], repo).strip() == "ssh -i ~/.ssh/work"


def test_git_ssh_becomes_a_command_no_config_can_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_SSH", "C:/Program Files/PuTTY/plink.exe")
    assert git._env()["GIT_SSH_COMMAND"] == "'C:/Program Files/PuTTY/plink.exe'"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -v")
    assert git._env()["GIT_SSH_COMMAND"] == "ssh -v"


def test_a_pull_checks_the_config_again_before_the_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    steps: list[str] = []
    monkeypatch.setattr(git, "check_checkout", lambda folder: steps.append("check"))
    def run(args: list[str], *rest: Any, **named: Any) -> str:
        steps.append(args[0])
        return "abc"

    monkeypatch.setattr(git, "run", run)
    git.pull(tmp_path)
    assert steps == ["check", "rev-parse", "fetch", "check", "merge", "rev-parse"]


def _service(name: str, folder: Path, mode: str = "sandbox", **more: Any) -> Service:
    return Service.model_validate({"name": name, "folder": str(folder), "runtime": "python", "entry": "app.py",
                                   "mode": mode, **more})


@pytest.fixture
def sup(dirs: DataDirs) -> Any:
    supervisor = Supervisor(dirs, Store(dirs))
    yield supervisor
    supervisor.close()


def _checkout(tmp_path: Path) -> Path:
    folder = tmp_path / "app"
    (folder / ".git").mkdir(parents=True)
    (folder / "worker").mkdir()
    return folder


class _Running:
    """Stands in for a Runner: something of that service is running."""

    pid = 0
    created = 1.0
    seen: tuple[Any, ...] = ()
    limit_hit = ""

    def kill(self) -> None:
        pass

    def stop(self, grace: float) -> None:
        pass


def _slot(sup: Supervisor, service: Service) -> Slot:
    sup.add(service)
    return sup.slots[service.id]


def test_a_pull_waits_for_every_sandboxed_service_in_the_folder(sup: Supervisor, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _checkout(tmp_path)
    pulled: list[str] = []
    monkeypatch.setattr(sup, "_pull", lambda slot, then: pulled.append(slot.service.name))
    web = _slot(sup, _service("web", folder))
    worker = _slot(sup, _service("worker", folder / "worker"))  # inside the checkout: it can write it too
    elsewhere = _slot(sup, _service("elsewhere", tmp_path / "other"))
    worker.runner = elsewhere.runner = _Running()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="Stop worker first"):
        sup.pull(web.service.id)
    worker.runner = None
    worker.shell = _Running()  # type: ignore[assignment]  # a console command counts too
    with pytest.raises(ValueError, match="Stop worker first"):
        sup.pull(web.service.id)
    worker.shell = None
    sup.pull(web.service.id)  # another folder's service doesn't matter
    assert pulled == ["web"]
    full = _slot(sup, _service("full", folder, mode="full", full_access_ok=True))
    full.runner = _Running()  # type: ignore[assignment]
    sup.pull(web.service.id)  # Full access runs as you anyway
    assert pulled == ["web", "web"]


def test_pull_on_start_is_skipped_while_another_sandboxed_service_runs_there(
        sup: Supervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _checkout(tmp_path)
    pulled: list[str] = []
    started: list[str] = []
    monkeypatch.setattr(sup, "_pull", lambda slot, then: pulled.append(slot.service.name))
    monkeypatch.setattr(sup, "_prepare_env", lambda slot: started.append(slot.service.name))
    web = _slot(sup, _service("web", folder, git={"url": "https://example.com/x.git", "pull_on_start": True}))
    worker = _slot(sup, _service("worker", folder))
    worker.runner = _Running()  # type: ignore[assignment]
    sup.start(web.service.id)
    assert pulled == [] and started == ["web"]
    assert "Didn't git pull: worker is sandboxed in this folder" in web.log.tail()
    worker.runner = None
    web.state = State.STOPPED
    sup.start(web.service.id)
    assert pulled == ["web"]


def test_nothing_sandboxed_starts_in_a_folder_git_is_pulling(sup: Supervisor, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _checkout(tmp_path)
    started: list[str] = []
    monkeypatch.setattr(sup, "_prepare_env", lambda slot: started.append(slot.service.name))
    web = _slot(sup, _service("web", folder))
    worker = _slot(sup, _service("worker", folder))
    full = _slot(sup, _service("full", folder, mode="full", full_access_ok=True))
    web.pulling = True
    sup.start(worker.service.id)
    sup.start(full.service.id)
    assert started == ["full"]
    assert "Waiting for git to finish pulling web's folder" in worker.log.tail()
    with pytest.raises(ValueError, match="Wait for git to finish pulling web's folder"):
        sup.shell(worker.service.id, "echo hi")
    with pytest.raises(ValueError, match="Wait for git"):
        sup.install(worker.service.id)
    sup.tick()
    assert started == ["full"]  # still pulling
    web.pulling = False
    sup.tick()
    assert started == ["full", "worker"]


# -- S3: Linux, Landlock alone isn't a sandbox --------------------------------------------------------------------


def test_landlock_without_seccomp_or_bubblewrap_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Landlock (before ABI 9) doesn't stop connect() to a Unix socket by path: the session bus, docker.sock."""
    alone = linux.Support(landlock=7, seccomp="", bwrap="")
    monkeypatch.setattr(linux, "support", lambda: alone)
    with pytest.raises(base.IsolationError, match="libseccomp2 or bubblewrap"):
        linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
    assert dict(alone.capabilities())["Other processes"] == "not enforced"
    for usable in (linux.Support(7, "libseccomp.so.2", ""), linux.Support(7, "", "/usr/bin/bwrap")):
        monkeypatch.setattr(linux, "support", lambda usable=usable: usable)
        linux.wrap(["/bin/true"], tmp_path, {}, base.Policy(network=True), tmp_path / "h", tmp_path / "t")
        assert dict(usable.capabilities())["Other processes"] == "enforced"


# -- S4: every env value masked in what plugins get ---------------------------------------------------------------


def test_every_env_value_is_hidden_from_plugins_whatever_its_name() -> None:
    env = {"DB_PASS": "hunter2hunter2", "DATABASE_URL": "postgres://app:S3cretPw@db/app", "DISCORD": "MTA4.abc.xyz",
           "BOT_TOKEN": "t0k3n-t0k3n", "PORT": "8080", "NODE_ENV": "production", "DEBUG": "true",
           "HOST": "127.0.0.1", "LIMIT": "100000", "SHORT": "abc"}
    secrets = plugin_secrets(env, (8080,))
    for value in ("hunter2hunter2", "postgres://app:S3cretPw@db/app", "S3cretPw", "MTA4.abc.xyz", "t0k3n-t0k3n"):
        assert value in secrets
    for value in ("8080", "production", "true", "127.0.0.1", "100000", "abc"):
        assert value not in secrets
    mask = Masker(secrets)
    line = "connecting to postgres://app:S3cretPw@db/app as DB_PASS=hunter2hunter2; retry with S3cretPw on 8080"
    assert mask(line) == f"connecting to {MASK} as DB_PASS={MASK}; retry with {MASK} on 8080"


def test_plugins_get_output_and_notes_masked(sup: Supervisor, tmp_path: Path) -> None:
    from pythonator.plugins.host import PluginHost

    project = tmp_path / "bot"
    project.mkdir()
    (project / ".env").write_text("DATABASE_URL=postgres://app:S3cretPw@db/app\n", encoding="utf-8")
    slot = _slot(sup, _service("bot", project, env={"DISCORD": "MTA4.abc.xyz"}, env_file=".env"))
    sid = slot.service.id
    sent: list[dict[str, Any]] = []
    plugin = SimpleNamespace(id="p")
    stub = SimpleNamespace(sup=sup, _last_tick=1e18, _live=lambda: [plugin], granted=lambda p: {"read_logs"},
                           _send=lambda p, msg: sent.append(msg), _view=lambda p, s: {"id": s})
    stub._masked = lambda s, text: PluginHost._masked(stub, s, text)  # type: ignore[arg-type]
    PluginHost._on_supervisor_event(stub, {"ev": "output", "id": sid,  # type: ignore[arg-type]
                                           "text": "token MTA4.abc.xyz\nwrong password S3cretPw\n"})
    PluginHost._on_supervisor_event(stub, {"ev": "state", "id": sid, "kind": "crashed",  # type: ignore[arg-type]
                                           "note": "Exited: bad token MTA4.abc.xyz"})
    assert sent[0]["lines"] == [f"token {MASK}", f"wrong password {MASK}"]
    assert sent[1]["note"] == f"Exited: bad token {MASK}"
    assert "MTA4.abc.xyz" not in str(sent) and "S3cretPw" not in str(sent)


# -- cron: five fields only ----------------------------------------------------------------------------------------


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


# -- templates ------------------------------------------------------------------------------------------------------


def _version(spec: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", spec))


def test_templates_start_from_versions_without_known_advisories() -> None:
    from pythonator.core import template_files, templates

    floors = {"fastify": (5, 12, 8), "hono": (4, 13, 16), "@hono/node-server": (2, 1, 6), "elysia": (1, 4, 31)}
    seen: set[str] = set()
    for template in templates.TEMPLATES.values():
        for package, spec in template.dependencies.items():
            if package in floors:
                seen.add(package)
                assert _version(spec) >= floors[package], (template.key, package, spec)
    assert seen == set(floors)
    assert 'gem "sinatra", "~> 4.2"' in template_files.SINATRA_GEMFILE
    assert 'gem "puma", "~> 8.0", ">= 8.0.2"' in template_files.SINATRA_GEMFILE


def test_express_apps_exit_with_an_error_when_the_port_is_taken() -> None:
    """Express 5 passes a failed listen to the callback: printing "Listening" there and exiting 0 hid it."""
    from pythonator.core import template_files

    for text in (template_files.EXPRESS_JS, template_files.WHATSAPP_JS):
        assert "app.listen(" in text
        callback = text[text.index("app.listen("):]
        assert re.search(r"\(err\) => \{\s*if \(err\) throw err;", callback), callback


# -- dependencies and CI -----------------------------------------------------------------------------------------


def _locks() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.lock") if ".git" not in p.parts and "node_modules" not in p.parts
                  and p.name.startswith("requirements"))


def test_every_lock_says_how_it_was_made_and_the_refresh_workflow_knows_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "refresh-locks.yml").read_text(encoding="utf-8")
    locks = _locks()
    assert len(locks) >= 4
    for lock in locks:
        rel = lock.relative_to(ROOT).as_posix()
        header = lock.read_text(encoding="utf-8").splitlines()[1].removeprefix("#    ")
        assert header.startswith("uv pip compile ") and header.endswith(f" -o {rel}"), header
        assert "--generate-hashes" in header and "--universal" in header
        assert rel in workflow  # refreshed every week
        assert rel in (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")


def test_the_locked_versions_are_inside_the_declared_ranges() -> None:
    from packaging.requirements import Requirement

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    locked = dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", (ROOT / "requirements.lock").read_text(encoding="utf-8"),
                             re.MULTILINE))
    locked = {k.lower().replace("_", "-"): v for k, v in locked.items()}
    for line in project["project"]["dependencies"]:
        req = Requirement(line)
        assert req.specifier.contains(locked[req.name.lower()]), (line, locked[req.name.lower()])
    psutil = next(Requirement(x) for x in project["project"]["dependencies"] if x.startswith("psutil"))
    assert not psutil.specifier.contains("8.0.0")  # 8 changes Process.info and memory_info


def test_workflows_name_their_ubuntu_and_use_current_actions() -> None:
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "ubuntu-latest" not in text, path.name
        assert "attest-build-provenance@" not in text, path.name
        for sha, tag in re.findall(r"uses: actions/download-artifact@([0-9a-f]{40})\s+#\s*(\S+)", text):
            assert tag.startswith("v8"), (path.name, sha, tag)


def test_env_files_are_never_committed() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "!.env.example"} <= set(lines)


# -- Windows CI: plugin pipes read in blocks, threads outliving the window ---------------------------------------


def test_a_plugin_message_is_read_in_blocks_not_byte_by_byte() -> None:
    """The pipe is unbuffered: readline() on it made one system call per byte, which on Windows took longer than
    10 seconds for a 1 MB message. The oversized message is now noticed at once, and the plugin killed by the
    reading thread itself: it is still writing the rest, which nobody reads, so waiting for it would never end."""
    import queue
    import threading
    import time

    from pythonator.plugins.host import MAX_LINE, PluginHost

    posted: list[Any] = []
    stub = SimpleNamespace(sup=SimpleNamespace(post=posted.append))
    # Far more than any pipe buffer holds after the first megabyte: the writer blocks until it is killed.
    code = f"import os; os.write(1, b'a' * {MAX_LINE * 3} + b'\\n')"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, bufsize=0)
    reader = threading.Thread(target=PluginHost._read, args=(stub, SimpleNamespace(), proc, queue.Queue()),
                              daemon=True)
    started = time.perf_counter()
    reader.start()
    reader.join(15)
    took = time.perf_counter() - started
    if reader.is_alive():  # stuck: end it, so the test fails instead of hanging
        proc.kill()
        reader.join(15)
    proc.wait()
    assert took < 10, f"the reading thread was stuck for {took:.0f} s"
    assert len(posted) == 2  # the kill for the oversized message, then the exit
    assert proc.returncode != 0  # killed by the reader, not finished


def test_a_worker_thread_can_report_after_the_window_closed(qtbot: Any) -> None:
    from PyQt6 import sip

    from pythonator.ui.service_dialog import _Bridge

    bridge = _Bridge()
    got: list[str] = []
    bridge.text.connect(got.append)
    bridge.send("one")
    sip.delete(bridge)  # the window, its parent, closed while the thread still ran
    bridge.send("two")  # RuntimeError before: "wrapped C/C++ object of type _Bridge has been deleted"
    assert got == ["one"]
    source = (ROOT / "pythonator" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "bridge.text.emit(" not in source
