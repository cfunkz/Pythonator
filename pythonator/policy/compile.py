"""Policy compiler: ServiceSpec + HostFacts -> Jail. Pure functions, no I/O.
Every flag that reaches a sandbox tool is produced here, so a change is one line in this file and one line in a
golden file (tests/golden). If a layer the spec needs can't be applied on this machine, compile raises Refused:
there is no fallback that quietly runs with less."""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
import subprocess
from dataclasses import dataclass, field

from pythonator.model.service import Limits, Mode, Reach, ServiceSpec
from pythonator.policy.host import HostFacts
from pythonator.policy.paths import check_folder, is_windows_path
from pythonator.policy.plan import EnforcementPlan, Layer, Refused, Status

APP_DIR = "/app"
SVC_HOME = "/home/service"
LAUNCHER_IN_JAIL = "/.pythonator/launcher.py"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin"

@dataclass(frozen=True)
class Jail:
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    plan: EnforcementPlan
    unit: str = ""  # systemd scope name, when limits come from a cgroup
    watchdog_memory_mb: int = 0  # >0 when the reconciler must enforce memory itself (no cgroup)
    container_name: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    host_cwd: str | None = None  # where the first process starts on the host (None: the jail sets it)
    box: str = ""  # Windows AppContainer name
    grants: tuple[tuple[str, bool], ...] = ()  # folders the AppContainer is given, written to the ledger first
    stop_argv: tuple[str, ...] = ()  # run before the tree is ended (WSL: end the run inside Linux)
    make_dirs: tuple[str, ...] = ()  # private folders the reconciler creates (owner-only) before the start
    marker: str = ""  # env PYTHONATOR_RUN value: Stop also ends processes that left the tree (macOS)
    gate: GatePlan | None = None  # the egress gate this run needs
    rule_argv: tuple[str, ...] = ()  # WSL: the gate runs inside the distribution; its allow list is updated with this
    network: str = ""  # Docker: the service's own network, created before the start (never the shared bridge)

def unit_name(spec: ServiceSpec) -> str:
    return f"pythonator-{spec.name}-g{spec.generation}"

def _interpreter(spec: ServiceSpec, host: HostFacts) -> list[str]:
    from pythonator.policy.runtimes import RUNTIMES

    if spec.runtime == "binary":
        return []
    rt = RUNTIMES[spec.runtime]
    exe = host.runtime(spec.runtime)
    if not exe:
        code = {"python": "RUN-RT-001", "node": "RUN-RT-002"}.get(spec.runtime, "RUN-RT-003")
        raise Refused(code, f"No {rt.label} runtime was found on this computer.", rt.install)
    return [exe, *rt.prefix]

def _runtime_exes(spec: ServiceSpec, host: HostFacts) -> list[str]:
    """The executables whose install folders the jail sees: the launcher's Python, and the service's own runtime."""
    out = [host.python] if host.python else []
    if spec.runtime not in ("python", "binary"):
        exe = host.runtime(spec.runtime)
        if exe:
            out.append(exe)
    return out

def _real(path: str, host: HostFacts) -> list[tuple[str, HostFacts]]:
    """The path as written and, on this machine, where its links lead: both must pass (a link named ~/proj that
    points at ~/.ssh is ~/.ssh). The resolved path is checked against the resolved home, data and program folders,
    so a home reached through a link (/home -> /data/home, /var -> /private/var) can't hide ~/.ssh. A spec compiled
    for another OS (System check's matrix) is checked as text."""
    import dataclasses
    import os
    import sys

    here = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform, "")
    if host.os != here:
        return [(path, host)]
    r = os.path.realpath
    real = dataclasses.replace(host, home=r(host.home), data_dir=r(host.data_dir) if host.data_dir else "",
                               program=tuple(r(d) for d in host.program if d),
                               path_dirs=tuple(r(d) for d in host.path_dirs if d))
    return [(path, host), (r(path), real)]

def _check_folders(spec: ServiceSpec, host: HostFacts) -> None:
    for p, h in _real(spec.folder, host):
        v = check_folder(p, h.home, data_dir=h.data_dir, write=True, program_dirs=h.program, path_dirs=h.path_dirs)
        if not v.ok:
            raise Refused(v.code, v.reason, "Choose the project's own folder.")
    for g in spec.folders:
        for p, h in _real(g.path, host):
            v = check_folder(p, h.home, data_dir=h.data_dir, write=g.write, program_dirs=h.program, path_dirs=h.path_dirs)
            if not v.ok:
                raise Refused(v.code, f"{g.path}: {v.reason}", "Remove this folder from the service.")

def base_env(spec: ServiceSpec, *, home: str, host: HostFacts | None = None) -> dict[str, str]:
    from pythonator.policy.runtimes import RUNTIMES, rustup_home

    path = BASE_PATH
    exe = host.runtime(spec.runtime) if host is not None and spec.runtime != "binary" else ""
    if exe and "/" in exe and not exe.startswith("/usr/"):
        path = posixpath.dirname(exe) + ":" + path  # cargo runs rustc, go runs its tools: they must be found
    env = {
        "HOME": home, "PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb", "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONATOR_SERVICE": spec.name,
    }
    for k, v in RUNTIMES[spec.runtime].env:
        if "{home}" not in v or home:
            env[k] = v.replace("{home}", home)
    ports = spec.network.ports
    if ports:
        # The first port and where to bind it, so `PORT`-reading frameworks just work. In a private network namespace
        # (container, WSL, Linux sandbox) 0.0.0.0 is only the jail's own address; on the host itself the bind follows
        # the reach that was approved, so a "this computer" port is never opened to the network by a default.
        own_ns = spec.mode in (Mode.CONTAINER, Mode.WSL) or (spec.mode is Mode.SANDBOX and host is not None
                                                             and host.os == "linux")
        lan = any(p.reach is not Reach.THIS_COMPUTER for p in ports)
        env["PORT"] = str(ports[0].port)
        env["HOST"] = "0.0.0.0" if own_ns or lan else "127.0.0.1"  # noqa: S104
    if exe and (rh := rustup_home(exe)):
        env["RUSTUP_HOME"] = rh
    env.update(spec.env)
    return env

def _usr_binds(host: HostFacts) -> list[str]:
    """/bin, /lib, /lib64 and /sbin: links into /usr on merged systems, read-only binds elsewhere."""
    if host.usr_merged:
        return [a for d in ("bin", "lib", "lib64", "sbin") for a in ("--symlink", f"usr/{d}", f"/{d}")]
    return [a for d in ("/bin", "/lib", "/lib64", "/sbin") for a in ("--ro-bind-try", d, d)]

def _limits_prefix(spec: ServiceSpec, host: HostFacts) -> tuple[list[str], Layer, Layer, str, int]:
    """systemd-run --scope when a user manager exists; otherwise the reconciler's watchdog (labelled reduced)."""
    lim = spec.limits
    if host.systemd_run and host.systemd_user:
        unit = unit_name(spec)
        prefix = [
            host.systemd_run, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "-p",
            f"MemoryMax={lim.memory_mb}M", "-p", "MemorySwapMax=0", "-p", f"CPUQuota={int(round(lim.cpu * 100))}%",
            "-p", f"TasksMax={lim.tasks}", "--",
        ]
        tree = Layer("tree", "Process tree", Status.ENFORCED, "cgroup scope", "Stop ends every child.")
        limits = Layer(
            "limits", "Limits", Status.ENFORCED, "cgroup v2", f"{lim.memory_mb} MB · {lim.cpu:g} CPU · {lim.tasks} tasks"
        )
        return prefix, tree, limits, unit, 0
    tree = Layer("tree", "Process tree", Status.ENFORCED, "PID namespace", "Stop ends every child (the isolated PID 1 exits).")
    limits = Layer(
        "limits", "Limits", Status.REDUCED, "watchdog",
        f"Memory {lim.memory_mb} MB checked every second; CPU and task caps need a systemd user session.",
    )
    return [], tree, limits, "", lim.memory_mb

def compile_linux(spec: ServiceSpec, host: HostFacts) -> Jail:
    if spec.mode is Mode.WSL:
        raise Refused("RUN-WSL-001", "WSL mode is only available on Windows.", "Choose Sandbox mode.")
    _check_folders(spec, host)
    if spec.mode is Mode.CONTAINER:
        return compile_container(spec, host, pick_engine(spec, host))
    if spec.mode is Mode.FULL:
        return _compile_full_linux(spec, host)
    return _compile_sandbox_linux(spec, host)

GATE_KEY_SLOT = "@pythonator-gate-key@"  # WSL: the reconciler puts a fresh key here at each start (signs gate events)
GATE_PORT = 3128  # inside the jail: the relay to the egress gate (HTTP_PROXY / HTTPS_PROXY)
NET_IN_JAIL = "/.pythonator/net"
SOCKET_PATH_MAX = 100  # sun_path is 108 bytes on Linux, 104 on macOS

@dataclass(frozen=True)
class GatePlan:
    """What the egress gate opens for one run."""

    hosts: tuple[str, ...]
    allow_lan: bool
    unix: str = ""  # Linux: the gate listens here; bound into the jail
    tcp: int = 0  # macOS: the gate listens on 127.0.0.1:tcp, the only loopback port the profile allows
    ports: tuple[tuple[int, bool, str], ...] = ()  # (port, from the LAN, the relay's socket on the host side)

def gate_port(spec: ServiceSpec) -> int:
    import zlib

    return 41000 + zlib.crc32(spec.name.encode()) % 8000

def _proxy_env(port: int) -> dict[str, str]:
    url = f"http://127.0.0.1:{port}"
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url, "ALL_PROXY": url, "NO_PROXY": ""}

def _net_layers(spec: ServiceSpec, host: HostFacts) -> tuple[list[str], list[str], list[Layer], GatePlan | None]:
    """Returns (pasta prefix, bwrap net flags, layers, gate). Off and Allowed hosts: no network namespace route at
    all, only the relay to the gate. Anywhere: pasta, with link-local (and, unless allowed, LAN) blackholed."""
    t = spec.network.traffic
    ports = spec.network.ports
    reach = ", ".join(f"{p.port} ({'this computer' if p.reach is Reach.THIS_COMPUTER else 'local network'})" for p in ports)
    if t.mode in ("off", "hosts"):
        run = f"{host.run_dir or host.data_dir + '/run'}/n/{hashlib.sha256(spec.name.encode()).hexdigest()[:10]}"
        gate = None
        if t.mode == "hosts" or ports:
            if len(run) + len("/in-65535.sock") > SOCKET_PATH_MAX:
                raise Refused(
                    "NET-GATE-004",
                    f"The folder for this service's network sockets is too long for a Unix socket ({run}).",
                    "Set PYTHONATOR_DATA to a shorter folder, for example ~/.pythonator.",
                )
            gate = GatePlan(
                hosts=t.hosts if t.mode == "hosts" else (), allow_lan=t.allow_lan,
                unix=f"{run}/gate.sock" if t.mode == "hosts" else "",
                ports=tuple((p.port, p.reach is Reach.LAN, f"{run}/in-{p.port}.sock") for p in ports),
            )
        net = (
            Layer("network", "Network", Status.ENFORCED, "own network namespace", "No network at all.")
            if t.mode == "off"
            else Layer(
                "network", "Network", Status.ENFORCED, "own namespace + egress gate",
                f"Only {', '.join(t.hosts) or 'no hosts yet'}; every attempt is logged.",
            )
        )
        layers = [
            net, Layer("loopback", "Programs on 127.0.0.1", Status.ENFORCED, "own network namespace", "Unreachable."),
            Layer("ports", "Ports", Status.ENFORCED if ports else Status.OFF, "gate listener" if ports else "closed", reach),
        ]
        return [], ["--unshare-net"], layers, gate
    if not host.pasta:
        raise Refused(
            "NET-LNX-001", "Traffic needs pasta to give the service its own network.",
            "Install the 'passt' package (it provides pasta).",
        )
    prefix = [host.pasta, "--config-net", "--quiet", "--no-map-gw", "-T", "none", "-U", "none", "-u", "none"]
    if ports:
        for p in ports:
            prefix += ["-t", f"127.0.0.1/{p.port}" if p.reach is Reach.THIS_COMPUTER else str(p.port)]
    else:
        prefix += ["-t", "none"]
    guard = [host.python or "/usr/bin/python3", "-I", "-S", host.launcher, "--net-guard"]
    prefix += ["--", *guard, *([] if t.allow_lan else ["--block-lan"]), "--"]
    layers = [
        Layer(
            "network", "Network", Status.ENFORCED, "own namespace (pasta)",
            "Anywhere on the internet" + ("" if not t.allow_lan else " and your local network") + "; never cloud metadata.",
        ),
        Layer("loopback", "Programs on 127.0.0.1", Status.ENFORCED, "pasta --no-map-gw", "Unreachable."),
        Layer("ports", "Ports", Status.ENFORCED if ports else Status.OFF, "pasta port forward" if ports else "closed", reach),
    ]
    return prefix, [], layers, None

def _compile_sandbox_linux(spec: ServiceSpec, host: HostFacts) -> Jail:
    if not host.bwrap:
        raise Refused(
            "RUN-LNX-001", "Sandbox mode needs bubblewrap, which isn't installed.",
            "Install the 'bubblewrap' package, or choose Container mode.",
        )
    pasta, netflags, net_layers, gate = _net_layers(spec, host)
    cg, tree, limits, unit, wd = _limits_prefix(spec, host)
    has_net = spec.network.traffic.mode == "any"

    b: list[str] = [host.bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    b += netflags
    b += ["--die-with-parent", "--new-session", "--cap-drop", "ALL", "--hostname", spec.name]
    b += ["--ro-bind", "/usr", "/usr"]
    b += _usr_binds(host)
    etc = [
        "/etc/ssl", "/etc/ca-certificates", "/etc/pki", "/etc/alternatives", "/etc/localtime", "/etc/ld.so.cache",
        "/etc/nsswitch.conf", "/etc/mime.types",
    ]
    if has_net:
        etc += ["/etc/resolv.conf", "/etc/hosts", "/etc/gai.conf"]
    for e in etc:
        b += ["--ro-bind-try", e, e]
    b += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home", "--dir", SVC_HOME]  # noqa: S108 - private tmpfs inside the jail
    for root in _roots(spec, host, ("/usr/",)):
        b += ["--ro-bind-try", root, root]  # -try: ~/.rustup may not exist; Landlock skips a missing path too
    b += ["--bind", spec.folder, APP_DIR]
    rules: list[list[object]] = [
        [APP_DIR, True],
        ["/usr", False],
        ["/etc", False],
        ["/tmp", True],  # noqa: S108
        [SVC_HOME, True],
        ["/proc", False],
        ["/dev", True],
    ]
    for i, g in enumerate(spec.folders):
        target = f"/data/{i}-{posixpath.basename(g.path.rstrip('/')) or 'folder'}"
        b += ["--bind" if g.write else "--ro-bind", g.path, target]
        rules.append([target, g.write])
    for root in _roots(spec, host, ("/usr/",)):
        rules.append([root, False])
    b += ["--ro-bind", host.launcher, LAUNCHER_IN_JAIL, "--chdir", APP_DIR]
    relay: dict[str, object] = {}
    env = base_env(spec, home=SVC_HOME, host=host)
    dirs: tuple[str, ...] = ()
    if gate:
        run = posixpath.dirname(gate.unix or gate.ports[0][2])
        dirs = (run,)
        b += ["--bind", run, NET_IN_JAIL]
        rules.append([NET_IN_JAIL, True])
        if gate.unix:
            relay["out"] = [GATE_PORT, f"{NET_IN_JAIL}/gate.sock"]
            env.update(_proxy_env(GATE_PORT))
        relay["in"] = [[port, f"{NET_IN_JAIL}/{posixpath.basename(sock)}"] for port, _lan, sock in gate.ports]

    interp = _interpreter(spec, host)
    if not host.seccomp:
        raise Refused(
            "RUN-LNX-002", "Sandbox mode needs libseccomp for its system-call filter, which couldn't be found.",
            "Install the 'libseccomp2' package (libseccomp on Fedora and Arch).",
        )
    cfg = json.dumps(
        {
            "landlock": rules if host.landlock_abi >= 1 else [], "abi": host.landlock_abi, "seccomp": True,
            "block_net": not has_net and not gate, "relay": relay,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    launcher_py = host.python or "/usr/bin/python3"
    b += ["--", launcher_py, "-I", "-S", LAUNCHER_IN_JAIL, cfg, "--", *interp, *spec.command]

    files = Layer(
        "files", "Files", Status.ENFORCED, "bubblewrap + Landlock" if host.landlock_abi >= 1 else "bubblewrap mounts",
        "This folder (read-write) and the runtime (read-only). Nothing else of your home.",
    )
    sysc = Layer(
        "syscalls", "Syscalls", Status.ENFORCED, "seccomp · no_new_privs · no capabilities",
        "No ptrace, mounts, keyrings, bpf, io_uring, new user namespaces or setuid files.",
    )
    plan = EnforcementPlan("linux", "sandbox", (tree, limits, files, sysc, *net_layers))
    return Jail(tuple(cg + pasta + b), env, APP_DIR, plan, unit, wd, make_dirs=dirs, gate=gate)

AS_YOU = (
    Layer("files", "Files", Status.NOT_ENFORCED, "runs as you", "Can read and write everything you can."),
    Layer("syscalls", "Syscalls", Status.NOT_ENFORCED, "runs as you", "No filter."),
    Layer("network", "Network", Status.NOT_ENFORCED, "runs as you", "Can reach anything you can."),
    Layer("loopback", "Programs on 127.0.0.1", Status.NOT_ENFORCED, "runs as you", "Reachable."),
    Layer("ports", "Ports", Status.NOT_ENFORCED, "runs as you", "Can listen on any port and address."),
)

def _no_allow_list(spec: ServiceSpec) -> None:
    if spec.network.traffic.mode == "hosts":
        raise Refused(
            "NET-FULL-001",
            "Full access runs as you, so an allow-list of hosts can't be enforced; it would be ignored.",
            "Set Traffic to Anywhere, or use Sandbox mode for an allow-list.",
        )

def _compile_full_linux(spec: ServiceSpec, host: HostFacts) -> Jail:
    _no_allow_list(spec)
    cg, tree, limits, unit, wd = _limits_prefix(spec, host)
    if not unit:
        tree = Layer(
            "tree", "Process tree", Status.REDUCED, "process group + tree walk",
            "A child that leaves the group can outlive Stop without a systemd user session.",
        )
    argv = [*cg, *_interpreter(spec, host), *spec.command]
    plan = EnforcementPlan("linux", "full", (tree, limits, *AS_YOU))
    env = base_env(spec, home=host.home, host=host)
    return Jail(tuple(argv), env, spec.folder, plan, unit, wd, host_cwd=spec.folder)

def pick_engine(spec: ServiceSpec, host: HostFacts) -> str:
    """Podman (rootless) when it's there, else Docker; or the one the spec names."""
    want = spec.container.engine if spec.container else "auto"
    if want in ("auto", "podman") and host.podman:
        return "podman"
    if want in ("auto", "docker") and host.docker:
        return "docker"
    raise Refused(
        "RUN-CTR-001",
        "Container mode needs Podman or Docker."
        if want == "auto"
        else f"Container mode is set to {want}, which isn't installed.",
        "Install Podman (recommended, rootless), or choose Sandbox mode.",
    )

def _in_image(spec: ServiceSpec) -> list[str]:
    """The command inside the image. A Python service's command is its script (`main.py --port 8000`), so it runs as
    `python -u main.py …`, the same as in Sandbox; without that the engine tries to execute main.py itself and fails
    with "failed to exec pid1". An empty command keeps the image's own entrypoint (a database image)."""
    from pythonator.policy.runtimes import RUNTIMES

    cmd = list(spec.command)
    rt = RUNTIMES.get(spec.runtime)
    if not cmd or rt is None or not rt.exe:
        return cmd
    exe = "python" if spec.runtime == "python" else rt.exe[0]
    if cmd[0] in (*rt.exe, exe) or cmd[0].startswith("/"):
        return cmd  # already names its program ("python main.py", "/app/server")
    return [exe, *rt.prefix, *cmd]

def compile_container(spec: ServiceSpec, host: HostFacts, engine: str = "podman") -> Jail:
    """Hardened `run` argv. There are no fields for --privileged, host mounts or socket mounts: they can't be asked for."""
    if spec.container is None:
        raise Refused("SPEC-IMG-001", "Container mode needs an image.", "Set an image, for example python:3.13-slim.")
    t = spec.network.traffic
    if t.mode == "hosts":
        raise Refused(
            "NET-CTR-001",
            "Allowed hosts goes through Pythonator's egress gate, which containers can't use: their engine owns the network.",
            "Use Sandbox mode for an allow-list, or set Traffic to Off or Anywhere.",
        )
    drive = 2 if is_windows_path(spec.folder) else 0
    if any(c in spec.folder[drive:] for c in ",:\"'") or any(c in spec.folder for c in "\n\r"):
        raise Refused(
            "RUN-CTR-002",
            "The engine reads ',' ':' and quotes in a mount as options, so this folder can't be mounted safely.",
            "Rename the folder (or a parent) without , : or quotes.",
        )
    name = f"pythonator-{spec.name}"
    lim = spec.limits
    a = [
        engine,
        "run",
        "--rm",
        "--init",
        "--interactive",  # the console's "send a line to stdin"
        "--name",
        name,
        "--label",
        "pythonator.owner=1",
        "--label",
        f"pythonator.service={spec.name}",
        "--label",
        f"pythonator.generation={spec.generation}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        f"--memory={lim.memory_mb}m",
        f"--memory-swap={lim.memory_mb}m",
        f"--cpus={lim.cpu:g}",
        f"--pids-limit={lim.tasks}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",  # noqa: S108
        "--tmpfs",
        f"{SVC_HOME}:rw,nosuid,nodev,size=64m,mode=1777",  # a writable home on a read-only root
        "--env",
        f"HOME={SVC_HOME}",
        "-v",
        f"{spec.folder}:{APP_DIR}:rw" + (",Z" if engine == "podman" else ""),
        "-w",
        APP_DIR,
    ]
    if engine == "podman":
        a += ["--userns", "keep-id"]
    elif host.os != "windows" and host.uid:
        a += ["--user", f"{host.uid}:{host.gid or host.uid}"]  # rootful Docker: never root inside, files stay yours
    if t.mode == "off":
        a += ["--network", "none"]
    elif engine == "podman":
        a += ["--network", "pasta:--no-map-gw"]  # the host's own addresses stay out of reach
    else:
        a += ["--network", name]  # its own bridge: containers on Docker's default bridge reach each other
    for p in spec.network.ports:
        a += ["-p", f"127.0.0.1:{p.port}:{p.port}" if p.reach is Reach.THIS_COMPUTER else f"{p.port}:{p.port}"]
    for k in sorted(spec.env):
        a += ["-e", k]  # values come from the process env, never the argv
    for k in spec.secrets:
        a += ["-e", k]
    a += [spec.container.image, *_in_image(spec)]
    plan = EnforcementPlan(
        host.os,
        "container",
        (
            Layer("tree", "Process tree", Status.ENFORCED, "container"),
            Layer(
                "limits", "Limits", Status.ENFORCED, f"{engine} cgroup",
                f"{lim.memory_mb} MB · {lim.cpu:g} CPU · {lim.tasks} tasks",
            ),
            Layer("files", "Files", Status.ENFORCED, "read-only image + this folder"),
            Layer("syscalls", "Syscalls", Status.ENFORCED, f"{engine} default seccomp · no capabilities"),
            Layer(
                "network", "Network", Status.ENFORCED, "none" if t.mode == "off" else f"{engine} network",
                "No network." if t.mode == "off" else "Anywhere.",
            ),
            Layer(
                "loopback",
                "Programs on this computer",
                Status.ENFORCED if t.mode == "off" or engine == "podman" else Status.REDUCED,
                "no network" if t.mode == "off" else "pasta --no-map-gw" if engine == "podman" else "Docker bridge",
                "Unreachable." if t.mode == "off" or engine == "podman"
                else "Docker's bridge can reach services listening on this computer's network addresses and your LAN. "
                "Use Podman, or Sandbox mode, to block them.",
            ),
            Layer(
                "ports", "Ports", Status.ENFORCED if spec.network.ports else Status.OFF,
                "published ports" if spec.network.ports else "closed",
            ),
        ),
    )
    env = dict(spec.env)
    own_net = name if engine == "docker" and t.mode != "off" else ""
    return Jail(tuple(a), env, spec.folder, plan, container_name=name, host_cwd=spec.folder, network=own_net)

WIN_CAP_INTERNET, WIN_CAP_LAN = "S-1-15-3-1", "S-1-15-3-3"

def box_name(spec: ServiceSpec) -> str:
    """The service's AppContainer. Service names are already [a-z0-9-], which AppContainer names allow."""
    return f"Pythonator.{spec.name}"

def _runtime_roots(host: HostFacts, spec: ServiceSpec | None = None) -> list[str]:
    """Windows: install folders of the runtimes, read-only for the sandbox (python.exe sits in the prefix, or in
    Scripts of a venv)."""
    return _roots(spec, host, ())

def _roots(spec: ServiceSpec | None, host: HostFacts, system: tuple[str, ...]) -> list[str]:
    """Install folders the jail may read: the launcher's Python and the service's own runtime, minus system folders
    the jail already sees."""
    from pythonator.policy.runtimes import install_root, rustup_home

    exes = _runtime_exes(spec, host) if spec is not None else [host.python]
    out: list[str] = []
    for exe in exes:
        if exe and not exe.startswith(system):
            for r in (install_root(exe), rustup_home(exe)):
                if r and r not in out:
                    out.append(r)
    return out

def _job_layers(lim: Limits) -> tuple[Layer, Layer]:
    return (
        Layer("tree", "Process tree", Status.ENFORCED, "job object", "Stop ends every child, however deep."),
        Layer(
            "limits", "Limits", Status.ENFORCED, "job object",
            f"{lim.memory_mb} MB · {lim.cpu:g} CPU · {lim.tasks} processes",
        ),
    )

OUTSIDE_EXES = frozenset({"wsl", "wsl.exe", "bash", "bash.exe", "wslg.exe", "wslconfig.exe", "wslhost.exe"})

def _windows_command(spec: ServiceSpec) -> None:
    """WSL's launchers run outside any AppContainer and job; a .bat or .cmd goes through cmd.exe, whose quoting
    can't be made safe for arbitrary arguments (BatBadBut)."""
    first = ntpath.basename(spec.command[0]).lower()
    if spec.mode is Mode.SANDBOX and first in OUTSIDE_EXES:
        raise Refused("RUN-WIN-003", f"{spec.command[0]} starts programs outside the sandbox.",
                      "Use WSL mode to run Linux programs.")
    if spec.runtime == "binary" and first.endswith((".bat", ".cmd")):
        raise Refused("RUN-WIN-004", "Batch files run through cmd.exe, which can't pass arguments safely.",
                      "Run the program the batch file starts, or use PowerShell (runtime: powershell).")

def compile_windows(spec: ServiceSpec, host: HostFacts) -> Jail:
    """Windows: every native service starts in the stdlib jail launcher, which puts itself in a job object with
    kill-on-close and the limits, then (Sandbox) starts the program in the service's own AppContainer."""
    _check_folders(spec, host)
    if spec.mode is Mode.CONTAINER:
        return compile_container(spec, host, pick_engine(spec, host))
    if spec.mode is Mode.WSL:
        return compile_wsl(spec, host)
    t = spec.network.traffic
    if spec.mode is Mode.FULL:
        _no_allow_list(spec)
    if t.mode == "hosts":
        raise Refused(
            "NET-WIN-001",
            "An AppContainer can't reach the egress gate on this computer, so Sandbox mode on Windows has no allow-list.",
            "Use WSL mode: Allowed hosts works there, through the gate inside Pythonator's own WSL distribution.",
        )
    _windows_command(spec)
    lim = spec.limits
    tree, limits = _job_layers(lim)
    cmdline = subprocess.list2cmdline([*_interpreter(spec, host), *spec.command])
    job: dict[str, object] = {"memory_mb": lim.memory_mb, "cpu": lim.cpu, "processes": lim.tasks}
    env = {k: v for k, v in base_env(spec, home="", host=host).items() if k not in ("HOME", "PATH", "LANG", "LC_ALL")}
    env.update(spec.env)
    if spec.mode is Mode.FULL:
        cfg = {"job": job, "box": None, "cwd": spec.folder, "cmdline": cmdline}
        plan = EnforcementPlan("windows", "full", (tree, limits, *AS_YOU))
        argv = (host.python, "-I", "-S", host.winjail, json.dumps(cfg, separators=(",", ":")))
        return Jail(argv, env, spec.folder, plan, host_cwd=spec.folder)
    if spec.network.ports:
        raise Refused(
            "NET-WIN-002",
            "Windows blocks connections into an AppContainer, even from this computer, unless an administrator "
            "changes the firewall for it. Pythonator never makes that change.",
            "Use WSL mode (this computer) or Container mode (this computer or your network) for a service with ports.",
        )
    if not host.appcontainer:
        raise Refused(
            "RUN-WIN-002", "Windows refused to start a program in an AppContainer on this computer.",
            "Run System check for the reason, or choose Container or WSL mode.",
        )
    caps: list[str] = []
    if t.mode == "any":
        caps.append(WIN_CAP_INTERNET)
    if t.mode == "any" and (t.allow_lan or any(p.reach is Reach.LAN for p in spec.network.ports)):
        caps.append(WIN_CAP_LAN)
    grants = [(spec.folder, True), *((g.path, g.write) for g in spec.folders), *((r, False) for r in _runtime_roots(host, spec))]
    seen: dict[str, bool] = {}
    for path, write in grants:
        seen[path] = seen.get(path, False) or write
    box = box_name(spec)
    cfg = {
        "job": job, "box": {"name": box, "caps": caps, "grants": [[p, w] for p, w in seen.items()]}, "cwd": spec.folder,
        "cmdline": cmdline,
    }
    net = {
        "off": "No network capability: it can't open a connection at all.",
        "any": "Anywhere on the internet" + ("" if WIN_CAP_LAN in caps else "; not your local network."),
    }[t.mode]
    plan = EnforcementPlan(
        "windows",
        "sandbox",
        (
            tree,
            limits,
            Layer(
                "files", "Files", Status.ENFORCED, "AppContainer + ACL grants",
                "This folder (read-write) and the runtime (read-only). Nothing else of your files.",
            ),
            Layer(
                "syscalls", "Syscalls", Status.REDUCED, "AppContainer token",
                "A low-rights token limits which kernel objects it can open; Windows has no system-call filter.",
            ),
            Layer("network", "Network", Status.ENFORCED, "AppContainer capabilities", net),
            Layer("loopback", "Programs on 127.0.0.1", Status.ENFORCED, "AppContainer", "Unreachable."),
            Layer("ports", "Ports", Status.OFF, "closed", "An AppContainer takes no connections."),
        ),
    )
    argv = (host.python, "-I", "-S", host.winjail, json.dumps(cfg, separators=(",", ":")))
    return Jail(argv, env, spec.folder, plan, host_cwd=spec.folder, box=box, grants=tuple(seen.items()))

def compile_darwin(spec: ServiceSpec, host: HostFacts) -> Jail:
    """macOS: Seatbelt (sandbox-exec) with a deny-by-default profile, a private home and temp folder, a run marker
    so Stop finds children that left the tree, and the reconciler's memory watchdog."""
    from pythonator.policy import seatbelt

    _check_folders(spec, host)
    if spec.mode is Mode.WSL:
        raise Refused("RUN-WSL-001", "WSL mode is only available on Windows.", "Choose Sandbox mode.")
    if spec.mode is Mode.CONTAINER:
        return compile_container(spec, host, pick_engine(spec, host))
    lim, t, ports = spec.limits, spec.network.traffic, spec.network.ports
    marker = f"{spec.name}-g{spec.generation}"
    tree = Layer(
        "tree",
        "Process tree",
        Status.REDUCED,
        "process group + run marker",
        "Stop ends the group and every process carrying this run's marker; a child that clears its environment and "
        "detaches can outlive Stop.",
    )
    limits = Layer(
        "limits", "Limits", Status.REDUCED, "watchdog",
        f"Memory {lim.memory_mb} MB checked every second; macOS has no CPU or task caps.",
    )
    if spec.mode is Mode.FULL:
        _no_allow_list(spec)
        env = base_env(spec, home=host.home, host=host)
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        env["PYTHONATOR_RUN"] = marker
        plan = EnforcementPlan("darwin", "full", (tree, limits, *AS_YOU))
        return Jail(
            (*_interpreter(spec, host), *spec.command), env, spec.folder, plan, watchdog_memory_mb=lim.memory_mb,
            host_cwd=spec.folder, marker=marker,
        )
    if not host.sandbox_exec:
        raise Refused(
            "RUN-MAC-001", "macOS refused the Seatbelt profile (sandbox-exec) on this Mac.",
            "Choose Container mode, or run System check for the reason.",
        )
    gate = GatePlan(t.hosts, t.allow_lan, tcp=gate_port(spec)) if t.mode == "hosts" else None
    home = f"{host.data_dir}/homes/{spec.name}"
    tmp = f"{home}/tmp"  # noqa: S108 - inside the private home, not /tmp
    grants = [(spec.folder, True), *((g.path, g.write) for g in spec.folders), (home, True)]
    for root in _roots(spec, host, ("/usr/", "/System/")):
        grants.append((root, False))
    text, params = seatbelt.profile(
        grants, network=t.mode == "any", ports=[(p.port, p.reach is Reach.LAN) for p in ports], base=host.seatbelt_base,
        gate=gate.tcp if gate else 0,
    )
    env = base_env(spec, home=home, host=host)
    if gate:
        env.update(_proxy_env(gate.tcp))
    env.update(
        PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", TMPDIR=tmp + "/", PYTHONATOR_RUN=marker, PYTHONATOR_SANDBOX="1"
    )
    reach = ", ".join(f"{p.port} ({'this computer' if p.reach is Reach.THIS_COMPUTER else 'local network'})" for p in ports)
    plan = EnforcementPlan(
        "darwin",
        "sandbox",
        (
            tree,
            limits,
            Layer(
                "files", "Files", Status.ENFORCED, "Seatbelt profile",
                "This folder (read-write), a private home and the runtime (read-only).",
            ),
            Layer(
                "syscalls", "Syscalls", Status.REDUCED, "Seatbelt operations",
                "Seatbelt allows or denies operations (starting programs, files, network, IPC), not system calls.",
            ),
            Layer(
                "network",
                "Network",
                Status.ENFORCED,
                "Seatbelt profile",
                {
                    "off": "No network at all.",
                    "any": "Anywhere on the internet.",
                    "hosts": f"Only {', '.join(t.hosts)}, through the gate.",
                }[t.mode],
            ),
            Layer("loopback", "Programs on 127.0.0.1", Status.ENFORCED, "Seatbelt profile", "Unreachable."),
            Layer(
                "ports", "Ports", Status.ENFORCED if ports else Status.OFF, "Seatbelt bind rules" if ports else "closed", reach
            ),
        ),
    )
    argv = (*seatbelt.argv(host.sandbox_exec, text, params), *_interpreter(spec, host), *spec.command)
    return Jail(
        argv, env, spec.folder, plan, watchdog_memory_mb=lim.memory_mb, host_cwd=spec.folder, make_dirs=(home, tmp),
        marker=marker,
    )

WSL_DISTRO = "Pythonator"
WSL_MISSING = ("bun", "deno", "dotnet", "powershell")  # not in Debian's signed archive
WSL_RUNNER = "/usr/local/lib/pythonator/wslrun.py"

def compile_wsl(spec: ServiceSpec, host: HostFacts) -> Jail:
    """WSL mode: a bubblewrap jail inside Pythonator's own WSL distribution, which mounts no drive and can't start
    Windows programs. Only the service's folders are mounted, by the runner, for the run."""
    if not host.wsl:
        raise Refused("RUN-WSL-002", "WSL isn't installed.", "In an administrator PowerShell: wsl --install --no-distribution")
    if not host.wsl_ready:
        raise Refused(
            "RUN-WSL-003", "Pythonator's own WSL distribution isn't set up yet.",
            "System check → WSL → Set up (installs Microsoft's Debian image, once).",
        )
    t, ports = spec.network.traffic, spec.network.ports
    if ports and t.mode != "any":
        raise Refused(
            "NET-WSL-002", "In WSL mode a port uses WSL's own forwarding, which needs Traffic: Anywhere.",
            "Set Traffic to Anywhere, close the port, or use Sandbox mode on Linux.",
        )
    if any(p.reach is Reach.LAN for p in ports):
        raise Refused(
            "NET-WSL-003", "WSL forwards ports to this computer only.", "Set the port to This computer, or use Container mode."
        )
    if spec.runtime in WSL_MISSING:
        raise Refused(
            "RUN-WSL-004", f"{spec.runtime} isn't in Pythonator's WSL distribution (it isn't in Debian's archive).",
            "Use Sandbox or Container mode for this runtime.",
        )
    from pythonator.policy.runtimes import RUNTIMES

    rt = RUNTIMES[spec.runtime]
    interp = [] if spec.runtime == "binary" else [{"java": "java", "rust": "cargo"}.get(spec.runtime, rt.exe[0]), *rt.prefix]
    env = {k: v for k, v in base_env(spec, home="", host=host).items() if k not in ("HOME", "PATH", "LANG", "LC_ALL")}
    keys = sorted(env)
    env["WSLENV"] = ":".join(f"{k}/u" for k in keys)
    lim = spec.limits
    cfg = {
        "name": spec.name,
        "folder": spec.folder,
        "folders": [[g.path, g.write] for g in spec.folders],
        "net": t.mode == "any",
        "gate": {"hosts": list(t.hosts), "allow_lan": t.allow_lan, "port": GATE_PORT, "key": GATE_KEY_SLOT}
        if t.mode == "hosts"
        else None,
        "memory_mb": lim.memory_mb,
        "cpu": lim.cpu,
        "tasks": lim.tasks,
        "argv": [*interp, *spec.command],
        "env_keys": sorted({*keys, *spec.secrets}),  # secrets join WSLENV at start, from the keychain
    }
    root = (host.wsl, "--distribution", WSL_DISTRO, "--user", "root", "--cd", "/", "--exec", "/usr/bin/python3", "-I")
    plan = EnforcementPlan(
        "windows",
        "wsl",
        (
            Layer("tree", "Process tree", Status.ENFORCED, "own Linux user id", "Stop ends every process of this service."),
            Layer(
                "limits", "Limits", Status.ENFORCED, "cgroup v2 in WSL",
                f"{lim.memory_mb} MB · {lim.cpu:g} CPU · {lim.tasks} tasks",
            ),
            Layer(
                "files", "Files", Status.ENFORCED, "bubblewrap in Pythonator's WSL",
                "Only this folder is mounted. Your drives aren't, and Windows programs can't be started.",
            ),
            Layer(
                "syscalls", "Syscalls", Status.ENFORCED, "seccomp · no_new_privs · no capabilities",
                "The same filter as Sandbox mode on Linux, inside Pythonator's WSL.",
            ),
            Layer(
                "network",
                "Network",
                Status.ENFORCED,
                {"off": "own network namespace", "hosts": "own namespace + egress gate in WSL"}.get(t.mode, "WSL network"),
                {"off": "No network at all.", "hosts": f"Only {', '.join(t.hosts) or 'no hosts yet'}; every attempt is logged."}.get(
                    t.mode, "Anywhere on the internet."),
            ),
            Layer(
                "loopback", "Programs on 127.0.0.1", Status.ENFORCED, "WSL virtual machine", "Windows' 127.0.0.1 isn't reachable."
            ),
            Layer(
                "ports", "Ports", Status.ENFORCED if ports else Status.OFF,
                "WSL localhost forwarding" if ports else "closed",
                ", ".join(f"{p.port} (this computer)" for p in ports),
            ),
        ),
    )
    return Jail(
        (*root, WSL_RUNNER, json.dumps(cfg, separators=(",", ":"))), env, "/app", plan, host_cwd=None,
        stop_argv=(*root, WSL_RUNNER, "--stop", spec.name),
        rule_argv=(*root, WSL_RUNNER, "--rule", spec.name) if t.mode == "hosts" else (),
    )

def compile_service(spec: ServiceSpec, host: HostFacts) -> Jail:
    if spec.gui:
        return _display(spec, host)
    return _compile(spec, host)

def _compile(spec: ServiceSpec, host: HostFacts) -> Jail:
    if host.os == "linux":
        return compile_linux(spec, host)
    if host.os == "windows":
        return compile_windows(spec, host)
    if host.os == "darwin":
        return compile_darwin(spec, host)
    raise Refused("RUN-OS-001", f"Pythonator doesn't run services on {host.os}.", "Use Linux, macOS or Windows.")

def _display(spec: ServiceSpec, host: HostFacts) -> Jail:
    """A service that opens windows (a PyQt, Tk or Electron app). On Windows it keeps its jail: an AppContainer
    may open its own windows, while other programs' windows and the clipboard stay out of reach. On Linux and macOS
    a jailed program with your display could read other windows and keystrokes (X11) or reach the window server,
    so windows are only for Full access there."""
    import dataclasses

    if host.os == "windows" and spec.mode in (Mode.SANDBOX, Mode.FULL):
        jail = _compile(spec, host)
        by, st, detail = ("AppContainer + job UI limits", Status.REDUCED, "Its own windows only: other programs' "
                          "windows, the clipboard and global hooks stay blocked.") if spec.mode is Mode.SANDBOX else (
                          "runs as you", Status.NOT_ENFORCED, "Its windows on your desktop, like any program you open.")
    elif spec.mode is Mode.FULL and host.os in ("linux", "darwin"):
        jail = _compile(spec, host)
        if host.os == "linux":
            if not any(k in ("DISPLAY", "WAYLAND_DISPLAY") for k, _v in host.display):
                raise Refused(
                    "GUI-LNX-002", "The background service doesn't know your display, so a GUI app can't open.",
                    "Run once: systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY, then restart "
                    "Pythonator's background service (or start it from the window).",
                )
            jail = dataclasses.replace(jail, env={**dict(host.display), **jail.env})
        by, st, detail = "runs as you", Status.NOT_ENFORCED, "Its windows on your desktop, like any program you open."
    else:
        raise Refused(
            "GUI-001",
            "GUI apps can't run in this mode here: with your screen, an isolated program could read other windows "
            "and what you type, and Container and WSL have no screen of yours.",
            "Use Full access for a GUI app" + (", or Sandbox on Windows." if host.os == "windows" else "."),
        )
    layer = Layer("display", "Your screen", st, by, detail)
    plan = dataclasses.replace(jail.plan, layers=(*jail.plan.layers, layer))
    return dataclasses.replace(jail, plan=plan)

PLUGIN_DIR = "/plugin"
SDK_DIR = "/sdk"
PLUGIN_MEMORY_MB = 256

def compile_plugin(plugin_id: str, plugin_dir: str, sdk_dir: str, entry: str, host: HostFacts) -> Jail:
    """A plugin's jail. Same building blocks as a Sandbox service, but stricter and not configurable:
    its own code read-only, no home, no network, no writes outside a private /tmp. What a plugin may do beyond
    computing comes only from host calls, each checked against its grants."""
    if host.os == "windows":
        return _plugin_windows(plugin_id, plugin_dir, sdk_dir, entry, host)
    if host.os == "darwin":
        return _plugin_darwin(plugin_id, plugin_dir, sdk_dir, entry, host)
    if not host.bwrap:
        raise Refused("PLG-LNX-001", "Plugins need bubblewrap, which isn't installed.", "Install the 'bubblewrap' package.")
    if not host.landlock_abi:
        raise Refused("PLG-LNX-002", "Plugins need Landlock (Linux 5.13 or newer).", "Update the kernel.")
    b: list[str] = [host.bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    b += ["--hostname", "plugin", "--ro-bind", "/usr", "/usr"]
    b += _usr_binds(host)
    b += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache", "--proc", "/proc", "--dev", "/dev"]
    b += ["--tmpfs", "/tmp", "--tmpfs", "/home", "--dir", SVC_HOME]  # noqa: S108 - private to the jail
    rules: list[list[object]] = [[PLUGIN_DIR, False], [SDK_DIR, False], ["/usr", False], ["/tmp", True], ["/dev", True]]  # noqa: S108
    if host.python and not host.python.startswith("/usr/"):
        root = posixpath.dirname(posixpath.dirname(host.python))
        b += ["--ro-bind", root, root]
        rules.append([root, False])
    b += ["--ro-bind", plugin_dir, PLUGIN_DIR, "--ro-bind", sdk_dir, SDK_DIR, "--ro-bind", host.launcher, LAUNCHER_IN_JAIL]
    b += ["--chdir", PLUGIN_DIR]
    cfg = json.dumps(
        {"landlock": rules, "abi": host.landlock_abi, "seccomp": host.seccomp, "block_net": True},
        separators=(",", ":"), sort_keys=True,
    )
    py = host.python or "/usr/bin/python3"
    b += ["--", py, "-I", "-S", LAUNCHER_IN_JAIL, cfg, "--", py, "-I", "-X", "pycache_prefix=/tmp/pyc", f"{SDK_DIR}/pythonator_sdk.py",
          f"{PLUGIN_DIR}/{entry}"]  # pycache_prefix: a __pycache__ shipped in the plugin is never loaded
    plan = EnforcementPlan(
        "linux",
        "plugin",
        (
            Layer("tree", "Process tree", Status.ENFORCED, "PID namespace"),
            Layer("limits", "Limits", Status.REDUCED, "watchdog", f"{PLUGIN_MEMORY_MB} MB checked every second"),
            Layer("files", "Files", Status.ENFORCED, "bubblewrap + Landlock", "Its own code, read-only. No home."),
            Layer("network", "Network", Status.ENFORCED, "own network namespace", "None. Host calls only."),
        ),
    )
    env = {"HOME": SVC_HOME, "PATH": BASE_PATH, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONATOR_PLUGIN": plugin_id}
    return Jail(tuple(b), env, PLUGIN_DIR, plan, watchdog_memory_mb=PLUGIN_MEMORY_MB)

def _plugin_plan(os_name: str, tree: Layer, limits: Layer, files_by: str) -> EnforcementPlan:
    return EnforcementPlan(
        os_name,
        "plugin",
        (
            tree, limits, Layer("files", "Files", Status.ENFORCED, files_by, "Its own code, read-only. No home."),
            Layer("network", "Network", Status.ENFORCED, files_by, "None. Host calls only."),
        ),
    )

def _plugin_windows(plugin_id: str, plugin_dir: str, sdk_dir: str, entry: str, host: HostFacts) -> Jail:
    if not host.appcontainer:
        raise Refused(
            "PLG-WIN-001", "Plugins need an AppContainer, which Windows refused here.", "Run System check for the reason."
        )
    box = f"Pythonator.plugin.{plugin_id}"[:64]
    grants = [(plugin_dir, False), (sdk_dir, False), *((r, False) for r in _runtime_roots(host))]
    cmd = subprocess.list2cmdline([host.python, "-I", "-X", f"pycache_prefix={ntpath.join(plugin_dir, '.no-pycache')}",
                                   ntpath.join(sdk_dir, "pythonator_sdk.py"), ntpath.join(plugin_dir, entry)])
    cfg = {
        "job": {"memory_mb": PLUGIN_MEMORY_MB, "cpu": 1.0, "processes": 4},
        "box": {"name": box, "caps": [], "grants": [[p, w] for p, w in grants]}, "cwd": plugin_dir, "cmdline": cmd,
    }
    plan = _plugin_plan(
        "windows", Layer("tree", "Process tree", Status.ENFORCED, "job object"),
        Layer("limits", "Limits", Status.ENFORCED, "job object", f"{PLUGIN_MEMORY_MB} MB · 4 processes"),
        "AppContainer, no capabilities",
    )
    env = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONATOR_PLUGIN": plugin_id}
    argv = (host.python, "-I", "-S", host.winjail, json.dumps(cfg, separators=(",", ":")))
    return Jail(argv, env, plugin_dir, plan, host_cwd=plugin_dir, box=box, grants=tuple(grants))

def _plugin_darwin(plugin_id: str, plugin_dir: str, sdk_dir: str, entry: str, host: HostFacts) -> Jail:
    from pythonator.policy import seatbelt

    if not host.sandbox_exec:
        raise Refused(
            "PLG-MAC-001", "Plugins need the Seatbelt sandbox, which macOS refused here.", "Run System check for the reason."
        )
    home = f"{host.data_dir}/homes/plugin-{plugin_id}"
    grants = [(plugin_dir, False), (sdk_dir, False), (home, True)]
    if host.python and not host.python.startswith(("/usr/", "/System/")):
        grants.append((posixpath.dirname(posixpath.dirname(host.python)), False))
    text, params = seatbelt.profile(grants, network=False, ports=[], base=host.seatbelt_base)
    marker = f"plugin-{plugin_id}"
    env = {
        "HOME": home, "TMPDIR": home + "/", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1",
        "PYTHONATOR_PLUGIN": plugin_id, "PYTHONATOR_RUN": marker,
    }
    argv = (
        *seatbelt.argv(host.sandbox_exec, text, params), host.python, "-I", "-X", f"pycache_prefix={home}/pyc",
        posixpath.join(sdk_dir, "pythonator_sdk.py"), posixpath.join(plugin_dir, entry),
    )
    plan = _plugin_plan(
        "darwin", Layer("tree", "Process tree", Status.REDUCED, "process group + run marker"),
        Layer("limits", "Limits", Status.REDUCED, "watchdog", f"{PLUGIN_MEMORY_MB} MB checked every second"),
        "Seatbelt profile",
    )
    return Jail(
        argv, env, plugin_dir, plan, watchdog_memory_mb=PLUGIN_MEMORY_MB, host_cwd=plugin_dir, make_dirs=(home,), marker=marker
    )

TOOL_DIR = "/tool"
TOOL_MEMORY_MB = 512

def compile_tool(tool_id: str, exe: str, args: tuple[str, ...], host: HostFacts) -> Jail:
    """A helper program the daemon runs on your code (the ruff linter): it reads the text on stdin and writes its
    answer on stdout. Same jail as a plugin: its own binary read-only, no home, no project folder, no network."""
    if host.os == "windows":
        if not host.appcontainer:
            raise Refused("TOOL-WIN-001", "Helper tools need an AppContainer, which Windows refused here.",
                          "Run System check for the reason.")
        root = ntpath.dirname(exe)
        box = f"Pythonator.tool.{tool_id}"[:64]
        cfg = {
            "job": {"memory_mb": TOOL_MEMORY_MB, "cpu": 1.0, "processes": 8},
            "box": {"name": box, "caps": [], "grants": [[root, False]]}, "cwd": root,
            "cmdline": subprocess.list2cmdline([exe, *args]),
        }
        plan = _plugin_plan(
            "windows", Layer("tree", "Process tree", Status.ENFORCED, "job object"),
            Layer("limits", "Limits", Status.ENFORCED, "job object", f"{TOOL_MEMORY_MB} MB"),
            "AppContainer, no capabilities",
        )
        argv = (host.python, "-I", "-S", host.winjail, json.dumps(cfg, separators=(",", ":")))
        return Jail(argv, {"PYTHONIOENCODING": "utf-8"}, root, plan, host_cwd=root, box=box, grants=((root, False),))
    if host.os == "darwin":
        from pythonator.policy import seatbelt

        if not host.sandbox_exec:
            raise Refused("TOOL-MAC-001", "Helper tools need the Seatbelt sandbox.", "Run System check for the reason.")
        root = posixpath.dirname(exe)
        home = f"{host.data_dir}/homes/tool-{tool_id}"
        text, params = seatbelt.profile([(root, False), (home, True)], network=False, ports=[], base=host.seatbelt_base)
        plan = _plugin_plan(
            "darwin", Layer("tree", "Process tree", Status.REDUCED, "process group"),
            Layer("limits", "Limits", Status.REDUCED, "watchdog", f"{TOOL_MEMORY_MB} MB"), "Seatbelt profile",
        )
        env = {"HOME": home, "TMPDIR": home + "/", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
        argv = (*seatbelt.argv(host.sandbox_exec, text, params), exe, *args)
        return Jail(argv, env, root, plan, watchdog_memory_mb=TOOL_MEMORY_MB, host_cwd=root, make_dirs=(home,))
    if not host.bwrap or not host.landlock_abi:
        raise Refused("TOOL-LNX-001", "Helper tools need bubblewrap and Landlock.", "Install the 'bubblewrap' package.")
    inside = f"{TOOL_DIR}/{posixpath.basename(exe)}"
    b: list[str] = [host.bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    b += ["--hostname", "tool", "--ro-bind", "/usr", "/usr"]
    b += _usr_binds(host)
    b += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache", "--proc", "/proc", "--dev", "/dev"]
    b += ["--tmpfs", "/tmp", "--tmpfs", "/home", "--dir", SVC_HOME]  # noqa: S108 - private to the jail
    rules: list[list[object]] = [[TOOL_DIR, False], ["/usr", False], ["/tmp", True], ["/dev", True]]  # noqa: S108
    if host.python and not host.python.startswith("/usr/"):
        root = posixpath.dirname(posixpath.dirname(host.python))
        b += ["--ro-bind", root, root]
        rules.append([root, False])
    b += ["--ro-bind", exe, inside, "--ro-bind", host.launcher, LAUNCHER_IN_JAIL, "--chdir", "/tmp"]  # noqa: S108
    cfg = json.dumps({"landlock": rules, "abi": host.landlock_abi, "seccomp": host.seccomp, "block_net": True},
                     separators=(",", ":"), sort_keys=True)
    py = host.python or "/usr/bin/python3"
    b += ["--", py, "-I", "-S", LAUNCHER_IN_JAIL, cfg, "--", inside, *args]
    plan = _plugin_plan(
        "linux", Layer("tree", "Process tree", Status.ENFORCED, "PID namespace"),
        Layer("limits", "Limits", Status.REDUCED, "watchdog", f"{TOOL_MEMORY_MB} MB"), "bubblewrap + Landlock",
    )
    env = {"HOME": SVC_HOME, "PATH": BASE_PATH, "LANG": "C.UTF-8"}
    return Jail(tuple(b), env, "/tmp", plan, watchdog_memory_mb=TOOL_MEMORY_MB)  # noqa: S108
