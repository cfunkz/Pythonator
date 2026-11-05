"""macOS sandbox: Seatbelt (``sandbox-exec``), deny by default, with an allow list. Ported from 0.0.9.

Paths go into the profile as parameters (``-D P0=/path``), so no folder name can change the profile's rules.
"""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path

from .base import Command, Grant, IsolationError, Policy, merge

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
READ = ("/usr", "/bin", "/sbin", "/System", "/private/etc", "/private/var/db/timezone", "/private/var/select",
        "/private/var/db/xcode_select_link", "/Library/Frameworks", "/Library/Developer", "/Library/Java",
        "/Library/Apple", "/Library/Ruby", "/Library/Python", "/opt", "/nix",
        "/Applications/Xcode.app/Contents/Developer", "/dev",
        # dyld's shared cache lives in a cryptex on macOS 13+, reached through the Preboot volume.
        "/private/preboot/Cryptexes", "/private/var/db/dyld", "/Library/Preferences/Logging",
        "/private/var/db/DetachedSignatures")
PROFILES = "/System/Library/Sandbox/Profiles"
BASES = ("bsd.sb", "system.sb")  # Apple's own base profiles, smallest first, used when ours alone isn't enough
SERVICES = ("com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
            "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd",
            "com.apple.FSEvents", "com.apple.PowerManagement.control", "com.apple.bsd.dirhelper")
NET_SERVICES = ("com.apple.SystemConfiguration.configd", "com.apple.SystemConfiguration.DNSConfiguration",
                "com.apple.dnssd.service", "com.apple.networkd", "com.apple.nehelper", "com.apple.SecurityServer",
                "com.apple.trustd", "com.apple.trustd.agent", "com.apple.ocspd")


def profile(policy: Policy, grants: list[Grant], *, base: str = "") -> tuple[str, dict[str, str]]:
    """The SBPL profile and its parameters. Seatbelt applies the last matching rule, so grants go parents first."""
    params: dict[str, str] = {}

    def ref(path: Path, kind: str = "") -> str:
        key = f"P{len(params)}"
        params[key] = str(path)
        return f'({kind or ("subpath" if path.is_dir() else "literal")} (param "{key}"))'

    def names(services: tuple[str, ...]) -> str:
        return " ".join(f'(global-name "{s}")' for s in services)

    system = " ".join(f'(subpath "{p}")' for p in READ)
    lines = [
        "(version 1)", "(deny default)", *([f'(import "{base}")'] if base else []),
        "(allow process-fork)", "(allow process-exec)", "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))", "(allow sysctl-read)",
        '(allow sysctl-write (sysctl-name "kern.grade_cputype"))', "(allow user-preference-read)",
        "(allow file-read-metadata)", "(allow ipc-posix-sem)", "(allow ipc-posix-shm*)", "(allow ipc-sysv*)",
        "(allow pseudo-tty)", "(allow system-socket)", f"(allow mach-lookup {names(SERVICES)})",
        '(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))',
        f"(allow file-read* {system})",
        # macOS 13+ checks mapping code separately from reading it: without this dyld can't map any library.
        f"(allow file-map-executable {system})",
        '(deny file-read* file-write* (subpath "/System/Volumes/Data"))',
        '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") '
        '(literal "/dev/dtracehelper") (literal "/dev/ptmx") (regex #"^/dev/ttys[0-9]+$") (regex #"^/dev/fd/"))',
        '(allow file-ioctl (literal "/dev/dtracehelper") (literal "/dev/ptmx") (literal "/dev/tty") '
        '(regex #"^/dev/ttys[0-9]+$"))',
    ]
    for g in grants:
        if g.write:
            lines += [f"(allow file-read* file-write* {ref(g.path)})", f"(allow file-map-executable {ref(g.path)})"]
            if g.path.is_dir():  # the folder itself can't be swapped for a link
                lines.append(f'(deny file-write-unlink {ref(g.path, "literal")})')
                key = ref(g.path)
                lines += [f"(allow network-bind network-inbound (local unix-socket {key}))",
                          f"(allow network-outbound (remote unix-socket {key}))"]
        else:
            lines += [f"(allow file-read* {ref(g.path)})", f"(allow file-map-executable {ref(g.path)})",
                      f"(deny file-write* {ref(g.path)})"]
    if policy.network:
        if policy.ports:  # only these TCP ports, and only on this computer unless it may serve others
            host = "*" if policy.lan else "localhost"
            lines += [f'(allow network-bind network-inbound (local tcp "{host}:{port}"))' for port in policy.ports]
            lines.append('(allow network-bind network-inbound (local udp "*:*"))')
        else:
            lines += ['(allow network-inbound (local ip "*:*"))', '(allow network-bind (local ip "*:*"))']
        lines += ['(allow network-outbound (remote ip "*:*"))',
                  '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
                  '(allow file-read* (literal "/private/var/run/resolv.conf"))',
                  f"(allow mach-lookup {names(NET_SERVICES)})"]
    return "\n".join(lines) + "\n", params


def _params(params: dict[str, str]) -> list[str]:
    return [arg for key, value in params.items() for arg in ("-D", f"{key}={value}")]


@cache
def working_base() -> str | None:
    """The base profile this macOS needs ("" for none), or None if sandbox-exec refuses every variant."""
    if not Path(SANDBOX_EXEC).exists():
        return None
    home = Path.home().resolve()
    for base in ("", *(b for b in BASES if Path(PROFILES, b).is_file())):
        text, params = profile(Policy(network=True), [Grant(home, True)], base=base)
        try:
            done = subprocess.run([SANDBOX_EXEC, "-p", text, *_params(params), "/usr/bin/true"],
                                  capture_output=True, timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode == 0:
            return base
    return None


def capabilities() -> list[tuple[str, str]]:
    ok = "enforced" if working_base() is not None else "not enforced"
    return [("Files outside its folders", ok), ("Network off", ok), ("Only listed ports", ok),
            ("Only this computer reaches its ports", ok), ("Other processes", ok)]


def wrap(argv: list[str], cwd: Path, env: dict[str, str], policy: Policy, home: Path, tmp: Path) -> Command:
    base = working_base()
    if base is None:
        raise IsolationError("macOS refused the sandbox profile (sandbox-exec). Use Container mode instead.")
    grants = merge([*policy.grants, Grant(home, True), Grant(tmp, True)])
    text, params = profile(policy, grants, base=base)
    env = {**env, "HOME": str(home), "TMPDIR": f"{tmp}/", "PYTHONATOR_SANDBOX": "1"}
    return Command([SANDBOX_EXEC, "-p", text, *_params(params), *argv], cwd, env, describe="Seatbelt")
