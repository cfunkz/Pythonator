"""WSL jail runner. Standard library only; installed into Pythonator's own WSL distribution as
/usr/local/lib/pythonator/wslrun.py and started by wsl.exe as root:
    wslrun.py CONFIG              run one service
    wslrun.py --stop NAME         end every process of that service
    wslrun.py --rule NAME HOSTS   give the running gate a new allow list (JSON: {"hosts": [...], "allow_lan": bool})
The distribution mounts no Windows drive and can't start Windows programs (wsl.conf: automount and interop off).
For each run this mounts only the service's own folder (and the folders it was given), puts the run in a cgroup
with its limits, switches to the service's own user id and starts the program in a bubblewrap jail: its own PID,
IPC, UTS and (when traffic is off) network namespaces, no capabilities, nothing of the distribution writable.
If a layer can't be applied it exits 126 and the service never runs.
Allowed hosts: the egress gate (the same gate.py the background service uses) runs here, outside the jail, as its
own unprivileged user. The jail has no network; launcher.py's relay inside it passes 127.0.0.1:3128 (the service's
HTTP(S)_PROXY) to the gate's Unix socket. Each attempt goes back to the background service as one stderr line,
signed with a key that only this runner and the background service know, so the service can't fake one."""

import errno
import hashlib
import hmac
import ipaddress
import json
import os
import signal
import subprocess
import sys
import zlib

ROOT = "/srv/pythonator"
LIB = "/usr/local/lib/pythonator"
GATE_DIR = "/run/pythonator"
EVENT = "\x1epythonator-gate "  # stderr line prefix; the background service checks the signature
EXIT_JAIL = 126

def die(msg):
    sys.stderr.write("pythonator wsl: " + msg + "\n")
    sys.stderr.flush()
    os._exit(EXIT_JAIL)

def uid_for(name):
    """Each service runs as its own user id, so "every process of this service" is exact."""
    return 20000 + zlib.crc32(name.encode()) % 40000

def stop(name):
    uid = uid_for(name)
    unfirewall(uid)
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if os.stat("/proc/" + pid).st_uid == uid:
                    os.kill(int(pid), signal.SIGKILL)
            except OSError:
                continue
    return 0

NFT = "/usr/sbin/nft"
TABLE = "inet pythonator"
REJECT = "reject with icmpx type admin-prohibited"
METADATA = ("169.254.0.0/16", "100.100.100.200", "fd00:ec2::254", "fe80::/10")
PRIVATE = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
           "fc00::/7", "ff00::/8")

def nameservers(path="/etc/resolv.conf"):
    """WSL's DNS (10.255.255.254 with DNS tunnelling, else the Windows host): reachable on port 53 only."""
    out = []
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        p = line.split()
        if len(p) >= 2 and p[0] == "nameserver":
            try:
                out.append(ipaddress.ip_address(p[1].split("%")[0]))
            except ValueError:
                continue
    return out

def gateways(path="/proc/net/route"):
    """The default gateway: the Windows host with NAT networking, your router with mirrored networking. Usually in
    a private range already; closed by name too, in case your network uses public or CGNAT addresses."""
    out = []
    try:
        with open(path) as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000" and p[2] != "00000000":
                    out.append(ipaddress.ip_address(int.from_bytes(bytes.fromhex(p[2]), "little")))
    except (OSError, ValueError):
        pass
    return out

def _daddr(nets):
    """nft `ip daddr {...}` / `ip6 daddr {...}` matches for a list of networks."""
    v4 = [n for n in nets if ":" not in str(n)]
    v6 = [n for n in nets if ":" in str(n)]
    return ([f"ip daddr {{ {', '.join(map(str, v4))} }}"] if v4 else []) + ([f"ip6 daddr {{ {', '.join(map(str, v6))} }}"] if v6 else [])

def firewall_rules(uid, ports, allow_lan, dns, gw=()):
    """One nft transaction: this service's own chain, reached through a uid -> chain map from the output hook. Its
    traffic can't reach 127.0.0.1 (Windows' own, in mirrored networking) apart from its own ports, cloud metadata,
    or (unless allow_lan) your network and this PC through WSL's gateway. Replies to connections it accepted pass."""
    c = f"svc{int(uid)}"
    r = [
        f"add table {TABLE}",
        f"add map {TABLE} svcs {{ type uid : verdict; }}",
        f"add chain {TABLE} output {{ type filter hook output priority 0; policy accept; }}",
        f"flush chain {TABLE} output",
        f"add rule {TABLE} output meta skuid vmap @svcs",
        f"add chain {TABLE} {c}",
        f"flush chain {TABLE} {c}",
        f"add rule {TABLE} {c} ct state established,related accept",
    ]
    for m in _daddr(dns):
        r.append(f"add rule {TABLE} {c} {m} meta l4proto {{ tcp, udp }} th dport 53 accept")
    own = ", ".join(str(int(p)) for p in ports if 0 < int(p) < 65536)
    if own:
        r.append(f"add rule {TABLE} {c} fib daddr type local tcp dport {{ {own} }} accept")
    # Every address of this machine, not only 127.0.0.1: in mirrored networking WSL has the PC's own addresses.
    r.append(f"add rule {TABLE} {c} fib daddr type local {REJECT}")
    blocked = ["127.0.0.0/8", "::1", *METADATA, *(() if allow_lan else (*PRIVATE, *gw))]
    r += [f"add rule {TABLE} {c} {m} {REJECT}" for m in _daddr(blocked)]
    r.append(f"add element {TABLE} svcs {{ {int(uid)} : jump {c} }}")
    return "\n".join(r) + "\n"

def firewall(uid, ports, allow_lan):
    """Traffic Anywhere shares WSL's network, so the block is per user id: each service runs as its own."""
    if not os.path.exists(NFT):
        die("Traffic Anywhere needs the firewall in Pythonator's WSL (nftables). Run System check → WSL → Set up again.")
    done = subprocess.run([NFT, "-f", "-"], input=firewall_rules(uid, ports, allow_lan, nameservers(), gateways()),  # noqa: S603
                          capture_output=True, text=True)
    if done.returncode != 0:
        die(f"the firewall couldn't be set: {done.stderr.strip()[:300]}")

def unfirewall(uid):
    if os.path.exists(NFT):
        for cmd in (f"delete element {TABLE} svcs {{ {int(uid)} }}", f"delete chain {TABLE} svc{int(uid)}"):
            subprocess.run([NFT, cmd], capture_output=True, check=False)  # noqa: S603 - absent is fine

def gate_uid(name):
    return 60001 + zlib.crc32(name.encode()) % 4000

def rule_path(name):
    return f"{GATE_DIR}/{name}/rule.json"

def set_rule(name, text):
    """--rule: the running gate re-reads this file within a second."""
    if not name.replace("-", "").isalnum():
        die("bad service name")
    rule = json.loads(text)
    data = json.dumps({"hosts": [str(h) for h in rule.get("hosts", [])], "allow_lan": bool(rule.get("allow_lan"))})
    tmp = rule_path(name) + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, rule_path(name))
    return 0

def start_gate(name, uid, g):
    """Fork the gate. Returns the socket path to bind into the jail. The gate ends when the run ends."""
    d = f"{GATE_DIR}/{name}"
    os.makedirs(d, mode=0o711, exist_ok=True)
    sock = d + "/gate.sock"
    set_rule(name, json.dumps(g))
    key = bytes.fromhex(g["key"]) if len(g.get("key", "")) == 32 else b""
    if not key:
        die("the egress gate has no key")
    ready_r, ready_w = os.pipe()
    parent = os.getpid()
    if os.fork() == 0:
        os.close(ready_r)
        try:
            run_gate(name, uid, sock, key, ready_w, parent)
        finally:
            os._exit(0)
    os.close(ready_w)
    if os.read(ready_r, 1) != b"1":
        die("the egress gate didn't start")
    os.close(ready_r)
    return sock

def run_gate(name, uid, sock, key, ready_w, parent):
    import asyncio

    sys.path.insert(0, LIB)
    import gate  # noqa: PLC0415 - installed next to this runner

    def emit(topic, data):
        if topic != "traffic":
            return
        body = json.dumps(data, separators=(",", ":"), sort_keys=True)
        sig = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
        try:
            os.write(2, (EVENT + sig + " " + body + "\n").encode())
        except OSError:
            pass

    def load():
        with open(rule_path(name)) as f:
            r = json.load(f)
        return gate.Rule(tuple(r.get("hosts", [])), bool(r.get("allow_lan")))

    async def main():
        g = gate.Gate(emit)
        await g.open(name, load(), unix=sock)
        os.chown(sock, uid, uid)
        os.chmod(sock, 0o600)
        gu = gate_uid(name)  # parse untrusted requests without root
        os.setgroups([])
        os.setgid(gu)
        os.setuid(gu)
        os.write(ready_w, b"1")
        os.close(ready_w)
        seen = None
        while os.getppid() == parent:  # the runner became bubblewrap (same pid); when it ends, so does the gate
            try:
                m = os.stat(rule_path(name)).st_mtime_ns
                if m != seen:
                    seen = m
                    g.update(name, load())
            except (OSError, ValueError):
                pass
            await asyncio.sleep(1)
        os._exit(0)  # the socket stays until the next start replaces it (this user may not delete it)

    asyncio.run(main())

def mount(win_path, target, uid, write):
    os.makedirs(target, exist_ok=True)
    if os.path.ismount(target):
        subprocess.run(["/bin/umount", "-l", target], check=False)  # noqa: S603
    opts = f"uid={uid},gid={uid},umask=077" + ("" if write else ",ro")
    done = subprocess.run(["/bin/mount", "-t", "drvfs", win_path, target, "-o", opts], capture_output=True, text=True)  # noqa: S603
    if done.returncode != 0:
        die(f"can't mount {win_path}: {done.stderr.strip()}")

CONTROLLERS = ("memory", "pids", "cpu")
NO_V2 = ("WSL's kernel gives no {} controller to cgroup v2. In %UserProfile%\\.wslconfig add "
         "kernelCommandLine = cgroup_no_v1=all under [wsl2], then run: wsl --shutdown")

def cgroup_root(base="/sys/fs/cgroup"):
    """The cgroup v2 hierarchy: /sys/fs/cgroup on current WSL, its unified/ part on a hybrid layout, or our own
    cgroup2 mount when the distribution mounted none."""
    for p in (base, base + "/unified"):
        if os.path.exists(p + "/cgroup.controllers"):
            return p
    own = "/run/pythonator/cgroup"
    os.makedirs(own, exist_ok=True)
    if not os.path.ismount(own):
        done = subprocess.run(["/bin/mount", "-t", "cgroup2", "cgroup2", own], capture_output=True, text=True)  # noqa: S603
        if done.returncode != 0:
            die(f"cgroup v2 isn't available in WSL ({done.stderr.strip()}); update WSL with: wsl --update")
    return own

def delegate(cg):
    """Turn the controllers on for the groups below `cg`. Without this a new group has no memory.max, cpu.max or
    pids.max, and writing them fails with "Permission denied" (what WSL's fresh hierarchy did)."""
    with open(os.path.join(cg, "cgroup.controllers")) as fh:
        have = fh.read().split()
    for c in CONTROLLERS:
        if c not in have:
            die(NO_V2.format(c))
    with open(os.path.join(cg, "cgroup.subtree_control")) as fh:
        on = fh.read().split()
    want = " ".join(f"+{c}" for c in CONTROLLERS if c not in on)
    if not want:
        return
    try:
        with open(os.path.join(cg, "cgroup.subtree_control"), "w") as fh:
            fh.write(want)
    except OSError as e:
        if e.errno != errno.EBUSY:
            raise
        # The top of a cgroup namespace may hold processes (the distribution's init): move them into a leaf, as
        # systemd does with init.scope, then the controllers can be handed down.
        leaf = os.path.join(cg, "init")
        os.makedirs(leaf, exist_ok=True)
        with open(os.path.join(cg, "cgroup.procs")) as fh:
            pids = fh.read().split()
        for pid in pids:
            try:
                with open(os.path.join(leaf, "cgroup.procs"), "w") as out:
                    out.write(pid)
            except OSError:
                continue  # a kernel thread or one that just ended
        with open(os.path.join(cg, "cgroup.subtree_control"), "w") as fh:
            fh.write(want)

def limit(name, memory_mb, cpu, tasks, base="/sys/fs/cgroup"):
    """A cgroup v2 group for the run, under pythonator/ with the controllers delegated. Returns how the limits are
    enforced; if they can't be, the service doesn't run."""
    try:
        root = cgroup_root(base)
        delegate(root)
        parent = os.path.join(root, "pythonator")
        os.makedirs(parent, exist_ok=True)
        delegate(parent)
        cg = os.path.join(parent, name)
        os.makedirs(cg, exist_ok=True)
        values = [("memory.max", f"{memory_mb}M"), ("pids.max", str(tasks)), ("cpu.max", f"{int(cpu * 100000)} 100000")]
        if os.path.exists(os.path.join(cg, "memory.swap.max")):  # absent when the kernel doesn't account swap
            values.append(("memory.swap.max", "0"))
        for f, v in values:
            with open(os.path.join(cg, f), "w") as fh:
                fh.write(v)
        with open(os.path.join(cg, "cgroup.procs"), "w") as fh:
            fh.write(str(os.getpid()))
        return "cgroup"
    except OSError as e:
        # No RLIMIT_AS fallback: it is per process and breaks Go, Java and Node. Refuse instead of running unlimited.
        die(f"the limits couldn't be set (cgroup v2 in WSL: {e.strerror} on {e.filename}); update WSL with: wsl --update")

def run(cfg):
    name = cfg["name"]
    if not name.replace("-", "").isalnum():
        die("bad service name")
    uid = uid_for(name)
    base = f"{ROOT}/{name}"
    mount(cfg["folder"], base + "/app", uid, True)
    binds = []
    for i, (path, write) in enumerate(cfg.get("folders", [])):
        target = f"{base}/data/{i}"
        mount(path, target, uid, write)
        binds += ["--bind" if write else "--ro-bind", target, f"/data/{i}"]
    limit(name, int(cfg["memory_mb"]), float(cfg["cpu"]), int(cfg["tasks"]))
    if cfg.get("net"):
        firewall(uid, [int(p) for p in cfg.get("ports", [])], bool(cfg.get("allow_lan")))
    g = cfg.get("gate")
    sock = start_gate(name, uid, g) if g else ""
    b = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if not cfg.get("net"):
        b.append("--unshare-net")
    b += ["--die-with-parent", "--new-session", "--cap-drop", "ALL", "--hostname", name]
    b += ["--ro-bind", "/usr", "/usr"]
    for d in ("bin", "lib", "lib64", "sbin"):
        b += ["--symlink", f"usr/{d}", f"/{d}"]
    for e in ("/etc/ssl", "/etc/ca-certificates", "/etc/alternatives", "/etc/localtime", "/etc/ld.so.cache"):
        b += ["--ro-bind-try", e, e]
    if cfg.get("net"):
        for e in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            b += ["--ro-bind-try", e, e]
    b += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home", "--dir", "/home/service"]  # noqa: S108
    # The same in-jail launcher as Sandbox mode on Linux: no_new_privs and the seccomp filter, then the program.
    b += ["--dir", "/.pythonator", "--ro-bind", LIB + "/launcher.py", "/.pythonator/launcher.py"]
    jcfg = {"seccomp": True, "block_net": not cfg.get("net") and not sock}
    if sock:
        b += ["--bind", sock, "/.pythonator/gate.sock"]
        jcfg["relay"] = {"out": [int(g.get("port", 3128)), "/.pythonator/gate.sock"]}
    argv = ["/usr/bin/python3", "-I", "-S", "/.pythonator/launcher.py", json.dumps(jcfg, separators=(",", ":")), "--",
            *cfg["argv"]]
    b += ["--bind", base + "/app", "/app", *binds, "--chdir", "/app", "--", *argv]
    env = {k: os.environ[k] for k in cfg.get("env_keys", []) if k in os.environ}
    env.update(HOME="/home/service", PATH="/usr/local/bin:/usr/bin:/bin", LANG="C.UTF-8", TERM="dumb")
    if sock:
        url = f"http://127.0.0.1:{int(g.get('port', 3128))}"
        env.update(HTTP_PROXY=url, HTTPS_PROXY=url, http_proxy=url, https_proxy=url, ALL_PROXY=url, NO_PROXY="")
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    try:
        os.execve("/usr/bin/bwrap", b, env)  # noqa: S606
    except OSError as e:
        die(f"can't start bubblewrap: {e.strerror}")

def main(argv):
    if argv[1:2] == ["--stop"] and len(argv) == 3:
        return stop(argv[2])
    if argv[1:2] == ["--rule"] and len(argv) == 4:
        return set_rule(argv[2], argv[3])
    try:
        run(json.loads(argv[1]))
    except (IndexError, KeyError, ValueError, TypeError) as e:
        die(f"bad plan: {e}")
    return EXIT_JAIL

if __name__ == "__main__":
    sys.exit(main(sys.argv))
