"""Policy compiler, folder deny-list and risk hash. Golden files hold the exact argv for each case."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from pythonator.model.service import ServiceSpec
from pythonator.policy.compile import compile_service
from pythonator.policy.paths import check_folder
from pythonator.policy.plan import Refused, Status
from pythonator.policy.risk import risk_hash, widenings

GOLDEN = Path(__file__).parent / "golden"


def spec(**kw):
    base = {"name": "api", "folder": "/home/me/services/api", "command": ["main.py"]}
    base.update(kw)
    return ServiceSpec.model_validate(base, strict=False)


CASES = {
    "sandbox-offline": {},
    "sandbox-anywhere-port": {"network": {"traffic": {"mode": "any"}, "ports": [{"port": 8000}]}},
    "sandbox-lan-port": {"network": {"traffic": {"mode": "any"}, "ports": [{"port": 8080, "reach": "lan"}]}},
    "full": {"mode": "full"},
    "container": {"mode": "container", "container": {"image": "python:3.13-slim"}, "env": {"LOG": "1"}},
    "win-sandbox-anywhere": {"folder": "C:\\Users\\me\\services\\api", "network": {"traffic": {"mode": "any"}}},
    "win-full": {"folder": "C:\\Users\\me\\services\\api", "mode": "full"},
    "win-wsl": {"folder": "C:\\Users\\me\\services\\api", "mode": "wsl"},
    "win-wsl-hosts": {"folder": "C:\\Users\\me\\services\\api", "mode": "wsl",
                      "network": {"traffic": {"mode": "hosts", "hosts": ["api.example.com"]}}},
    "mac-sandbox-port": {"folder": "/Users/me/services/api", "network": {"traffic": {"mode": "any"}, "ports": [{"port": 8000}]}},
    "mac-full": {"folder": "/Users/me/services/api", "mode": "full"},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_golden_argv(case, host, request):
    """Every flag that reaches a jail, per OS. A change shows up as a diff in tests/golden."""
    import dataclasses

    if case.startswith("win-"):
        host = dataclasses.replace(
            request.getfixturevalue("win"),
            appcontainer=True,
            winjail="C:\\Program Files\\Pythonator\\jail\\winjail.py",
            wsl="C:\\Windows\\System32\\wsl.exe",
            wsl_ready=True,
        )
    elif case.startswith("mac-"):
        host = request.getfixturevalue("mac")
    jail = compile_service(spec(**CASES[case]), host)
    got = {
        "argv": list(jail.argv),
        "env": jail.env,
        "cwd": jail.cwd,
        "layers": [[x.key, str(x.status), x.by] for x in jail.plan.layers],
    }
    path = GOLDEN / f"{case}.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.write_text(json.dumps(got, indent=1) + "\n")
    assert got == json.loads(path.read_text()), f"argv changed for {case}; run with UPDATE_GOLDEN=1 if intended"


def test_sandbox_never_binds_home(host):
    argv = compile_service(spec(), host).argv
    for i, a in enumerate(argv):
        if a in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try"):
            assert argv[i + 1] != "/home/me" and not argv[i + 1].startswith("/home/me/.")


def test_offline_has_no_network(host):
    jail = compile_service(spec(), host)
    assert "--unshare-net" in jail.argv
    assert jail.plan.layer("network").status is Status.ENFORCED


def test_allowed_hosts_go_through_the_gate_only(host):
    import json

    jail = compile_service(spec(network={"traffic": {"mode": "hosts", "hosts": ["api.example.com"]}}), host)
    assert "--unshare-net" in jail.argv and "/usr/bin/pasta" not in jail.argv  # no route out but the relay
    short = hashlib.sha256(b"api").hexdigest()[:10]
    assert jail.gate and jail.gate.hosts == ("api.example.com",) and jail.gate.unix.endswith(f"/run/n/{short}/gate.sock")
    assert jail.env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    cfg = json.loads(jail.argv[len(jail.argv) - jail.argv[::-1].index("/.pythonator/launcher.py")])
    assert cfg["relay"]["out"] == [3128, "/.pythonator/net/gate.sock"] and cfg["block_net"] is False


def test_port_with_traffic_off_uses_the_gate_listener(host):
    jail = compile_service(spec(network={"ports": [{"port": 8000}]}), host)
    assert jail.gate and jail.gate.unix == "" and jail.gate.ports[0][:2] == (8000, False)
    assert jail.plan.layer("ports").by == "gate listener"


def test_anywhere_blackholes_metadata_and_lan(host):
    argv = compile_service(spec(network={"traffic": {"mode": "any"}}), host).argv
    i = argv.index("--net-guard")
    assert argv[i + 1] == "--block-lan"
    lan = compile_service(spec(network={"traffic": {"mode": "any", "allow_lan": True}}), host).argv
    assert "--block-lan" not in lan and "--net-guard" in lan


def test_missing_bwrap_refuses(host):
    from dataclasses import replace

    with pytest.raises(Refused) as e:
        compile_service(spec(), replace(host, bwrap=""))
    assert e.value.code == "RUN-LNX-001"


def test_no_user_session_is_labelled_reduced(host):
    from dataclasses import replace

    jail = compile_service(spec(), replace(host, systemd_user=False))
    assert jail.plan.layer("limits").status is Status.REDUCED
    assert jail.watchdog_memory_mb == 256


def test_container_values_never_in_argv(host):
    jail = compile_service(spec(mode="container", container={"image": "x"}, env={"TOKEN_X": "s3cret-value"}), host)
    assert not any("s3cret-value" in a for a in jail.argv)
    assert "--cap-drop" in jail.argv and "--read-only" in jail.argv


@pytest.mark.parametrize(
    "folder,code",
    [
        ("/home/me", "POL-PATH-002"),
        ("/", "POL-PATH-003"),
        ("/etc", "POL-PATH-004"),
        ("/home", "POL-PATH-005"),
        ("/home/me/.ssh", "POL-PATH-006"),
        ("/home/me/.ssh/keys", "POL-PATH-006"),
        ("/home/me/.config", "POL-PATH-007"),
        ("/home/me/.config/autostart", "POL-PATH-008"),
        ("/home/me/.local/share/Pythonator/x", "POL-PATH-009"),
    ],
)
def test_folder_denied(folder, code):
    v = check_folder(folder, "/home/me", data_dir="/home/me/.local/share/Pythonator")
    assert not v.ok and v.code == code


def test_folder_allowed():
    assert check_folder("/home/me/services/api", "/home/me").ok


def test_forbidden_env():
    with pytest.raises(ValueError):
        spec(env={"LD_PRELOAD": "/x.so"})
    with pytest.raises(ValueError):
        spec(env={"PYTHONPATH": "/x"})


def test_risk_hash_ignores_start_stop_but_not_widening():
    a = spec()
    assert risk_hash(a) == risk_hash(a.evolve(desired="running"))
    b = a.evolve(network={"traffic": {"mode": "any"}, "ports": []})
    assert risk_hash(a) != risk_hash(b)
    assert widenings(a, b) == ["traffic off → any"]
    assert widenings(b, a) == []  # narrowing never asks


def test_container_engine_choice(host):
    from dataclasses import replace

    s = spec(mode="container", container={"image": "x"})
    assert compile_service(s, replace(host, podman="/usr/bin/podman", docker="/usr/bin/docker")).argv[0] == "podman"
    assert compile_service(s, replace(host, podman="", docker="/usr/bin/docker")).argv[0] == "docker"
    with pytest.raises(Refused) as e:
        compile_service(s, replace(host, podman=""))
    assert e.value.code == "RUN-CTR-001"


@pytest.mark.parametrize(
    "folder,code",
    [
        ("services\\api", "POL-PATH-001"),
        ("C:\\Users\\me", "POL-PATH-002"),
        ("c:\\users\\ME", "POL-PATH-002"),  # Windows paths ignore case
        ("D:\\", "POL-PATH-003"),
        ("C:\\Windows\\Temp\\x", "POL-PATH-004"),
        ("C:\\Program Files\\App", "POL-PATH-004"),
        ("C:\\Users", "POL-PATH-005"),
        ("C:\\Users\\me\\.ssh", "POL-PATH-006"),
        ("C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data\\Default", "POL-PATH-006"),
        ("C:\\Users\\me\\AppData\\Roaming\\Microsoft", "POL-PATH-007"),
        ("C:\\Users\\me\\AppData\\Local\\Pythonator", "POL-PATH-006"),  # all of AppData holds secrets
    ],
)
def test_windows_folder_denied(folder, code):
    v = check_folder(folder, "C:\\Users\\me", data_dir="C:\\Users\\me\\AppData\\Local\\Pythonator")
    assert not v.ok and v.code == code, v


def test_windows_folder_allowed():
    assert check_folder("C:\\Users\\me\\services\\api", "C:\\Users\\me").ok
    assert check_folder("D:/projects/api", "C:\\Users\\me").ok


@pytest.fixture
def win():
    from pythonator.policy.host import HostFacts

    return HostFacts(
        os="windows",
        home="C:\\Users\\me",
        data_dir="C:\\Users\\me\\AppData\\Local\\Pythonator",
        python="C:\\Python313\\python.exe",
        docker="C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe",
    )


def wspec(**kw):
    return spec(folder="C:\\Users\\me\\services\\api", **kw)


def test_windows_sandbox_needs_a_working_appcontainer(win):
    with pytest.raises(Refused) as e:
        compile_service(wspec(), win)
    assert e.value.code == "RUN-WIN-002"


@pytest.fixture
def winbox(win):
    import dataclasses

    return dataclasses.replace(
        win,
        appcontainer=True,
        winjail="C:\\pz\\jail\\winjail.py",
        python="C:\\Users\\me\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
    )


def _cfg(jail):
    import json

    assert jail.argv[1:4] == ("-I", "-S", "C:\\pz\\jail\\winjail.py")
    return json.loads(jail.argv[4])


def test_windows_sandbox_is_an_appcontainer_in_a_job(winbox):
    jail = compile_service(wspec(), winbox)
    cfg = _cfg(jail)
    assert cfg["job"] == {"memory_mb": 256, "cpu": 1.0, "processes": 128}
    assert cfg["box"]["name"] == "Pythonator.api" and cfg["box"]["caps"] == []  # traffic off: no network capability
    grants = dict(map(tuple, cfg["box"]["grants"]))
    assert grants["C:\\Users\\me\\services\\api"] is True
    assert grants["C:\\Users\\me\\AppData\\Local\\Programs\\Python\\Python313"] is False  # runtime read-only
    assert "C:\\Users\\me" not in grants  # never the home folder
    assert jail.box == "Pythonator.api" and dict(jail.grants) == grants
    assert cfg["cmdline"].endswith("python.exe -u main.py")
    status = {x.key: x.status for x in jail.plan.layers}
    assert status["tree"] is Status.ENFORCED and status["limits"] is Status.ENFORCED and status["files"] is Status.ENFORCED


def test_windows_sandbox_network_capabilities(winbox):
    any_ = _cfg(compile_service(wspec(network={"traffic": {"mode": "any"}}), winbox))
    assert any_["box"]["caps"] == ["S-1-15-3-1"]  # internet, not the local network
    lan = _cfg(compile_service(wspec(network={"traffic": {"mode": "any", "allow_lan": True}}), winbox))
    assert lan["box"]["caps"] == ["S-1-15-3-1", "S-1-15-3-3"]


def test_windows_full_access_is_in_a_job(winbox):
    jail = compile_service(wspec(mode="full", env={"LOG": "1"}), winbox)
    cfg = _cfg(jail)
    assert cfg["box"] is None and cfg["job"]["memory_mb"] == 256
    assert jail.host_cwd == "C:\\Users\\me\\services\\api" and jail.watchdog_memory_mb == 0
    status = {x.key: x.status for x in jail.plan.layers}
    assert status == {
        "tree": Status.ENFORCED,
        "limits": Status.ENFORCED,
        **{k: Status.NOT_ENFORCED for k in ("files", "syscalls", "network", "loopback", "ports")},
    }
    assert "PATH" not in jail.env and "HOME" not in jail.env and jail.env["LOG"] == "1"


def test_wsl_needs_pythonators_distro(winbox):
    import dataclasses

    with pytest.raises(Refused) as e:
        compile_service(wspec(mode="wsl"), dataclasses.replace(winbox, wsl="C:\\Windows\\System32\\wsl.exe"))
    assert e.value.code == "RUN-WSL-003"


def test_wsl_runs_jailed_in_own_distro(winbox):
    import dataclasses
    import json

    host = dataclasses.replace(winbox, wsl="C:\\Windows\\System32\\wsl.exe", wsl_ready=True)
    jail = compile_service(wspec(mode="wsl", env={"TOKEN_NAME": "s3cr3t-value"}, secrets=("API_KEY",)), host)
    assert jail.argv[:5] == ("C:\\Windows\\System32\\wsl.exe", "--distribution", "Pythonator", "--user", "root")
    cfg = json.loads(jail.argv[-1])
    assert cfg["folder"] == "C:\\Users\\me\\services\\api" and cfg["net"] is False
    assert (
        "s3cr3t-value" not in " ".join(jail.argv) and "API_KEY" in cfg["env_keys"]
    )  # values travel through WSLENV, never the command line
    assert "TOKEN_NAME/u" in jail.env["WSLENV"]
    assert jail.stop_argv[-2:] == ("--stop", "api")
    with pytest.raises(Refused) as e:
        compile_service(wspec(mode="wsl", network={"traffic": {"mode": "any"}, "ports": [{"port": 8080, "reach": "lan"}]}), host)
    assert e.value.code == "NET-WSL-003"


@pytest.fixture
def mac():
    from pythonator.policy.host import HostFacts

    return HostFacts(
        os="darwin",
        home="/Users/me",
        data_dir="/Users/me/Library/Application Support/Pythonator",
        python="/opt/homebrew/bin/python3",
        sandbox_exec="/usr/bin/sandbox-exec",
    )


def test_macos_sandbox_is_seatbelt_with_params(mac):
    jail = compile_service(spec(folder="/Users/me/services/my api (1)"), mac)
    assert jail.argv[:2] == ("/usr/bin/sandbox-exec", "-p")
    profile = jail.argv[2]
    assert "(deny default)" in profile and "/Users/me/services" not in profile  # folders are parameters, never text
    assert "-D" in jail.argv and any(a.endswith("=/Users/me/services/my api (1)") for a in jail.argv)
    assert "network-outbound" not in profile  # traffic off
    assert jail.marker == "api-g1" and jail.env["PYTHONATOR_RUN"] == "api-g1" and jail.watchdog_memory_mb == 256
    assert jail.make_dirs[0].endswith("/homes/api")
    status = {x.key: x.status for x in jail.plan.layers}
    assert status["files"] is Status.ENFORCED and status["tree"] is Status.REDUCED


def test_macos_blocks_loopback_with_traffic_on(mac):
    profile = compile_service(spec(folder="/Users/me/services/api", network={"traffic": {"mode": "any"}}), mac).argv[2]
    assert profile.index('(allow network-outbound (remote ip "*:*"))') < profile.index(
        '(deny network-outbound (remote ip "localhost:*"))'
    )


def test_macos_without_seatbelt_is_refused(mac):
    import dataclasses

    with pytest.raises(Refused) as e:
        compile_service(spec(folder="/Users/me/services/api"), dataclasses.replace(mac, sandbox_exec=""))
    assert e.value.code == "RUN-MAC-001"


def test_windows_container_uses_docker_desktop(win):
    jail = compile_service(wspec(mode="container", container={"image": "python:3.13-slim"}), win)
    assert jail.argv[0] == "docker"
    assert "C:\\Users\\me\\services\\api:/app:rw" in jail.argv and "--userns" not in jail.argv


def test_windows_secret_folder_refused_in_every_mode(win):
    for extra in ({"mode": "full"}, {"mode": "container", "container": {"image": "x"}}):
        with pytest.raises(Refused) as e:
            compile_service(spec(folder="C:\\Users\\me\\.ssh", **extra), win)
        assert e.value.code == "POL-PATH-006"


def test_gate_socket_path_too_long_is_refused(host):
    import dataclasses

    long_host = dataclasses.replace(host, run_dir="/" + "x" * 120)
    with pytest.raises(Refused) as e:
        compile_service(spec(network={"traffic": {"mode": "hosts", "hosts": ["api.example.com"]}}), long_host)
    assert e.value.code == "NET-GATE-004"


@pytest.mark.parametrize(("runtime", "command", "tail"), [
    ("python", ["main.py", "--port", "8000"], ["python", "-u", "main.py", "--port", "8000"]),  # was exec'd as pid 1
    ("python", ["python", "app.py"], ["python", "app.py"]),
    ("node", ["server.js"], ["node", "server.js"]),
    ("binary", ["./server"], ["./server"]),
    ("python", ["/usr/local/bin/server"], ["/usr/local/bin/server"]),  # a path inside the image runs as is
])
def test_container_runs_the_command_with_its_runtime(host, runtime, command, tail):
    """`podman run python:3.13-slim main.py` fails with "catatonit: failed to exec pid1": the script needs python."""
    s = spec(mode="container", container={"image": "python:3.13-slim"}, runtime=runtime, command=command)
    argv = list(compile_service(s, host).argv)
    assert argv[argv.index("python:3.13-slim") + 1:] == tail


# --- GUI apps -----------------------------------------------------------------------------------------------------
def test_window_app_off_leaves_the_risk_hash_alone():
    assert risk_hash(spec()) == risk_hash(spec(gui=False))
    assert risk_hash(spec()) != risk_hash(spec(gui=True))
    assert "may open windows on your screen" in widenings(spec(), spec(gui=True))
    assert not any("windows" in w for w in widenings(spec(gui=True), spec(gui=True)))


def test_window_app_is_refused_in_a_linux_sandbox_and_container(host):
    for kw in ({}, {"mode": "container", "container": {"image": "x"}}):
        with pytest.raises(Refused) as e:
            compile_service(spec(gui=True, **kw), host)
        assert e.value.code == "GUI-001"


def test_window_app_in_full_access_on_linux_gets_the_display(host):
    import dataclasses

    with pytest.raises(Refused) as e:
        compile_service(spec(gui=True, mode="full"), host)
    assert e.value.code == "GUI-LNX-002"
    seen = dataclasses.replace(host, display=(("DISPLAY", ":0"), ("XAUTHORITY", "/home/me/.Xauthority")))
    jail = compile_service(spec(gui=True, mode="full"), seen)
    assert jail.env["DISPLAY"] == ":0"
    assert jail.plan.layers[-1].key == "display"
    assert "DISPLAY" not in compile_service(spec(mode="full"), seen).env


def test_window_app_in_a_windows_sandbox_keeps_the_appcontainer(winbox):
    plain, gui = compile_service(wspec(), winbox), compile_service(wspec(gui=True), winbox)
    assert gui.argv == plain.argv
    layer = gui.plan.layers[-1]
    assert (layer.key, layer.status) == ("display", Status.REDUCED)
    with pytest.raises(Refused):
        compile_service(wspec(gui=True, mode="wsl"), dataclasses_wsl(winbox))


def dataclasses_wsl(h):
    import dataclasses

    return dataclasses.replace(h, wsl="C:\\Windows\\System32\\wsl.exe", wsl_ready=True)
