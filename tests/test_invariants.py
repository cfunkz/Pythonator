"""The 16 invariants (docs/security-cases.md, "Invariants"), one test or more each. Any of these failing fails CI.

Tests that need a real jail are skipped without bubblewrap; everything else runs on Linux, macOS and Windows.
"""

import dataclasses
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pythonator.model.service import ServiceSpec
from pythonator.policy.compile import compile_service
from pythonator.policy.plan import Refused, Status
from pythonator.policy.reference import REFERENCE, for_os
from pythonator.policy.risk import risk_hash, widenings

SRC = Path(__file__).resolve().parent.parent / "pythonator"
needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap") or sys.platform != "linux", reason="needs bubblewrap")
POISON = "poison-7f3a"


def spec(**kw):
    base = {"name": "api", "folder": "/home/me/services/api", "command": ["main.py"]}
    base.update(kw)
    return ServiceSpec.model_validate(base, strict=False)


def every_plan():
    """(os, mode, jail) for every mode on every reference machine that compiles."""
    for target in ("linux", "darwin", "windows"):
        for mode, extra in (("sandbox", {}), ("full", {}), ("wsl", {}),
                            ("container", {"container": {"image": "python:3.13-slim"}})):
            s, ref = for_os(spec(mode=mode, env={"LOG": "1"}, **extra), target, "/home/me")
            if mode == "wsl" and target != "windows":
                continue
            yield target, mode, compile_service(s, ref)


# 1 · service folders are hostile input --------------------------------------------------------------------------
# (link, FIFO, junction, size and race probes: tests/test_safe.py). Here: nothing else reads a service folder.
FOLDER_READERS = ("work/files.py", "policy/fit.py", "reconcile/trycopy.py", "work/git.py")
RAW_READ = re.compile(r"(?<![\w.])open\(|\.read_text\(|\.read_bytes\(|os\.listdir\(|os\.scandir\(|os\.walk\(|\.iterdir\(")


def test_1_only_safefs_reads_service_folders():
    for rel in FOLDER_READERS:
        for n, line in enumerate((SRC / rel).read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if RAW_READ.search(code) and "safefs." not in code and "# ours:" not in line:
                pytest.fail(f"{rel}:{n} reads a service folder without safefs: {line.strip()}")


# 2 · host tools get an allow-listed environment ------------------------------------------------------------------
@pytest.mark.parametrize("name", ["LD_PRELOAD", "GIT_SSH_COMMAND", "DOCKER_HOST", "PYTHONPATH", "AWS_SECRET_ACCESS_KEY",
                                  "NODE_OPTIONS", "SSH_AUTH_SOCK", "WSLENV"])
def test_2_poisoned_environment_never_reaches_a_host_tool_or_jail(monkeypatch, name):
    from pythonator.safe.hostenv import host_env

    monkeypatch.setenv(name, POISON)
    assert POISON not in host_env().values()
    for target, mode, jail in every_plan():
        assert POISON not in jail.env.values(), (target, mode, name)


@pytest.mark.parametrize("name", ["DOCKER_HOST", "DOCKER_CONFIG", "CONTAINER_HOST", "PODMAN_CONNECTION", "XDG_RUNTIME_DIR",
                                  "PYTHONATOR_DATA", "WSLENV", "WSL_DISTRO_NAME", "COMPOSE_FILE", "BUILDX_CONFIG",
                                  "SSH_AUTH_SOCK", "GIT_DIR", "GPG_AGENT_INFO", "_PYI_ARCHIVE", "_MEIPASS2", "LD_AUDIT",
                                  "DYLD_INSERT_LIBRARIES", "PROGRAMFILES", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "PATH",
                                  "TEMP", "GCONV_PATH", "PYTHONINSPECT", "ld_preload"])
def test_2_a_service_can_never_set_a_host_side_variable(name):
    for field in ("env", "secrets"):
        value = {name: "x"} if field == "env" else [name]
        with pytest.raises(ValueError):
            spec(**{field: value})


# 3 · git only in a private git dir in the data folder ------------------------------------------------------------
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_3_snapshots_never_use_the_folders_own_git(tmp_path):
    import asyncio

    from pythonator.work.git import Snapshots

    folder, data = tmp_path / "proj", tmp_path / "data"
    folder.mkdir()
    data.mkdir()
    (folder / "app.py").write_text("print(1)\n")
    planted = tmp_path / "pwned"
    git = folder / ".git"
    (git / "hooks").mkdir(parents=True)
    hook = f"#!/bin/sh\ntouch {planted}\n"
    for h in ("pre-commit", "post-commit", "commit-msg", "reference-transaction"):
        (git / "hooks" / h).write_text(hook)
        (git / "hooks" / h).chmod(0o755)
    (git / "config").write_text(f"[core]\n\tfsmonitor = touch {planted}\n\thooksPath = {git / 'hooks'}\n")
    snaps = Snapshots(data)
    sid = asyncio.run(snaps.snapshot("proj", str(folder), "test"))
    assert sid, "a snapshot was taken"
    assert not planted.exists(), "a hook or fsmonitor from the folder's own .git ran"
    assert snaps.git_dir("proj").is_dir() and str(snaps.git_dir("proj")).startswith(str(data))
    assert sorted(p.name for p in git.iterdir()) == ["config", "hooks"], "the folder's .git was touched"


# 4 · every run lives in a tree handle; 5 · limits are kernel limits or labelled reduced ---------------------------
def test_4_5_every_plan_has_a_tree_and_limits_row():
    for target, mode, jail in every_plan():
        layers = {x.key: x for x in jail.plan.layers}
        for key in ("tree", "limits"):
            assert key in layers, (target, mode, key)
            row = layers[key]
            assert row.status in (Status.ENFORCED, Status.REDUCED), (target, mode, key, row.status)
            assert row.by, (target, mode, key)
            if row.status is Status.REDUCED:
                assert row.detail, f"{target} {mode} {key}: a reduced row must say why"


@needs_bwrap
def test_4_setsid_and_double_fork_children_end_with_stop(daemon_root):
    from pythonator.service.client import Client

    folder = daemon_root.parent / "forker"
    folder.mkdir()
    tag = f"pz-orphan-{os.getpid()}"
    (folder / "app.py").write_text(textwrap.dedent(f"""
        import os, subprocess, sys, time
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(600)", "{tag}"])
            os._exit(0)
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "{tag}"], start_new_session=True)
        print("forked", flush=True)
        time.sleep(600)
    """))
    with Client(daemon_root) as c:
        res = c.call("apply", spec={"name": "forker", "folder": str(folder), "command": ["app.py"]})
        c.call("approve", name="forker", hash=res["hash"])
        c.call("start", name="forker")
        _wait_line(c, "forker", "forked")
        assert _tagged(tag), "the probe didn't start"
        c.call("stop", name="forker")
        for _ in range(100):
            if not _tagged(tag):
                break
            time.sleep(0.1)
    assert not _tagged(tag), "a setsid or double-forked child outlived Stop"


def _tagged(tag):
    import psutil

    found = []
    for p in psutil.process_iter(["cmdline"]):
        if tag in (p.info.get("cmdline") or []):
            found.append(p.pid)
    return found


# 8 · a missing layer is a refusal, never a silent downgrade -------------------------------------------------------
@pytest.mark.parametrize(("os_", "change", "code"), [
    ("linux", {"bwrap": ""}, "RUN-LNX-001"),
    ("linux", {"seccomp": False}, "RUN-LNX-002"),
    ("linux", {"pasta": ""}, "NET-LNX-001"),
    ("darwin", {"sandbox_exec": ""}, "RUN-MAC-001"),
    ("windows", {"appcontainer": False}, "RUN-WIN-002"),
    ("windows", {"wsl_ready": False}, "RUN-WSL-003"),
])
def test_8_missing_layer_refuses(os_, change, code):
    mode = "wsl" if code.startswith("RUN-WSL") else "sandbox"
    traffic = {"network": {"traffic": {"mode": "any"}}} if code == "NET-LNX-001" else {}
    s, ref = for_os(spec(mode=mode, **traffic), os_, "/home/me")
    with pytest.raises(Refused) as e:
        compile_service(s, dataclasses.replace(ref, **change))
    assert e.value.code == code


@pytest.mark.skipif(sys.platform != "linux" or bool(shutil.which("bwrap")), reason="runs on Linux without bubblewrap (CI)")
def test_8_this_machine_without_bubblewrap_refuses_sandbox_and_plugins(tmp_path):
    """The CI job without bubblewrap: the real probe of this machine, never a fallback to a weaker jail."""
    from pythonator.policy.compile import compile_plugin
    from pythonator.policy.host import probe

    host = probe(str(tmp_path / "data"))
    (tmp_path / "home" / "api").mkdir(parents=True)
    host = dataclasses.replace(host, home=str(tmp_path / "home"))
    with pytest.raises(Refused):
        compile_service(spec(folder=str(tmp_path / "home" / "api")), host)
    with pytest.raises(Refused):
        compile_plugin("guard", str(tmp_path / "p"), str(tmp_path / "sdk"), "main.py", host)


# 9 · hostile scripts per mode, with a Full access control run -----------------------------------------------------
HOSTILE = """
import os, socket, sys
secret, port = sys.argv[1], int(sys.argv[2])
def probe(name, fn):
    try:
        fn(); print(name, "OPEN", flush=True)
    except Exception as e:
        print(name, "blocked", type(e).__name__, flush=True)
probe("read-outside", lambda: open(secret).read())
probe("write-outside", lambda: open(os.path.join(os.path.dirname(secret), "dropped"), "w").write("x"))
probe("loopback", lambda: socket.create_connection(("127.0.0.1", port), timeout=2).close())
print("done", flush=True)
"""


@needs_bwrap
@pytest.mark.parametrize("mode", ["sandbox", "full"])
def test_9_hostile_script_per_mode(daemon_root, mode):
    from pythonator.service.client import Client

    outside = daemon_root.parent / f"outside-{mode}"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("s3cret")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    folder = daemon_root.parent / f"hostile-{mode}"
    folder.mkdir()
    (folder / "app.py").write_text(HOSTILE)
    s = {"name": f"hostile-{mode}", "folder": str(folder), "command": ["app.py", str(secret), str(port)], "mode": mode}
    try:
        with Client(daemon_root) as c:
            res = c.call("apply", spec=s)
            c.call("approve", name=s["name"], hash=res["hash"])
            c.call("start", name=s["name"])
            lines = _wait_line(c, s["name"], "done")
    finally:
        listener.close()
    got = {x.split()[0]: x.split()[1] for x in lines if x.split() and x.split()[0] in
           ("read-outside", "write-outside", "loopback")}
    if mode == "sandbox":
        assert got == {"read-outside": "blocked", "write-outside": "blocked", "loopback": "blocked"}, lines
    else:  # the control run: the same attacks really work as you, so the sandbox result above means something
        assert got["read-outside"] == "OPEN" and got["write-outside"] == "OPEN" and got["loopback"] == "OPEN", lines


# 10 · one trusted-executable resolver, never bare names -----------------------------------------------------------
def test_10_every_compiled_argv_starts_with_an_absolute_program():
    for target, mode, jail in every_plan():
        first = jail.argv[0]
        if jail.container_name:  # the engine is resolved from trusted folders by the reconciler at start
            assert first in ("podman", "docker"), (target, mode, first)
            continue
        assert first.startswith("/") or re.match(r"^[A-Z]:\\", first), (target, mode, first)


BARE = re.compile(r"(create_subprocess_exec|Popen|subprocess\.run|check_output|execvpe?|execve)\(\s*\[?\s*\"(?![/\\]|[A-Z]:)")


def test_10_no_bare_program_name_in_the_source():
    for py in SRC.rglob("*.py"):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if BARE.search(line):
                pytest.fail(f"{py.relative_to(SRC)}:{n} starts a program by bare name: {line.strip()}")


# 11 · every configured value masked at one choke point -----------------------------------------------------------
def test_11_secret_values_are_masked_for_every_channel(tmp_path):
    from pythonator.logs.store import LogStore
    from pythonator.safe.mask import Masker, secret_values

    env = {"API_TOKEN": "tok-9f8e7d6c5b", "DATABASE_URL": "postgres://app:hunter22@db/app", "LOG_LEVEL": "debug",
           "REGION": "eu-west-1x9"}
    console = Masker(secret_values(env))
    plugins = Masker(secret_values(env, everything=True))
    line = "token tok-9f8e7d6c5b url postgres://app:hunter22@db/app level debug region eu-west-1x9"
    for m in (console, plugins):
        out = m.mask(line)
        assert "tok-9f8e7d6c5b" not in out and "hunter22" not in out and "debug" in out
    assert "eu-west-1x9" not in plugins.mask(line), "plugins get every non-trivial value masked"
    log = LogStore(tmp_path, "api", console)
    log.append("o", line)
    log.close()
    assert b"tok-9f8e7d6c5b" not in b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


# 12 · System check and the docs come from one EnforcementPlan -------------------------------------------------------
def test_12_docs_protection_tables_match_the_compiler():
    from pythonator.service.check import matrix_markdown

    doc = (SRC.parent / "docs" / "isolation.md").read_text(encoding="utf-8")
    m = re.search(r"<!-- matrix:start -->\n(.*?)<!-- matrix:end -->", doc, re.S)
    assert m, "docs/isolation.md lost its generated matrix block"
    assert m.group(1).strip() == matrix_markdown().strip(), (
        "docs/isolation.md is out of date: python -m pythonator.service.check --doc > the block between the markers"
    )


# 14 · a global excepthook: an exception in a slot never ends the window -------------------------------------------
UI_PROBE = """
import sys
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from pythonator.safe import process
app = QApplication([])
process.install_hook(Path(sys.argv[1]), lambda t: print("reported", t, flush=True))
def boom():
    raise RuntimeError("from a slot")
QTimer.singleShot(0, boom)
QTimer.singleShot(300, lambda: (print("alive", flush=True), app.quit()))
sys.exit(app.exec())
"""


def test_14_exception_in_a_slot_keeps_the_app_alive(tmp_path):
    pytest.importorskip("PyQt6.QtWidgets")
    log = tmp_path / "window.log"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(SRC.parent)}
    out = subprocess.run([sys.executable, "-c", UI_PROBE, str(log)], capture_output=True, text=True, timeout=60,
                         env=env, check=False)
    assert "reported RuntimeError: from a slot" in out.stdout, out.stderr
    assert "alive" in out.stdout and out.returncode == 0, out.stderr
    assert "from a slot" in log.read_text()


# 15 · nothing widens without a plan line and an approval bound to the risk hash ----------------------------------
WIDER = {
    "mode": {"mode": "full"},
    "folders": {"folders": [{"path": "/home/me/data", "write": False}]},
    "folders write": {"folders": [{"path": "/home/me/data", "write": True}]},
    "traffic any": {"network": {"traffic": {"mode": "any"}}},
    "traffic hosts": {"network": {"traffic": {"mode": "hosts", "hosts": ["api.example.com"]}}},
    "lan": {"network": {"traffic": {"mode": "any", "allow_lan": True}}},
    "port": {"network": {"ports": [{"port": 8000}]}},
    "port lan": {"network": {"ports": [{"port": 8000, "reach": "lan"}]}},
    "memory": {"limits": {"memory_mb": 4096}},
    "cpu": {"limits": {"cpu": 4.0}},
    "tasks": {"limits": {"tasks": 4096}},
    "secret": {"secrets": ["API_TOKEN"]},
    "folder": {"folder": "/home/me/services/other"},
    "image": {"mode": "container", "container": {"image": "python:3.13-slim"}},
}


@pytest.mark.parametrize("field", sorted(WIDER))
def test_15_every_widening_changes_the_hash_and_names_itself(field):
    base = spec()
    wide = spec(**WIDER[field])
    assert risk_hash(base) != risk_hash(wide), f"{field}: widening doesn't change the risk hash"
    assert widenings(base, wide), f"{field}: no plan line names the widening"


def test_15_changes_inside_the_same_jail_never_need_a_new_approval():
    """The approval is about access. What runs inside the same walls (command, variables) is shown as a change,
    but asks for nothing new."""
    base = spec()
    for change in ({"desired": "running"}, {"group": "web"}, {"generation": 7}, {"command": ["other.py"]},
                   {"env": {"LOG": "debug"}}):
        assert risk_hash(base) == risk_hash(spec(**change)), change


# 16 · the ledger: every side effect is recorded first, and start-up finishes or undoes it --------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_16_leftover_process_from_a_crash_is_ended_and_the_row_closed(tmp_path):
    import asyncio

    from pythonator.ledger.ledger import Ledger
    from pythonator.reconcile import proc
    from pythonator.reconcile.reconciler import Reconciler

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    try:
        ledger = Ledger(root / "ledger.jsonl")
        ledger.done(ledger.intend("process", "api", "pending"), f"{child.pid}:{proc.start_time(child.pid)}")
        ledger.intend("process", "api", "pending")  # torn: written before a start that never happened
        rec = Reconciler(root, REFERENCE["linux"])
        asyncio.run(rec.recover())
        assert child.wait(5) is not None, "the leftover process is still running"
        assert Ledger(root / "ledger.jsonl").live() == [], "a ledger row stayed open"
    finally:
        if child.poll() is None:
            child.kill()


def test_16_a_reused_pid_is_never_killed(tmp_path):
    import asyncio

    from pythonator.ledger.ledger import Ledger
    from pythonator.reconcile.reconciler import Reconciler

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    ledger = Ledger(root / "ledger.jsonl")
    ledger.done(ledger.intend("process", "api", "pending"), f"{os.getpid()}:12345.0")  # our pid, another start time
    asyncio.run(Reconciler(root, REFERENCE["linux"]).recover())
    assert Ledger(root / "ledger.jsonl").live() == []  # and we're still here


def test_16_plugin_rows_and_bad_rows_never_stop_the_background_service(tmp_path):
    """A plugin's folder grants and processes are ledger rows too (`plugin:<id>`), which isn't a service name. The
    0.0.1 build that crashed at start on Windows read every row as a service; recovery must start whatever is in it."""
    import asyncio

    from pythonator.ledger.ledger import Ledger
    from pythonator.reconcile.reconciler import Reconciler

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    ledger = Ledger(root / "ledger.jsonl")
    ledger.done(ledger.intend("acl", "plugin:guard", "pythonator.plugin.guard|C:/Users/x/AppData/Local/Pythonator/plugins/guard"))
    ledger.done(ledger.intend("process", "plugin:guard", "pending"), "999999:1.0")
    ledger.done(ledger.intend("acl", "../../etc", "box|/etc"))
    ledger.done(ledger.intend("acl", "gone", "pythonator.svc.gone|/srv/gone"))
    rec = Reconciler(root, REFERENCE["windows"])
    asyncio.run(rec.recover())  # must not raise
    live = {(e.kind, e.service) for e in Ledger(root / "ledger.jsonl").live()}
    assert ("acl", "plugin:guard") in live, "a plugin's grant is the plugin host's to take back, not recovery's"
    assert ("process", "plugin:guard") not in live


# fixtures and helpers ---------------------------------------------------------------------------------------------
@pytest.fixture
def daemon_root(tmp_path):
    import asyncio
    import threading

    from pythonator.service import ipc
    from pythonator.service.daemon import Daemon

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    d = Daemon(root)
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=lambda: loop.run_until_complete(d.serve()), daemon=True)
    th.start()
    for _ in range(200):
        if ipc.endpoint_exists(root):
            break
        time.sleep(0.05)
    yield root
    loop.call_soon_threadsafe(d._stop.set)
    th.join(15)


def _wait_line(c, name, word, timeout=20.0):
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        lines = [x[3] for x in c.call("logs", name=name, count=200)["lines"]]
        if any(x.startswith(word) for x in lines):
            return lines
        time.sleep(0.1)
    pytest.fail(f"{name}: no '{word}' line in time: {lines[-10:]}")
