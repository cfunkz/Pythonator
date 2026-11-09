"""Class-level tests for findings closed while answering the 286 security cases (docs/security-cases.md)."""

import dataclasses
import os
import socket
import sys
from pathlib import Path

import pytest

from pythonator.model.service import ServiceSpec
from pythonator.policy.compile import compile_service
from pythonator.policy.paths import check_folder
from pythonator.policy.plan import Refused
from pythonator.policy.reference import REFERENCE, for_os


def spec(**kw):
    base = {"name": "api", "folder": "/home/me/services/api", "command": ["main.py"]}
    base.update(kw)
    return ServiceSpec.model_validate(base, strict=False)


@pytest.mark.parametrize("image", ["--privileged", "-v/:/host", "python:3.13 --rm", "app/UPPER", "a b", "python:3.13\n",
                                   "x@sha256:short", ""])
def test_option_like_or_malformed_images_are_refused(image):
    with pytest.raises(ValueError):
        spec(mode="container", container={"image": image})


@pytest.mark.parametrize("image", ["python:3.13-slim", "redis:7-alpine@sha256:" + "a" * 64, "ghcr.io/org/app:1.2",
                                   "localhost:5000/team/api", "docker.io/library/node"])
def test_real_image_references_pass(image):
    assert spec(mode="container", container={"image": image}).container.image == image


@pytest.mark.parametrize("folder", ["/home/me/a,b", "/home/me/a:b", "/home/me/it's", '/home/me/"x"'])
def test_container_mount_options_cant_be_smuggled_in_a_folder_name(folder):
    with pytest.raises(Refused) as e:
        compile_service(spec(folder=folder, mode="container", container={"image": "python:3.13-slim"}), REFERENCE["linux"])
    assert e.value.code == "RUN-CTR-002"


def test_rootful_docker_runs_as_you_not_root():
    host = dataclasses.replace(REFERENCE["linux"], podman="", docker="/usr/bin/docker", gid=1000)
    argv = compile_service(spec(mode="container", container={"image": "python:3.13-slim"}), host).argv
    assert argv[argv.index("--user") + 1] == "1000:1000"


@pytest.mark.parametrize("bad", ["a\u202eb", "zero\u200bwidth", "esc\x1b[2J", "bell\x07"])
def test_control_and_invisible_characters_are_refused(bad):
    with pytest.raises(ValueError):
        spec(command=["main.py", bad])
    with pytest.raises(ValueError):
        spec(env={"NOTE": bad})


def test_values_may_hold_line_breaks():
    assert spec(env={"PEM": "-----BEGIN-----\nabc\n-----END-----"}).env["PEM"].count("\n") == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_link_to_a_secret_folder_is_the_secret_folder(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    os.symlink(home / ".ssh", home / "proj")
    here = "darwin" if sys.platform == "darwin" else "linux"  # links are followed only for this machine's OS
    host = dataclasses.replace(REFERENCE[here], home=str(home), data_dir=str(tmp_path / "data"))
    with pytest.raises(Refused) as e:
        compile_service(spec(folder=str(home / "proj")), host)
    assert e.value.code == "POL-PATH-006"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_home_reached_through_a_link_still_hides_its_secrets(tmp_path):
    (tmp_path / "data" / "me" / ".ssh").mkdir(parents=True)
    os.symlink(tmp_path / "data", tmp_path / "home")  # /home -> /data/home on some machines
    home = tmp_path / "home" / "me"
    os.symlink(home / ".ssh", home / "proj")
    here = "darwin" if sys.platform == "darwin" else "linux"
    host = dataclasses.replace(REFERENCE[here], home=str(home), data_dir=str(tmp_path / "pdata"))
    with pytest.raises(Refused) as e:
        compile_service(spec(folder=str(home / "proj")), host)
    assert e.value.code == "POL-PATH-006"


@pytest.mark.parametrize(("folder", "code"), [
    ("/home/me/.npmrc", "POL-PATH-006"),
    ("/home/me/.config/containers", "POL-PATH-006"),
    ("/home/me/.cargo/credentials.toml", "POL-PATH-006"),
    ("/home/me/.local/share/applications", "POL-PATH-008"),
    ("/home/me/Documents", "POL-PATH-008"),  # holds the PowerShell profile folders
    ("/home/me/.cargo/bin", "POL-PATH-008"),
    ("/opt/tools", "POL-PATH-004"),
    ("/srv/www", "POL-PATH-004"),
    ("/tmp", "POL-PATH-004"),  # noqa: S108
    ("//wsl.localhost/Ubuntu/mnt/c/Users/me", "POL-PATH-010"),
])
def test_new_deny_list_entries(folder, code):
    v = check_folder(folder, "/home/me", data_dir="/home/me/.local/share/Pythonator")
    assert not v.ok and v.code == code, v


def test_a_project_inside_tmp_is_fine():
    assert check_folder("/tmp/work/api", "/home/me").ok  # noqa: S108


def test_pythonators_own_program_folder_is_refused():
    v = check_folder("/home/me/apps/pythonator/pythonator", "/home/me", program_dirs=("/home/me/apps/pythonator/pythonator",))
    assert not v.ok and v.code == "POL-PATH-011"
    v = check_folder("/home/me/apps", "/home/me", program_dirs=("/home/me/apps/pythonator/pythonator",))
    assert not v.ok, "a writable folder around the program folder is refused too"


@pytest.mark.parametrize(("exe", "root"), [
    ("/home/me/.cargo/bin/cargo", "/home/me/.cargo/bin"),
    ("/home/me/.local/bin/python3", "/home/me/.local/bin"),
    ("/usr/local/go/bin/go", "/usr/local/go"),
    ("C:\\Program Files\\nodejs\\node.exe", "C:\\Program Files\\nodejs"),
    ("C:\\Users\\me\\.bun\\bin\\bun.exe", "C:\\Users\\me\\.bun\\bin"),
])
def test_runtime_grants_never_cover_a_dot_folder_parent(exe, root):
    from pythonator.policy.runtimes import install_root

    assert install_root(exe) == root


def test_windows_sandbox_refuses_ports_instead_of_promising_them():
    s, ref = for_os(spec(network={"traffic": {"mode": "any"}, "ports": [{"port": 8000}]}), "windows", "/home/me")
    with pytest.raises(Refused) as e:
        compile_service(s, ref)
    assert e.value.code == "NET-WIN-002"


@pytest.mark.parametrize("target", ["linux", "darwin", "windows"])
def test_full_access_refuses_an_allow_list_it_cant_enforce(target):
    s, ref = for_os(spec(mode="full", network={"traffic": {"mode": "hosts", "hosts": ["api.example.com"]}}), target, "/home/me")
    with pytest.raises(Refused) as e:
        compile_service(s, ref)
    assert e.value.code == "NET-FULL-001"


@pytest.mark.parametrize("target", ["linux", "darwin", "windows"])
def test_plugins_never_load_bytecode_shipped_in_the_plugin(target):
    from pythonator.policy.compile import compile_plugin

    ref = dataclasses.replace(REFERENCE[target], bwrap="/usr/bin/bwrap") if target == "linux" else REFERENCE[target]
    folder = "C:\\p\\guard" if target == "windows" else "/p/guard"
    jail = compile_plugin("guard", folder, folder + "-sdk", "main.py", ref)
    text = " ".join(jail.argv)
    assert "pycache_prefix=" in text


def test_busy_port_is_named_before_the_start():
    from pythonator.reconcile.reconciler import _busy_port, _free_near

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    if port < 1024:
        pytest.skip("the OS gave a low port")
    try:
        assert _busy_port(spec(network={"ports": [{"port": port}]})) == port
        assert _free_near(port) > port
    finally:
        s.close()


def test_giving_up_is_followed_by_one_more_try_after_a_cool_down(tmp_path):
    import time

    from pythonator.model.state import Observed, Phase
    from pythonator.reconcile.reconciler import COOL_DOWN_S, Reconciler

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    rec = Reconciler(root, REFERENCE["linux"])
    s = spec(desired="running", restart={"policy": "always", "give_up_after": 1})
    ob = Observed(name="api", restarts=1)
    rec._after_exit(s, ob, 1, 0.5)
    assert ob.phase is Phase.FAILED and ob.next_retry_at
    assert abs(ob.next_retry_at - (time.time() + COOL_DOWN_S)) < 5
    assert "trying again at" in ob.message


def test_secret_looking_variables_are_masked_in_the_console_too():
    from pythonator.safe.mask import Masker, secret_values

    m = Masker(secret_values({"DISCORD_TOKEN": "abc.def.ghi-123", "STRIPE_API_KEY": "sk_live_123456", "PORT": "8000"}))
    out = m.mask("abc.def.ghi-123 sk_live_123456 8000")
    assert "abc.def" not in out and "sk_live" not in out and "8000" in out


@pytest.mark.parametrize("field", ["name", "env", "secrets", "hosts"])
def test_a_trailing_line_break_never_slips_past_a_pattern(field):
    """Python's `$` matches before a final newline; every pattern is a full match."""
    bad = {"name": {"name": "api\n"}, "env": {"env": {"LOG\n": "1"}}, "secrets": {"secrets": ["TOKEN\n"]},
           "hosts": {"network": {"traffic": {"mode": "hosts", "hosts": ["example.com\n"]}}}}[field]
    with pytest.raises(ValueError):
        spec(**bad)


def test_well_known_token_shapes_and_private_keys_are_masked_without_being_declared():
    from pythonator.safe.mask import MASK, Masker

    m = Masker()
    line = "gh ghp_" + "a" * 36 + " aws AKIAABCDEFGHIJKLMNOP slack xoxb-123456789012-abc key sk-ant-api03-" + "x" * 30
    assert m.mask(line).count(MASK) == 4 and "gh " in m.mask(line)
    assert m.mask("-----BEGIN OPENSSH PRIVATE KEY-----") == "-----BEGIN OPENSSH PRIVATE KEY-----"
    assert m.mask("b3BlbnNzaC1rZXktdjEAAAAA") == MASK, "the body is masked on the following lines too"
    assert m.mask("-----END OPENSSH PRIVATE KEY-----") == "-----END OPENSSH PRIVATE KEY-----"
    assert m.mask("listening on 8000") == "listening on 8000"


def test_port_and_host_follow_the_approved_reach():
    from pythonator.policy.compile import base_env

    here = spec(mode="full", network={"ports": [{"port": 8000}]})
    lan = spec(mode="full", network={"ports": [{"port": 8000, "reach": "lan"}]})
    box = spec(mode="container", container={"image": "python:3.13-slim"}, network={"ports": [{"port": 8000}]})
    assert base_env(here, home="/h", host=REFERENCE["linux"])["HOST"] == "127.0.0.1"
    assert base_env(lan, home="/h", host=REFERENCE["linux"])["HOST"] == "0.0.0.0"  # noqa: S104
    assert base_env(box, home="/h", host=REFERENCE["linux"])["HOST"] == "0.0.0.0"  # noqa: S104 (the jail's own address)
    assert base_env(box, home="/h", host=REFERENCE["linux"])["PORT"] == "8000"
    assert "PORT" not in base_env(spec(), home="/h", host=REFERENCE["linux"])


def test_official_plugin_hashes_are_pinned_by_the_release():
    """resources/plugins/official.json pins each bundled plugin; UPDATE_GOLDEN=1 rewrites it after a deliberate edit."""
    import json

    from pythonator.plugins.host import BUNDLED
    from pythonator.plugins.model import content_hash, load_manifest

    now = {load_manifest(d).id: content_hash(d) for d in sorted(BUNDLED.iterdir()) if (d / "plugin.toml").is_file()}
    path = BUNDLED / "official.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = (".py", ".toml", ".json", ".md", ".txt")
    crlf = [f.name for f in BUNDLED.rglob("*") if f.suffix in text and "__pycache__" not in f.parts and b"\r\n" in f.read_bytes()]
    assert not crlf, f"CRLF line endings (check .gitattributes and run git add --renormalize .): {crlf[:3]}"
    assert json.loads(path.read_text(encoding="utf-8")) == now, "a bundled plugin changed: rerun with UPDATE_GOLDEN=1"


def test_git_checks_out_lf_everywhere():
    """official.json pins exact bytes, so Windows must not convert the bundled plugins to CRLF on checkout."""
    attrs = (Path(__file__).resolve().parent.parent / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attrs and "*.png binary" in attrs


@pytest.mark.parametrize("group", ["<b>x</b>", "a\\nb", "x" * 49, " lead"])
def test_group_names_are_plain_short_words(group):
    with pytest.raises(ValueError):
        spec(group=group)
    assert spec(group="Back end_2").group == "Back end_2"


def test_rustup_cargo_sees_its_toolchains_but_not_cargo_credentials():
    from pythonator.policy.compile import base_env

    host = dataclasses.replace(REFERENCE["linux"], runtimes=(("rust", "/home/me/.cargo/bin/cargo"),),
                               bwrap="/usr/bin/bwrap")
    s = spec(runtime="rust", command=["run"])
    argv = " ".join(compile_service(s, host).argv)
    assert "/home/me/.rustup" in argv and "/home/me/.cargo/bin" in argv
    assert " /home/me/.cargo " not in argv + " "
    assert base_env(s, home="/home/service", host=host)["RUSTUP_HOME"] == "/home/me/.rustup"


def test_every_subprocess_on_windows_opens_no_console_window() -> None:
    """pythonatord has no console, so on Windows each console child (podman, wsl, git, uv, ruff) would open a window."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "pythonator"
    missing = []
    for p in root.rglob("*.py"):
        if p.name in ("wslrun.py", "launcher.py") or "resources" in p.parts:
            continue  # these run inside Linux (WSL or the jail), never on Windows
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name not in ("create_subprocess_exec", "run", "Popen") or (
                    name == "run" and not (isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "subprocess")):
                continue
            kws = [k for k in node.keywords]
            ok = any(k.arg == "creationflags" for k in kws) or any(
                k.arg is None and ("NO_WINDOW" in ast.unparse(k.value) or "_detach" in ast.unparse(k.value)
                                   or "kw" in ast.unparse(k.value)) for k in kws)
            if not ok:
                missing.append(f"{p.relative_to(root)}:{node.lineno}")
    assert not missing, missing


def test_a_container_the_engine_cannot_run_is_blocked_not_restarted(tmp_path):
    """A pull that fails (exit 125) or a command missing from the image would only fail again: Blocked, with why."""
    from pythonator.model.service import ServiceSpec
    from pythonator.policy.reference import REFERENCE
    from pythonator.reconcile.reconciler import Reconciler

    rec = Reconciler(tmp_path, REFERENCE["linux"])
    spec = ServiceSpec.model_validate({"name": "api", "folder": "/srv/api", "command": ["main.py"], "mode": "container",
                                       "container": {"image": "python:13.3-slim"}}, strict=False)
    lg = rec.log("api")
    lg.append("e", "Error: unable to copy from source docker://python:13.3-slim: manifest unknown")
    assert "RUN-CTR-004" in rec._engine_refused("api", 125, spec)
    lg.append("e", "ERROR (catatonit:2): failed to exec pid1: No such file or directory")
    assert "RUN-CTR-006" in rec._engine_refused("api", 1, spec)
    assert rec._engine_refused("other", 1, spec) == ""  # an ordinary crash still restarts


def test_removing_a_service_removes_its_log(tmp_path):
    """A new service with the same name started with the old one's log (890,264 lines on a fresh service)."""
    import asyncio

    from pythonator.policy.reference import REFERENCE
    from pythonator.reconcile.reconciler import Reconciler

    rec = Reconciler(tmp_path, REFERENCE["linux"])
    for i in range(100):
        rec.log("api").append("o", f"line {i}")
    asyncio.run(rec.remove("api"))
    assert rec.log("api").total == 0


def test_wsl_distribution_missing_is_blocked_and_the_ready_mark_dropped(tmp_path):
    """Engines said "Pythonator's distro ready" while wsl.exe said WSL_E_DISTRO_NOT_FOUND, and it restarted every 3 s."""
    import dataclasses

    from pythonator.model.service import ServiceSpec
    from pythonator.policy.reference import REFERENCE
    from pythonator.reconcile.reconciler import Reconciler

    (tmp_path / "wsl").mkdir()
    (tmp_path / "wsl" / "ready").write_text("1")
    rec = Reconciler(tmp_path, dataclasses.replace(REFERENCE["windows"], data_dir=str(tmp_path)))
    spec = ServiceSpec.model_validate({"name": "api", "folder": "C:\\Users\\me\\api", "command": ["main.py"],
                                       "mode": "wsl"}, strict=False)
    rec.log("api").append("o", "There is no distribution with the supplied name.")
    rec.log("api").append("o", "Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND")
    assert "RUN-WSL-004" in rec._engine_refused("api", 4294967295, spec)
    assert not (tmp_path / "wsl" / "ready").exists() and not rec.host.wsl_ready


def test_a_run_that_can_never_work_is_held_not_restarted_every_tick(tmp_path):
    """WSL exited 126 ("the limits couldn't be set") and was started again every second: Blocked must hold until you
    start it again or change it."""
    import asyncio

    from pythonator.model.service import ServiceSpec
    from pythonator.policy.reference import REFERENCE
    from pythonator.reconcile.reconciler import Observed, Phase, Reconciler

    rec = Reconciler(tmp_path, REFERENCE["linux"])
    spec = ServiceSpec.model_validate({"name": "api", "folder": "/srv/api", "command": ["main.py"], "mode": "wsl",
                                       "desired": "running"}, strict=False)
    rec.specs["api"] = spec
    starts = []

    async def fake_start(s, ob):
        starts.append(s.name)

    rec._start = fake_start
    rec.log("api").append("e", "pythonator wsl: the limits couldn't be set (cgroup v2 in WSL: Permission denied)")
    ob = rec.observed.setdefault("api", Observed(name="api"))
    rec._hold(ob, "A protection layer couldn't be applied, so the service was not run: " + rec._last_error("api"))
    assert ob.phase is Phase.BLOCKED and "limits couldn't be set" in ob.message
    for _ in range(3):
        asyncio.run(rec.tick())
    assert starts == []
    rec.set_desired("api", spec.desired)  # Start again
    asyncio.run(rec.tick())
    assert starts == ["api"]


def test_wsl_runner_delegates_cgroup_controllers(tmp_path, monkeypatch):
    """New WSL groups had no memory.max: the controllers were never turned on for the groups below."""
    import errno

    from pythonator.jail import wslrun

    base = tmp_path / "cg"
    for d in (base, base / "pythonator"):
        d.mkdir(parents=True)
        (d / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
        (d / "cgroup.subtree_control").write_text("")
    (base / "cgroup.procs").write_text("1\n7\n")
    real_open = open
    busy = {"left": 1}

    def fake_open(path, mode="r", *a, **kw):  # the top of the namespace still holds init: EBUSY once
        if str(path) == str(base / "cgroup.subtree_control") and "w" in mode and busy["left"]:
            busy["left"] -= 1
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert wslrun.limit("api", 256, 1.0, 128, base=str(base)) == "cgroup"
    monkeypatch.setattr("builtins.open", real_open)
    assert (base / "cgroup.subtree_control").read_text() == "+memory +pids +cpu"
    assert (base / "pythonator" / "cgroup.subtree_control").read_text() == "+memory +pids +cpu"
    assert (base / "init" / "cgroup.procs").read_text() == "7"  # init's processes moved to a leaf
    g = base / "pythonator" / "api"
    assert (g / "memory.max").read_text() == "256M" and (g / "pids.max").read_text() == "128"
    assert (g / "cgroup.procs").read_text() == str(__import__("os").getpid())

    def died(msg):
        raise SystemExit(msg)

    monkeypatch.setattr(wslrun, "die", died)
    (base / "cgroup.controllers").write_text("cpu pids\n")
    with pytest.raises(SystemExit, match="no memory controller"):
        wslrun.limit("api", 256, 1.0, 128, base=str(base))


def test_console_shows_colours_and_marks_control_characters():
    """`\\x1b[90m[server] alive\\x1b[0m` showed as a box glyph and "[90m": colours are colours, the rest visible marks."""
    from pythonator.ui import ansi

    assert ansi.segments("\x1b[90m[server] alive · 0 hits\x1b[0m") == [("[server] alive · 0 hits", "tone:faint", False)]
    assert ansi.plain("a\x07b\x1b[2Kc\x1b]0;title\x07d") == "a␇bcd"  # cursor moves and titles dropped, the bell shown
    assert ansi.plain("x‮evil") == "x�evil"  # a bidi override can't reorder the line
    assert ansi.plain("10%\r50%\r100%") == "100%"
    assert ansi.segments("\x1b[1;38;5;196mX\x1b[38;2;1;2;3mY") == [("X", "#ff0000", True), ("Y", "#010203", True)]
    assert ansi.plain("lone \x1b") == "lone ␛"


def test_a_character_split_by_an_over_long_line_stays_whole(tmp_path):
    import asyncio

    from pythonator.policy.reference import REFERENCE
    from pythonator.reconcile.reconciler import Reconciler

    rec = Reconciler(tmp_path, REFERENCE["linux"])

    async def go():
        r = asyncio.StreamReader(limit=16)
        data = ("x" * 15 + "€ ok\n").encode()  # the € (3 bytes) straddles the 16-byte limit
        r.feed_data(data)
        r.feed_eof()
        await rec._pump("api", r, "o", None)

    asyncio.run(go())
    assert "".join(x.text for x in rec.log("api").tail(4)) == "x" * 15 + "€ ok"


def test_rename_keeps_the_log_and_approval_and_refuses_clashes(tmp_path):
    import asyncio

    from pythonator.model.service import ServiceSpec
    from pythonator.policy.reference import REFERENCE
    from pythonator.policy.risk import risk_hash
    from pythonator.reconcile.reconciler import Reconciler

    rec = Reconciler(tmp_path, REFERENCE["linux"])
    for n in ("api", "web"):
        rec.store.save(ServiceSpec.model_validate({"name": n, "folder": f"/srv/{n}", "command": ["main.py"]}, strict=False))
    rec.reload()
    assert rec.store.approve("api", risk_hash(rec.specs["api"]))
    rec.log("api").append("o", "hello")
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(rec.rename("api", "web"))
    asyncio.run(rec.rename("api", "orders"))
    assert "api" not in rec.specs and rec.store.is_approved(rec.specs["orders"])
    assert [x.text for x in rec.log("orders").tail(2)] == ["hello"]
    assert rec.specs["orders"].folder == "/srv/api"


def test_completion_names_are_parsed_never_run_and_skip_links(tmp_path):
    from pythonator.work import files

    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "cart.py").write_text("TAX = 0.2\nclass Cart:\n    def total(self): ...\nopen('/x', 'w')\n")
    (tmp_path / "broken.py").write_text("def f(:\n")
    sp = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    (sp / "requests").mkdir(parents=True)
    (sp / "requests" / "adapters.py").write_text("")
    (sp / "six.py").write_text("")
    (sp / "requests-2.0.dist-info").mkdir()
    outside = tmp_path.parent / "secret_mod.py"
    outside.write_text("LEAK = 1\n")
    try:
        (tmp_path / "linked.py").symlink_to(outside)
    except OSError:
        pass
    words = files.names(str(tmp_path))
    assert {"shop.cart", "shop.cart.TAX", "shop.cart.Cart", "shop.cart.Cart.total", "broken"} <= set(words)
    assert {"requests", "requests.adapters", "six"} <= set(words)
    assert not any("LEAK" in w or "dist-info" in w or "linked" in w for w in words)
    assert not os.path.exists("/x")


@pytest.mark.skipif(sys.platform == "win32", reason="the Linux autostart entry")
def test_window_at_login_writes_only_our_own_entry(tmp_path, monkeypatch):
    from pythonator.service import osservice

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert osservice.window_at_login() is False
    assert osservice.window_at_login(True) is True
    entry = (tmp_path / "autostart" / "pythonator.desktop").read_text()
    assert f'Exec="{sys.executable}" -m pythonator --tray' in entry
    assert osservice.window_at_login(False) is False and not (tmp_path / "autostart" / "pythonator.desktop").exists()
