"""macOS Seatbelt profile (sandbox-exec): deny by default, then an allow list. Pure; ported from 0.0.9.
Paths go into the profile as parameters (`-D P0=/path`), never into its text, so no folder name can change a rule.
Seatbelt applies the last matching rule, so the grants come parents first and the blocks come last."""

from __future__ import annotations

READ = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/private/etc",
    "/private/var/db/timezone",
    "/private/var/select",
    "/private/var/db/xcode_select_link",
    "/Library/Frameworks",
    "/Library/Developer",
    "/Library/Java",
    "/Library/Apple",
    "/Library/Python",
    "/opt",
    "/Applications/Xcode.app/Contents/Developer",
    "/dev",
    "/private/preboot/Cryptexes",  # dyld's shared cache on macOS 13+
    "/private/var/db/dyld",
    "/Library/Preferences/Logging",
    "/private/var/db/DetachedSignatures",
)
BASES = ("", "bsd.sb", "system.sb")  # Apple's base profiles, smallest first, used when ours alone isn't enough
SERVICES = (
    "com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
    "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd", "com.apple.FSEvents",
    "com.apple.PowerManagement.control", "com.apple.bsd.dirhelper",
)
NET_SERVICES = (
    "com.apple.SystemConfiguration.configd", "com.apple.SystemConfiguration.DNSConfiguration",
    "com.apple.dnssd.service", "com.apple.networkd", "com.apple.nehelper",
    "com.apple.trustd", "com.apple.trustd.agent", "com.apple.ocspd",  # certificate checks need only trustd
)
# The keychain: an item Pythonator stores trusts the program that stored it, and a service may run that same Python.
KEYCHAIN = ("com.apple.SecurityServer", "com.apple.securityd.xpc", "com.apple.security.keychain-agent")

def profile(
    grants: list[tuple[str, bool]],
    *,
    network: bool,
    ports: list[tuple[int, bool]],
    base: str = "",
    gate: int = 0,
) -> tuple[str, dict[str, str]]:
    """The SBPL text and its parameters. `grants`: (folder, writable); `ports`: (port, reachable from the LAN)."""
    params: dict[str, str] = {}

    def ref(path: str) -> str:
        key = f"P{len(params)}"
        params[key] = path
        return f'(subpath (param "{key}"))'

    def lit(path: str) -> str:
        key = f"P{len(params)}"
        params[key] = path
        return f'(literal (param "{key}"))'

    def names(services: tuple[str, ...]) -> str:
        return " ".join(f'(global-name "{s}")' for s in services)

    system = " ".join(f'(subpath "{p}")' for p in READ)
    lines = [
        "(version 1)",
        "(deny default)",
        *([f'(import "{base}")'] if base else []),
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        '(allow sysctl-write (sysctl-name "kern.grade_cputype"))',
        '(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))',
        "(allow ipc-sysv*)",
        "(allow user-preference-read)",
        "(allow file-read-metadata)",
        "(allow ipc-posix-sem)",
        "(allow ipc-posix-shm*)",
        "(allow pseudo-tty)",
        "(allow system-socket)",
        f"(allow mach-lookup {names(SERVICES)})",
        f"(allow file-read* {system})",
        f"(allow file-map-executable {system})",  # macOS 13+ checks mapping code separately from reading it
        '(deny file-read* file-write* (subpath "/System/Volumes/Data"))',
        # Not /dev/ttys*: those are your Terminal tabs.
        '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") '
        '(literal "/dev/dtracehelper") (literal "/dev/ptmx") (regex #"^/dev/fd/"))',
        '(allow file-ioctl (literal "/dev/dtracehelper") (literal "/dev/ptmx") (literal "/dev/tty"))',
    ]
    for path, write in grants:
        if write:
            lines += [f"(allow file-read* file-write* {ref(path)})", f"(allow file-map-executable {ref(path)})"]
            lines.append(f"(deny file-write-unlink {lit(path)})")  # the folder itself can't be swapped for a link
        else:
            lines += [
                f"(allow file-read* {ref(path)})", f"(allow file-map-executable {ref(path)})",
                f"(deny file-write* {ref(path)})",
            ]
    if network:
        lines += [
            '(allow network-outbound (remote ip "*:*"))',
            '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
            '(allow file-read* (literal "/private/var/run/resolv.conf"))',
            f"(allow mach-lookup {names(NET_SERVICES)})",
            # Programs on this computer's 127.0.0.1 stay out of reach, also with traffic on.
            '(deny network-outbound (remote ip "localhost:*"))',
        ]
    if gate:  # Allowed hosts: the only way out is the egress gate on this one loopback port
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(gate)}"))')
    for port, lan in ports:
        host = "*" if lan else "localhost"
        lines.append(f'(allow network-bind network-inbound (local tcp "{host}:{port}"))')
    lines.append(f"(deny mach-lookup {names(KEYCHAIN)})")  # last, so no base profile can allow it back
    return "\n".join(lines) + "\n", params

def argv(sandbox_exec: str, text: str, params: dict[str, str]) -> list[str]:
    return [sandbox_exec, "-p", text, *(a for k, v in params.items() for a in ("-D", f"{k}={v}"))]
