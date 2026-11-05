"""Linux sandbox: Landlock and seccomp always, bubblewrap namespaces on top when bubblewrap works here.

* **Landlock** (kernel 5.13+) limits which files may be read, written or run, which TCP ports may be bound
  (6.7+), and signals / abstract Unix sockets to processes outside the sandbox (6.12+).
* **seccomp** (libseccomp) blocks ptrace, bpf, mount, io_uring, new user namespaces and, with the network off,
  network sockets.
* **bubblewrap**, when installed and allowed (stock Ubuntu 24.04 blocks it through AppArmor), adds a mount
  namespace with only the system folders and the service's own folders, a PID namespace, and with the network off
  an empty network namespace.
"""

from __future__ import annotations

import contextlib
import ctypes.util
import json
import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .. import launcher
from .base import Command, Grant, IsolationError, Policy, launcher_argv, launcher_files, merge

SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt", "/nix", "/gnu", "/snap",
          "/var/lib/snapd/snap", "/run/current-system", "/proc", "/sys", "/dev")
DEVICES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/ptmx", "/dev/pts",
           "/dev/shm")
BWRAP = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap", "/run/current-system/sw/bin/bwrap")


@dataclass(frozen=True, slots=True)
class Support:
    landlock: int  # ABI version, 0 = none
    seccomp: str  # path of libseccomp, "" = none
    bwrap: str  # usable bubblewrap, "" = none
    bwrap_problem: str = ""

    @property
    def available(self) -> bool:
        return bool(self.landlock or self.bwrap)

    def capabilities(self) -> list[tuple[str, str]]:
        """``(item, enforced | partial | not enforced)`` for the Isolation tab."""
        def mark(ok: bool, partial: bool = False) -> str:
            return "enforced" if ok else "partial" if partial else "not enforced"
        return [
            ("Files outside its folders", mark(bool(self.landlock or self.bwrap))),
            ("Network off", mark(bool(self.bwrap or self.seccomp), bool(self.landlock >= 4))),
            ("Only listed ports", mark(self.landlock >= 4)),
            ("Only this computer reaches its ports", "not enforced (binds where the program binds)"),
            ("Other processes", mark(bool(self.bwrap) or self.landlock >= 6)),
            ("Kernel attack surface (ptrace, bpf, io_uring...)", mark(bool(self.seccomp))),
        ]


def bwrap_path() -> str:
    """bubblewrap from the system folders only, never from a project's PATH."""
    return next((p for p in BWRAP if os.access(p, os.X_OK)), "")


@cache
def support() -> Support:
    abi = launcher.landlock_abi()
    lib = ""
    name = ctypes.util.find_library("seccomp")
    for candidate in filter(None, (name, "libseccomp.so.2")):
        try:
            ctypes.CDLL(candidate)
            lib = candidate
            break
        except OSError:
            continue
    bwrap, problem = bwrap_path(), ""
    if bwrap:
        try:
            done = subprocess.run([bwrap, "--ro-bind", "/", "/", "--unshare-user", "--unshare-net", "--", "/bin/true"],
                                  capture_output=True, text=True, timeout=10, check=False)
            if done.returncode != 0:
                problem = (done.stderr.strip() or "bubblewrap refused to start").splitlines()[0]
                if "ermission" in problem:
                    problem += " (Ubuntu 24.04+: AppArmor blocks it; see docs/isolation.md)"
                bwrap = ""
        except (OSError, subprocess.SubprocessError) as exc:
            bwrap, problem = "", str(exc)
    return Support(abi, lib, bwrap, problem)


def _system_links() -> list[str]:
    """Folders outside the system folders that /etc files point into (resolv.conf in /run, CA files)."""
    out: list[str] = []
    for name in ("/etc/resolv.conf", "/etc/ssl/certs", "/etc/pki", "/etc/localtime", "/etc/ssl/cert.pem"):
        with contextlib.suppress(OSError):
            target = Path(os.path.realpath(name))
            if target.exists() and target.parts[1:2] and f"/{target.parts[1]}" not in SYSTEM:
                folder = str(target if target.is_dir() else target.parent)
                if folder not in out:
                    out.append(folder)
    return out


def bwrap_args(bwrap: str, *, network: bool, grants: list[Grant], cwd: str, tmp: Path) -> list[str]:
    """The bubblewrap command line: only the system folders and the service's own folders exist inside."""
    args = [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
            "--die-with-parent", "--cap-drop", "ALL"]
    if not network:
        args.append("--unshare-net")
    for folder in SYSTEM:
        if folder in ("/proc", "/dev"):
            continue
        path = Path(folder)
        if path.is_symlink():  # merged /usr: /bin -> usr/bin
            args += ["--symlink", str(path.readlink()), folder]
        elif path.is_dir():
            args += ["--ro-bind", folder, folder]
    args += ["--dev", "/dev", "--proc", "/proc", "--bind", str(tmp), "/tmp"]
    for folder in _system_links():
        args += ["--ro-bind-try", folder, folder]
    for g in merge([*(Grant(f, False) for f in launcher_files()), *grants]):
        args += ["--bind-try" if g.write else "--ro-bind-try", str(g.path), str(g.path)]
    return [*args, "--chdir", cwd]


def wrap(argv: list[str], cwd: Path, env: dict[str, str], policy: Policy, home: Path, tmp: Path) -> Command:
    sup = support()
    if not sup.available:
        raise IsolationError("This Linux can't sandbox: it has neither Landlock (kernel 5.13+) nor a working "
                             f"bubblewrap. {sup.bwrap_problem}".strip())
    if not policy.network and not (sup.bwrap or sup.seccomp):
        raise IsolationError("Can't turn the network off here: install bubblewrap or libseccomp2")
    grants = merge([*policy.grants, Grant(home, True), Grant(tmp, True)])
    plan: dict[str, object] = {"argv": argv}
    if sup.landlock:
        read = [*SYSTEM, *_system_links(), *(str(f) for f in launcher_files()),
                *(str(g.path) for g in grants if not g.write)]
        write = [*DEVICES, *(["/tmp"] if sup.bwrap else []), *(str(g.path) for g in grants if g.write)]
        plan["landlock"] = {"ro": read, "rw": write, "net": not policy.network, "scope": True,
                            "ports": list(policy.ports)}
    if sup.seccomp:
        plan["seccomp"] = {"net": not policy.network, "unix": not sup.bwrap, "lib": sup.seccomp}
    if sup.bwrap:
        plan["bwrap"] = bwrap_args(sup.bwrap, network=policy.network, grants=grants, cwd=str(cwd), tmp=tmp)
        plan["self"] = launcher_argv()
    env = {**env, "HOME": str(home), "TMPDIR": "/tmp" if sup.bwrap else str(tmp), "PYTHONATOR_SANDBOX": "1"}
    layers = ["Landlock" if sup.landlock else "", "seccomp" if sup.seccomp else "", "bubblewrap" if sup.bwrap else ""]
    return Command([*launcher_argv(), json.dumps(plan)], cwd, env, describe=" + ".join(filter(None, layers)))
