"""The sandbox launcher: puts one service in its sandbox and starts it. Standard library only.

:mod:`pythonator.isolation` decides *what* a service may do; this module makes the operating system enforce
it. It runs as a small process of its own, started as ``python -I -S -c <boot> <plan>`` (or
``Pythonator --sandbox-exec <plan>`` in a frozen build), where ``<plan>`` is a JSON object. Ported from
Pythonator 0.0.9, whose CI proved it on Windows, macOS and Linux; keep it standard-library only:

* **Linux**, in up to two stages. Stage 1 (only when bubblewrap is used) ignores SIGTERM and
  execs ``bwrap``, which builds the namespaces and runs stage 2 inside them. bubblewrap stays
  behind as the service's parent; ignoring SIGTERM (inherited across exec) keeps it alive when a
  stop request is sent to the whole process group, so the service gets the signal, shuts down in
  its own time and bubblewrap exits with its exit code. Stage 2 restores the default signal
  handling, applies Landlock (which files may be read, written or run; TCP; signals and abstract
  Unix sockets) and a seccomp filter (network sockets, Unix sockets when bubblewrap isn't there,
  and kernel interfaces a service never needs), then execs the service: same pid, no wrapper.
* **Windows**: creates the service in an AppContainer (a per-service identity with no access to
  your files, other processes or, without capabilities, the network), after granting that
  identity access to the folders it may use. The launcher stays as the service's parent, ignores
  Ctrl+C, shares the service's console (so Pythonator's Ctrl+C reaches it) and exits with its
  exit code. Both run inside the service's job object.

Every step fails closed: if a requested restriction can't be applied, the service doesn't start.
Nothing is imported after the restrictions are in place, so the launcher's own files don't need
to stay readable.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_SANDBOX = 126  # the sandbox couldn't be set up; the service never ran
EXIT_EXEC = 127  # the service's program couldn't be started inside the sandbox
PROBE = "--probe"


class SandboxError(Exception):
    """A restriction couldn't be applied; the service is not started."""
    pass


def _say(message: str) -> None:
    """A line for the service's console (its stderr is Pythonator's pipe)."""
    _write(2, f"pythonator sandbox: {message}\n")


def _write(fd: int, text: str) -> None:
    """Write to stdout / stderr, also from a windowed build where ``sys.stdout`` is None."""
    data = text.encode("utf-8", "replace")
    if sys.platform == "win32":
        _win_write_std(fd, data)
        return
    with contextlib.suppress(OSError):
        os.write(fd, data)


def main(args: list[str] | None = None) -> int:
    """Entry point of the launcher: ``--probe`` reports what this system supports; otherwise apply the JSON plan and
    start the service."""
    args = sys.argv[1:] if args is None else args
    if args[:1] == [PROBE]:
        return _probe()
    try:
        plan = json.loads(args[0])
        if not isinstance(plan, dict):
            raise TypeError
    except (IndexError, ValueError, TypeError):
        _say("missing or invalid sandbox plan")
        return 2
    if sys.platform == "win32":
        return _run_windows(plan)
    return _run_linux(plan)


def _probe() -> int:
    """Report what this system can enforce, as JSON on stdout, by really applying it to this process."""
    result: dict[str, Any] = {}
    if sys.platform == "win32":
        try:
            result["appcontainer"] = windows_probe()
        except (OSError, SandboxError) as exc:
            result["appcontainer"] = False
            result["error"] = str(exc)
    else:
        abi = landlock_abi()
        result["landlock"] = abi
        try:
            if abi:
                apply_landlock([], ["/"], block_tcp=False, scope=False)  # handles every right, allows them all
            result["landlock_ok"] = bool(abi)
        except SandboxError as exc:
            result["landlock_ok"] = False
            result["landlock_error"] = str(exc)
        try:
            apply_seccomp(block_net=False, block_unix=False)
            result["seccomp"] = True
            result["seccomp_lib"] = seccomp_library()
        except SandboxError as exc:
            result["seccomp"] = False
            result["seccomp_error"] = str(exc)
    _write(1, json.dumps(result))
    return 0


# -- Linux ------------------------------------------------------------------------------------------


def _restore_library_path() -> None:
    """A frozen (PyInstaller) launcher changes LD_LIBRARY_PATH for itself; give the service the original."""
    if not getattr(sys, "frozen", False):
        return
    for var in ("LD_LIBRARY_PATH",):
        original = os.environ.pop(var + "_ORIG", None)
        if original is not None:
            os.environ[var] = original
        else:
            os.environ.pop(var, None)


def _run_linux(plan: dict[str, Any]) -> int:
    _restore_library_path()
    bwrap = plan.get("bwrap")
    if bwrap:
        # Stage 1. Ignored signals survive exec; stage 2 turns them back on for the service.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        inner = {k: v for k, v in plan.items() if k not in ("bwrap", "self")}
        argv = [*bwrap, "--", *plan["self"], json.dumps(inner)]
        try:
            os.execv(argv[0], argv)
        except OSError as exc:
            _say(f"could not start bubblewrap ({argv[0]}): {exc.strerror or exc}")
            return EXIT_SANDBOX
    # Stage 2: restore what a normal child gets (Python itself ignores SIGPIPE and SIGXFSZ).
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(sig, signal.SIG_DFL)
    argv = [str(a) for a in plan.get("argv") or []]
    if not argv:
        _say("nothing to run")
        return 2
    try:
        seccomp = plan.get("seccomp")
        if seccomp:
            # Load libseccomp first: once Landlock is on, this process may no longer read the library, nor
            # (in the frozen app) the modules the loader needs, which live inside the app's own executable.
            _load_libseccomp(str(seccomp.get("lib") or ""))
        landlock = plan.get("landlock")
        if landlock:
            apply_landlock([str(p) for p in landlock.get("ro", [])], [str(p) for p in landlock.get("rw", [])],
                           block_tcp=bool(landlock.get("net")), scope=bool(landlock.get("scope")),
                           bind_ports=[int(p) for p in landlock.get("ports") or []])
        if seccomp:
            apply_seccomp(block_net=bool(seccomp.get("net")), block_unix=bool(seccomp.get("unix")),
                          library=str(seccomp.get("lib") or ""))
        drop_capabilities()
    except SandboxError as exc:
        _say(f"{exc}; not starting the service without its sandbox")
        return EXIT_SANDBOX
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        _say(f"could not start {argv[0]}: {exc.strerror or exc}")
    return EXIT_EXEC


_libc_handle: Any = None


def _libc() -> Any:
    global _libc_handle
    if _libc_handle is None:
        import ctypes

        lib = ctypes.CDLL(None, use_errno=True)
        lib.syscall.restype = ctypes.c_long
        lib.prctl.restype = ctypes.c_int
        _libc_handle = lib
    return _libc_handle


def _syscall(nr: int, *args: Any) -> int:
    import ctypes

    # The syscall() wrapper is variadic: pass every integer as a full C long.
    conv = [ctypes.c_long(a) if isinstance(a, int) else a for a in args]
    result = int(_libc().syscall(ctypes.c_long(nr), *conv))
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


def _no_new_privs() -> None:
    import ctypes

    zero = ctypes.c_ulong(0)
    if _libc().prctl(38, ctypes.c_ulong(1), zero, zero, zero) != 0:  # PR_SET_NO_NEW_PRIVS
        err = ctypes.get_errno()
        raise SandboxError(f"could not set no_new_privs: {os.strerror(err)}")


def drop_capabilities() -> None:
    """Give up every capability for good. Only matters when Pythonator runs as root: root's capabilities
    (CAP_SYS_PTRACE, CAP_DAC_OVERRIDE, ...) would otherwise let a service read other programs' /proc files."""
    import ctypes

    try:
        status = Path("/proc/self/status").read_bytes().decode()  # no codec to load: this runs after Landlock
        caps = {ln.split(":")[0]: int(ln.split()[1], 16) for ln in status.splitlines() if ln.startswith("Cap")}
    except (OSError, ValueError, IndexError):
        caps = {"CapBnd": -1}
    if not any(caps.values()):
        return
    libc = _libc()
    for cap in range(64):  # PR_CAPBSET_DROP; EINVAL past the last capability this kernel knows
        failed = libc.prctl(24, ctypes.c_ulong(cap), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0
        if failed and ctypes.get_errno() == errno.EINVAL:
            break
    libc.prctl(47, ctypes.c_ulong(4), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))  # ambient: clear all

    class Header(ctypes.Structure):
        _fields_ = (("version", ctypes.c_uint32), ("pid", ctypes.c_int))

    class Data(ctypes.Structure):
        _fields_ = (("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32))

    header, data = Header(0x20080522, 0), (Data * 2)()  # _LINUX_CAPABILITY_VERSION_3
    if libc.capset(ctypes.byref(header), data) != 0:
        raise SandboxError(f"could not drop capabilities: {os.strerror(ctypes.get_errno())}")


# Landlock (Linux 5.13+): unprivileged, stackable access control for files, TCP and IPC scopes.
_SYS_LANDLOCK_CREATE_RULESET = 444  # the same number on every architecture
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_RULE_NET_PORT = 2
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REFER = 1 << 13  # ABI 2: rename / link between folders
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_ABI1 = (1 << 13) - 1  # execute ... make_sym
_FS_FILE_RIGHTS = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE  # rights that apply to files
_FS_READ = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1


def landlock_abi() -> int:
    """The Landlock ABI version the kernel offers; 0 if Landlock is missing or turned off."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        return _syscall(_SYS_LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    except OSError:
        return 0


def landlock_rights(abi: int) -> int:
    """Every filesystem right this ABI knows (and so restricts unless a rule allows it)."""
    rights = _FS_ABI1
    if abi >= 2:
        rights |= _FS_REFER
    if abi >= 3:
        rights |= _FS_TRUNCATE
    return rights


def apply_landlock(read: list[str], write: list[str], *, block_tcp: bool, scope: bool,
                   bind_ports: list[int] | None = None) -> None:
    """Restrict this process (and everything it starts) to ``read`` / ``write`` (paths and what is below them).

    ``block_tcp``: no TCP bind or connect (ABI 4+). ``scope``: no signals to, and no abstract Unix
    socket connections with, processes outside this sandbox (ABI 6+). ``bind_ports``: the only TCP ports it
    may listen on (ABI 4+; older kernels can't enforce it and allow any).
    """
    import ctypes

    abi = landlock_abi()
    if abi < 1:
        raise SandboxError("Landlock is not available in this kernel")

    class RulesetAttr(ctypes.Structure):
        _fields_ = (("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                    ("scoped", ctypes.c_uint64))

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))

    class NetPort(ctypes.Structure):
        _fields_ = (("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64))

    handled = landlock_rights(abi)
    attr = RulesetAttr(handled, 0, 0)
    size = 8
    if abi >= 4:
        size = 16
        if block_tcp:
            attr.handled_access_net = _NET_BIND_TCP | _NET_CONNECT_TCP
        elif bind_ports:
            attr.handled_access_net = _NET_BIND_TCP
    if abi >= 6:
        size = 24
        if scope:
            attr.scoped = _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL
    try:
        ruleset = _syscall(_SYS_LANDLOCK_CREATE_RULESET, ctypes.cast(ctypes.byref(attr), ctypes.c_void_p), size, 0)
    except OSError as exc:
        raise SandboxError(f"Landlock refused the ruleset: {exc.strerror}") from exc
    try:
        for paths, rights in ((read, _FS_READ & handled), (write, handled)):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    continue  # doesn't exist here (e.g. /lib32): nothing to allow
                try:
                    allowed = rights
                    if not Path(f"/proc/self/fd/{fd}").is_dir():
                        allowed &= _FS_FILE_RIGHTS
                    rule = PathBeneath(allowed, fd)
                    _syscall(_SYS_LANDLOCK_ADD_RULE, ruleset, _LANDLOCK_RULE_PATH_BENEATH,
                             ctypes.cast(ctypes.byref(rule), ctypes.c_void_p), 0)
                except OSError as exc:
                    raise SandboxError(f"Landlock refused the rule for {path}: {exc.strerror}") from exc
                finally:
                    os.close(fd)
        if abi >= 4 and bind_ports and not block_tcp:
            for port in bind_ports:
                net = NetPort(_NET_BIND_TCP, port)
                try:
                    _syscall(_SYS_LANDLOCK_ADD_RULE, ruleset, _LANDLOCK_RULE_NET_PORT,
                             ctypes.cast(ctypes.byref(net), ctypes.c_void_p), 0)
                except OSError as exc:
                    raise SandboxError(f"Landlock refused the rule for port {port}: {exc.strerror}") from exc
        _no_new_privs()
        try:
            _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)
        except OSError as exc:
            raise SandboxError(f"Landlock could not restrict the process: {exc.strerror}") from exc
    finally:
        os.close(ruleset)


# seccomp, through libseccomp: the library Docker, systemd, Flatpak and Chrome build their filters with. It
# knows every architecture's system call numbers and rejects other ABIs (32-bit calls on a 64-bit kernel).
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_FLTATR_ACT_BADARCH = 2
_SCMP_CMP_GT, _SCMP_CMP_MASKED_EQ = 6, 7
_EPERM, _EACCES, _ENOSYS = 1, 13, 38
_AF_UNIX, _AF_NETLINK = 1, 16
_CLONE_NEWUSER = 0x10000000
_TIOCSTI, _TIOCLINUX = 0x5412, 0x541C
_LOW32 = 0xFFFFFFFF

# Never needed by a service, and each one is either a way out of a sandbox or a large kernel attack surface.
DENIED = (
    "ptrace", "process_vm_readv", "process_vm_writev", "keyctl", "add_key", "request_key", "bpf",
    "perf_event_open", "userfaultfd", "mount", "umount2", "pivot_root", "chroot", "setns", "open_tree",
    "move_mount", "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr", "kexec_load", "kexec_file_load",
    "init_module", "finit_module", "delete_module", "open_by_handle_at", "swapon", "swapoff", "reboot", "acct",
    "syslog", "quotactl", "vhangup", "lookup_dcookie", "uselib", "iopl", "ioperm",
)
# ENOSYS, so programs fall back: io_uring can create sockets and open files past the checks above, and
# clone3's flags can't be inspected (glibc, Go and Rust then use clone, whose flags can).
UNSUPPORTED = ("io_uring_setup", "io_uring_enter", "io_uring_register", "clone3")


def seccomp_arch() -> str:
    """The machine architecture as libseccomp names it."""
    machine = os.uname().machine if hasattr(os, "uname") else ""
    return {"arm64": "aarch64"}.get(machine, machine)


def _errno(code: int) -> int:
    return 0x00050000 | code  # SCMP_ACT_ERRNO


def seccomp_rules(*, block_net: bool, block_unix: bool) -> list[tuple[int, str, list[tuple[int, int, int, int]]]]:
    """``(action, system call, [(argument, comparison, a, b), ...])``: the filter, in libseccomp's terms."""
    flags_arg = 1 if seccomp_arch() == "s390x" else 0  # clone takes its flags second there
    rules: list[tuple[int, str, list[tuple[int, int, int, int]]]] = []
    rules += [(_errno(_EPERM), name, []) for name in DENIED]
    rules += [(_errno(_ENOSYS), name, []) for name in UNSUPPORTED]
    # No new user namespaces: a fresh one hands out every capability inside it, a big kernel attack surface.
    rules.append((_errno(_EPERM), "clone", [(flags_arg, _SCMP_CMP_MASKED_EQ, _CLONE_NEWUSER, _CLONE_NEWUSER)]))
    rules.append((_errno(_EPERM), "unshare", [(0, _SCMP_CMP_MASKED_EQ, _CLONE_NEWUSER, _CLONE_NEWUSER)]))
    # No typing into the terminal it was started from (the kernel only looks at the low 32 bits of cmd).
    rules += [(_errno(_EPERM), "ioctl", [(1, _SCMP_CMP_MASKED_EQ, _LOW32, cmd)]) for cmd in (_TIOCSTI, _TIOCLINUX)]
    if block_unix:
        rules.append((_errno(_EACCES), "socket", [(0, _SCMP_CMP_MASKED_EQ, _LOW32, _AF_UNIX)]))
    if block_net:  # every address family but Unix sockets and netlink (interface lists, no traffic)
        rules += [(_errno(_EACCES), "socket", [(0, _SCMP_CMP_MASKED_EQ, _LOW32, family)])
                  for family in range(_AF_UNIX + 1, _AF_NETLINK)]
        rules.append((_errno(_EACCES), "socket", [(0, _SCMP_CMP_GT, _AF_NETLINK, 0)]))
    return rules


# Where libseccomp usually is. The plan normally carries the path Pythonator itself loaded it from, so this
# list only matters when that path doesn't work (in a frozen app the loader's search can differ).
_SECCOMP_DIRS = ("/usr/lib/{triplet}", "/lib/{triplet}", "/usr/lib64", "/lib64", "/usr/lib", "/lib",
                 "/usr/local/lib")
_TRIPLETS = {"x86_64": "x86_64-linux-gnu", "aarch64": "aarch64-linux-gnu", "armv7l": "arm-linux-gnueabihf",
             "ppc64le": "powerpc64le-linux-gnu", "s390x": "s390x-linux-gnu", "riscv64": "riscv64-linux-gnu",
             "i686": "i386-linux-gnu"}
_libseccomp: Any = None


def _load_libseccomp(hint: str = "") -> Any:
    """libseccomp, from ``hint`` (an absolute path), the loader's search or its usual folders."""
    global _libseccomp
    if _libseccomp is not None:
        return _libseccomp
    import ctypes

    def search() -> Iterator[str]:
        yield hint
        yield "libseccomp.so.2"
        import ctypes.util

        yield ctypes.util.find_library("seccomp") or ""
        triplet = _TRIPLETS.get(os.uname().machine, "")
        yield from (f"{d.format(triplet=triplet)}/libseccomp.so.2" for d in _SECCOMP_DIRS if triplet or "{" not in d)

    errors: list[str] = []
    tried: set[str] = set()
    for name in search():
        if not name or name in tried or (name.startswith("/") and not Path(name).exists()):
            continue
        tried.add(name)
        try:
            _libseccomp = ctypes.CDLL(name, use_errno=True)
            return _libseccomp
        except OSError as exc:
            errors.append(str(exc))
    detail = f" ({errors[0]})" if errors else ""
    raise SandboxError(f"seccomp filters need libseccomp (the libseccomp2 package), which couldn't be loaded{detail}")


def seccomp_library() -> str:
    """The file libseccomp was loaded from in this process ("" if it wasn't)."""
    with contextlib.suppress(OSError):
        for line in Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").splitlines():
            path = line.split(maxsplit=5)[-1] if line.count(" ") >= 5 else ""
            if "/libseccomp.so" in path and path.startswith("/"):
                return path.strip()
    return ""


def apply_seccomp(*, block_net: bool, block_unix: bool, library: str = "") -> None:
    """Install the filter on this process and everything it starts. Raises SandboxError if it can't be."""
    import ctypes

    class ArgCmp(ctypes.Structure):
        _fields_ = (("arg", ctypes.c_uint), ("op", ctypes.c_int), ("datum_a", ctypes.c_uint64),
                    ("datum_b", ctypes.c_uint64))

    lib = _load_libseccomp(library)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = (ctypes.c_uint32,)
    lib.seccomp_release.argtypes = (ctypes.c_void_p,)
    lib.seccomp_load.argtypes = (ctypes.c_void_p,)
    lib.seccomp_attr_set.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32)
    lib.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    lib.seccomp_rule_add_array.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
                                           ctypes.POINTER(ArgCmp))
    ctx = lib.seccomp_init(_SCMP_ACT_ALLOW)
    if not ctx:
        raise SandboxError("libseccomp could not start a filter")
    try:
        if lib.seccomp_attr_set(ctx, _SCMP_FLTATR_ACT_BADARCH, _errno(_ENOSYS)) != 0:
            raise SandboxError("libseccomp refused the filter settings")
        for action, name, cmps in seccomp_rules(block_net=block_net, block_unix=block_unix):
            nr = lib.seccomp_syscall_resolve_name(name.encode())
            if nr < 0:
                continue  # not on this architecture (e.g. iopl on arm64)
            array = (ArgCmp * max(1, len(cmps)))(*(ArgCmp(*c) for c in cmps))
            if lib.seccomp_rule_add_array(ctx, action, nr, len(cmps), array) != 0:
                raise SandboxError(f"libseccomp refused the rule for {name}")
        err = lib.seccomp_load(ctx)  # also sets no_new_privs
        if err != 0:
            raise SandboxError(f"could not install the seccomp filter: {os.strerror(-err)}")
    finally:
        lib.seccomp_release(ctx)


# -- Windows ------------------------------------------------------------------------------------------

# Capability SIDs: internetClient, internetClientServer, privateNetworkClientServer.
NETWORK_CAPABILITIES = ("S-1-15-3-1", "S-1-15-3-2", "S-1-15-3-3")
ALL_APP_PACKAGES = "S-1-15-2-1"
STATUS_ACCESS_DENIED = 0xC0000022  # a process's exit code when Windows wouldn't let it load its own files
_READ_EXECUTE = 0x001200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
_MODIFY = 0x001301BF  # read, write, execute and delete
# Rights that change a folder or its files: write / append data, write extended attributes and attributes,
# delete a child, delete, change permissions, take ownership. Plus GENERIC_ALL / GENERIC_WRITE, which an entry
# can hold unmapped. Any one of them in an allow entry means "can write".
_WRITE_RIGHTS = 0x000D0156
_WRITE_BITS = _WRITE_RIGHTS | 0x10000000 | 0x40000000
_ACE_ALLOW, _ACE_DENY = 0, 1
_INHERIT_ONLY, _INHERITED = 0x08, 0x10


@dataclass(frozen=True)
class Ace:
    """One entry of a folder's permissions: allow (0) or deny (1), its flags, its rights and whose it is."""
    kind: int
    flags: int
    mask: int
    sid: str

    @property
    def applies(self) -> bool:
        """It applies to the folder itself (not only to what is created inside it later)."""
        return not self.flags & _INHERIT_ONLY

    @property
    def explicit(self) -> bool:
        """Set on the folder itself, not inherited from the folder above."""
        return not self.flags & _INHERITED


def plan_access(aces: list[Ace], sid: str, others: list[str], *, write: bool, force: bool = False) -> list[str]:
    """What to change so the AppContainer ``sid`` gets exactly read & write (``write``) or read-only access.

    Returns the steps in order: ``"set"`` replaces every entry of ``sid`` set on the folder with one allow entry
    (a read-only folder loses the write access an earlier start gave it; a writable one loses an earlier deny),
    ``"grant"`` adds an allow entry, ``"deny"`` adds a deny entry for every write right. A deny is needed when
    something else still lets the sandbox write: an entry inherited from the folder above, or one for
    ALL APPLICATION PACKAGES (``others``). Windows checks entries set on a folder before inherited ones, so a
    sub-folder you share read & write still works inside a read-only working folder.
    """
    mine = [a for a in aces if a.sid == sid and a.applies]
    if write:
        if any(a.kind == _ACE_DENY and a.explicit for a in mine):
            return ["set"]  # it was read only before
        if not force and any(a.kind == _ACE_ALLOW and a.mask & _MODIFY == _MODIFY for a in mine):
            return []
        return ["grant"]
    steps: list[str] = []
    if any(a.kind == _ACE_ALLOW and a.explicit and a.mask & _WRITE_BITS for a in mine):
        steps.append("set")  # e.g. the service was set to "read & write" before
    elif force or not any(a.kind == _ACE_ALLOW and a.applies and a.mask & _READ_EXECUTE == _READ_EXECUTE
                          and a.sid in (sid, *others) for a in aces):
        steps.append("grant")
    replaced = "set" in steps
    writers = [a for a in aces if a.kind == _ACE_ALLOW and a.applies and a.mask & _WRITE_BITS
               and a.sid in (sid, *others) and not (replaced and a.sid == sid and a.explicit)]
    denied = not replaced and any(a.kind == _ACE_DENY and a.explicit and a.mask & _WRITE_RIGHTS == _WRITE_RIGHTS
                                  for a in mine)
    if writers and not denied:
        steps.append("deny")
    return steps
def shared_with_sandboxes(aces: list[Ace]) -> bool:
    """Every AppContainer may read and run what is in the folder (ALL APPLICATION PACKAGES may read and execute
    it, as Windows sets up C:\\Program Files), and no entry denies it."""
    if any(a.kind == _ACE_DENY and a.applies and a.sid == ALL_APP_PACKAGES and a.mask & _READ_EXECUTE for a in aces):
        return False
    return any(a.kind == _ACE_ALLOW and a.applies and a.sid == ALL_APP_PACKAGES
               and a.mask & _READ_EXECUTE == _READ_EXECUTE for a in aces)


_HRESULT_ALREADY_EXISTS = 0x800700B7


def container_name(service_id: str) -> str:
    """The AppContainer profile of one service. Only letters, digits, '.' and '-' are allowed."""
    clean = "".join(c if c.isascii() and (c.isalnum() or c in ".-") else "-" for c in service_id)
    return f"Pythonator.{clean}"[:64]


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as w

    def _win_write_std(fd: int, data: bytes) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = w.HANDLE
        handle = k32.GetStdHandle(-11 if fd == 1 else -12)  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        if handle and handle != w.HANDLE(-1).value:
            written = w.DWORD()
            k32.WriteFile(w.HANDLE(handle), data, len(data), ctypes.byref(written), None)

    class _Win:
        """The Windows API calls the AppContainer launcher needs, with their signatures."""

        def __init__(self) -> None:
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
            self.uenv = ctypes.WinDLL("userenv", use_last_error=True)
            self.ole = ctypes.WinDLL("ole32", use_last_error=True)
            vp, h, b, d = ctypes.c_void_p, w.HANDLE, w.BOOL, w.DWORD
            hr = ctypes.c_long
            for lib, name, res, args in (
                (self.uenv, "CreateAppContainerProfile", hr,
                 (w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, vp, d, ctypes.POINTER(vp))),
                (self.uenv, "DeriveAppContainerSidFromAppContainerName", hr, (w.LPCWSTR, ctypes.POINTER(vp))),
                (self.uenv, "DeleteAppContainerProfile", hr, (w.LPCWSTR,)),
                (self.uenv, "GetAppContainerFolderPath", hr, (w.LPCWSTR, ctypes.POINTER(w.LPWSTR))),
                (self.ole, "CoTaskMemFree", None, (vp,)),
                (self.adv, "FreeSid", vp, (vp,)),
                (self.adv, "ConvertSidToStringSidW", b, (vp, ctypes.POINTER(w.LPWSTR))),
                (self.adv, "ConvertStringSidToSidW", b, (w.LPCWSTR, ctypes.POINTER(vp))),
                (self.adv, "EqualSid", b, (vp, vp)),
                (self.adv, "GetNamedSecurityInfoW", d, (w.LPCWSTR, ctypes.c_int, d, vp, vp, ctypes.POINTER(vp), vp,
                                                        ctypes.POINTER(vp))),
                (self.adv, "SetNamedSecurityInfoW", d, (w.LPWSTR, ctypes.c_int, d, vp, vp, vp, vp)),
                (self.adv, "SetEntriesInAclW", d, (w.ULONG, vp, vp, ctypes.POINTER(vp))),
                (self.adv, "GetAclInformation", b, (vp, vp, d, ctypes.c_int)),
                (self.adv, "GetAce", b, (vp, d, ctypes.POINTER(vp))),
                (self.k32, "LocalFree", vp, (vp,)),
                (self.k32, "CloseHandle", b, (h,)),
                (self.k32, "GetStdHandle", h, (d,)),
                (self.k32, "SetHandleInformation", b, (h, d, d)),
                (self.k32, "InitializeProcThreadAttributeList", b, (vp, d, d, ctypes.POINTER(ctypes.c_size_t))),
                (self.k32, "UpdateProcThreadAttribute", b, (vp, d, ctypes.c_size_t, vp, ctypes.c_size_t, vp, vp)),
                (self.k32, "DeleteProcThreadAttributeList", None, (vp,)),
                (self.k32, "CreateProcessW", b, (w.LPCWSTR, w.LPWSTR, vp, vp, b, d, vp, w.LPCWSTR, vp, vp)),
                (self.k32, "ResumeThread", d, (h,)),
                (self.k32, "WaitForSingleObject", d, (h, d)),
                (self.k32, "GetExitCodeProcess", b, (h, ctypes.POINTER(d))),
                (self.k32, "TerminateProcess", b, (h, w.UINT)),
                (self.k32, "FreeConsole", b, ()),
                (self.k32, "AttachConsole", b, (d,)),
                (self.k32, "SetConsoleCtrlHandler", b, (vp, b)),
                (self.k32, "ExitProcess", None, (w.UINT,)),
                (self.k32, "CreateMutexW", h, (vp, b, w.LPCWSTR)),
                (self.k32, "ReleaseMutex", b, (h,)),
            ):
                fn = getattr(lib, name)
                fn.restype, fn.argtypes = res, args

        def error(self, what: str) -> SandboxError:
            code = ctypes.get_last_error()
            return SandboxError(f"{what} failed: {_message(code)} ({code})")

        # -- SIDs ---------------------------------------------------------------------------------

        def profile_sid(self, name: str, *, create: bool = True) -> Any:
            """The AppContainer SID of profile ``name`` (created if needed). Free with FreeSid."""
            sid = ctypes.c_void_p()
            if create:
                hr = self.uenv.CreateAppContainerProfile(name, name, "A Pythonator service sandbox", None, 0,
                                                         ctypes.byref(sid))
                if hr >= 0:
                    return sid
                if hr & 0xFFFFFFFF != _HRESULT_ALREADY_EXISTS:
                    raise SandboxError(f"could not create the AppContainer profile {name} (0x{hr & 0xFFFFFFFF:08X})")
            hr = self.uenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
            if hr < 0:
                raise SandboxError(f"could not find the AppContainer profile {name} (0x{hr & 0xFFFFFFFF:08X})")
            return sid

        def sid_string(self, sid: Any) -> str:
            text = w.LPWSTR()
            if not self.adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise self.error("ConvertSidToStringSid")
            try:
                return str(text.value)
            finally:
                self.k32.LocalFree(text)

        def string_sid(self, text: str) -> Any:
            """A SID from its string form. Free with LocalFree."""
            sid = ctypes.c_void_p()
            if not self.adv.ConvertStringSidToSidW(text, ctypes.byref(sid)):
                raise self.error(f"ConvertStringSidToSid({text})")
            return sid

        def folder(self, sid_text: str) -> str:
            path = w.LPWSTR()
            hr = self.uenv.GetAppContainerFolderPath(sid_text, ctypes.byref(path))
            if hr < 0:
                raise SandboxError(f"could not find the AppContainer folder (0x{hr & 0xFFFFFFFF:08X})")
            try:
                return str(path.value)
            finally:
                self.ole.CoTaskMemFree(path)

        # -- ACLs ----------------------------------------------------------------------------------

        def acl_sids(self, path: str, *, inherited: bool = False) -> list[str]:
            """The SIDs with an allow or deny entry on ``path``: only entries set on it, unless ``inherited``."""
            dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
            err = self.adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
            if err:
                raise SandboxError(f"could not read the permissions of {path}: {_message(err)}")
            try:
                if not dacl.value:
                    return []

                class AclSize(ctypes.Structure):
                    _fields_ = (("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD), ("AclBytesFree", w.DWORD))

                info = AclSize()
                if not self.adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
                    raise self.error("GetAclInformation")
                out: list[str] = []
                for i in range(info.AceCount):
                    ace = ctypes.c_void_p()
                    if not self.adv.GetAce(dacl, i, ctypes.byref(ace)) or not ace.value:
                        continue
                    ace_type = ctypes.c_ubyte.from_address(ace.value).value
                    flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                    if ace_type not in (0, 1) or (flags & 0x10 and not inherited):  # allow / deny; INHERITED_ACE
                        continue
                    text = self.sid_string(ctypes.c_void_p(ace.value + 8))
                    if text not in out:
                        out.append(text)
                return out
            finally:
                self.k32.LocalFree(sd)

        def revoke(self, path: str, sid_text: str) -> None:
            """Remove every entry for ``sid_text`` set on ``path``; Windows updates what its contents inherited."""
            sid = self.string_sid(sid_text)
            try:
                self._set_entry(path, sid, 0, mode=4, inheritance=0)  # REVOKE_ACCESS
            finally:
                self.k32.LocalFree(sid)

        def grant(self, path: str, sid: Any, mask: int, *, folder: bool) -> None:
            # GRANT_ACCESS; SUB_CONTAINERS_AND_OBJECTS_INHERIT for folders
            self._set_entry(path, sid, mask, mode=1, inheritance=3 if folder else 0)

        def set_only(self, path: str, sid: Any, mask: int, *, folder: bool) -> None:
            """SET_ACCESS: ``sid``'s entries set on ``path`` (allow and deny) are replaced by this one."""
            self._set_entry(path, sid, mask, mode=2, inheritance=3 if folder else 0)

        def deny(self, path: str, sid: Any, mask: int, *, folder: bool) -> None:
            """DENY_ACCESS: a deny entry, which Windows checks before the allow entries inherited from above."""
            self._set_entry(path, sid, mask, mode=3, inheritance=3 if folder else 0)

        def entries(self, path: str) -> list[Ace]:
            """Every allow and deny entry of ``path`` (set on it or inherited)."""
            dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
            err = self.adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
            if err:
                raise SandboxError(f"could not read the permissions of {path}: {_message(err)}")
            try:
                if not dacl.value:
                    return []

                class AclSize(ctypes.Structure):
                    _fields_ = (("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD), ("AclBytesFree", w.DWORD))

                info = AclSize()
                if not self.adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
                    raise self.error("GetAclInformation")
                out: list[Ace] = []
                for i in range(info.AceCount):
                    ace = ctypes.c_void_p()
                    if not self.adv.GetAce(dacl, i, ctypes.byref(ace)) or not ace.value:
                        continue
                    kind = ctypes.c_ubyte.from_address(ace.value).value
                    if kind not in (_ACE_ALLOW, _ACE_DENY):
                        continue
                    out.append(Ace(kind, ctypes.c_ubyte.from_address(ace.value + 1).value,
                                   w.DWORD.from_address(ace.value + 4).value,
                                   self.sid_string(ctypes.c_void_p(ace.value + 8))))
                return out
            finally:
                self.k32.LocalFree(sd)

        def _set_entry(self, path: str, sid: Any, mask: int, *, mode: int, inheritance: int) -> None:

            class Trustee(ctypes.Structure):
                _fields_ = (("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                            ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int),
                            ("ptstrName", ctypes.c_void_p))

            class ExplicitAccess(ctypes.Structure):
                _fields_ = (("grfAccessPermissions", w.DWORD), ("grfAccessMode", ctypes.c_int),
                            ("grfInheritance", w.DWORD), ("Trustee", Trustee))

            # TRUSTEE_IS_SID, TRUSTEE_IS_WELL_KNOWN_GROUP
            entry = ExplicitAccess(mask, mode, inheritance, Trustee(None, 0, 0, 5, sid.value))
            dacl, sd, new = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            err = self.adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
            if err:
                raise SandboxError(f"could not read the permissions of {path}: {_message(err)}")
            try:
                err = self.adv.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new))
                if err:
                    raise SandboxError(f"could not build the permissions for {path}: {_message(err)}")
                try:
                    buf = ctypes.create_unicode_buffer(path)
                    err = self.adv.SetNamedSecurityInfoW(buf, 1, 4, None, None, new, None)
                    if err:
                        raise SandboxError(f"could not change the permissions of {path}: {_message(err)}")
                finally:
                    self.k32.LocalFree(new)
            finally:
                self.k32.LocalFree(sd)

        # -- processes ------------------------------------------------------------------------------

        def spawn(self, cmdline: str, sid: Any, capabilities: list[Any]) -> tuple[tuple[Any, Any], int]:
            """Create ``cmdline`` suspended in the AppContainer: ``((process handle, thread handle), pid)``."""

            class SidAndAttributes(ctypes.Structure):
                _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", w.DWORD))

            class SecurityCapabilities(ctypes.Structure):
                _fields_ = (("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.POINTER(SidAndAttributes)),
                            ("CapabilityCount", w.DWORD), ("Reserved", w.DWORD))

            class StartupInfo(ctypes.Structure):
                _fields_ = (("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                            ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                            ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                            ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                            ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                            ("hStdError", w.HANDLE))

            class StartupInfoEx(ctypes.Structure):
                _fields_ = (("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p))

            class ProcessInformation(ctypes.Structure):
                _fields_ = (("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD),
                            ("dwThreadId", w.DWORD))

            caps = (SidAndAttributes * max(1, len(capabilities)))(
                *(SidAndAttributes(c.value, 0x4) for c in capabilities))  # SE_GROUP_ENABLED
            security = SecurityCapabilities(sid.value, None, len(capabilities), 0)
            if capabilities:
                security.Capabilities = ctypes.cast(caps, ctypes.POINTER(SidAndAttributes))
            std: list[int] = []
            for which in (-10, -11, -12):  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
                handle = self.k32.GetStdHandle(which & 0xFFFFFFFF)
                std.append(handle or 0)
            inherit = [h for i, h in enumerate(std) if h and h != w.HANDLE(-1).value and h not in std[:i]]
            for handle in inherit:
                self.k32.SetHandleInformation(handle, 1, 1)  # HANDLE_FLAG_INHERIT
            size = ctypes.c_size_t()
            count = 2 if inherit else 1
            self.k32.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
            attrs = (ctypes.c_byte * size.value)()
            if not self.k32.InitializeProcThreadAttributeList(attrs, count, 0, ctypes.byref(size)):
                raise self.error("InitializeProcThreadAttributeList")
            handles = (w.HANDLE * max(1, len(inherit)))(*inherit)
            try:
                # PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES: run in the AppContainer
                if not self.k32.UpdateProcThreadAttribute(attrs, 0, 0x00020009, ctypes.byref(security),
                                                          ctypes.sizeof(security), None, None):
                    raise self.error("UpdateProcThreadAttribute(security capabilities)")
                # PROC_THREAD_ATTRIBUTE_HANDLE_LIST: inherit the standard handles and nothing else
                if inherit and not self.k32.UpdateProcThreadAttribute(
                        attrs, 0, 0x00020002, handles, ctypes.sizeof(w.HANDLE) * len(inherit), None, None):
                    raise self.error("UpdateProcThreadAttribute(handle list)")
                info = StartupInfoEx()
                info.StartupInfo.cb = ctypes.sizeof(StartupInfoEx)
                info.StartupInfo.dwFlags = 0x00000100 | 0x00000080  # STARTF_USESTDHANDLES | STARTF_FORCEOFFFEEDBACK:
                # no busy "app starting" pointer for the user each time a service starts
                info.StartupInfo.hStdInput, info.StartupInfo.hStdOutput, info.StartupInfo.hStdError = std
                info.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
                proc = ProcessInformation()
                command = ctypes.create_unicode_buffer(cmdline)
                # EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW (a console of its own, no window)
                flags = 0x00080000 | 0x00000004 | 0x08000000
                if not self.k32.CreateProcessW(None, command, None, None, bool(inherit), flags, None, None,
                                               ctypes.byref(info), ctypes.byref(proc)):
                    raise self.error("CreateProcess")
                return (proc.hProcess, proc.hThread), int(proc.dwProcessId)
            finally:
                self.k32.DeleteProcThreadAttributeList(attrs)

    def _message(code: int) -> str:
        return str(ctypes.FormatError(code)).strip()  # type: ignore[attr-defined]

    def windows_profile(name: str) -> tuple[str, str]:
        """Create (or find) AppContainer profile ``name``: ``(SID string, its private folder)``."""
        api = _Win()
        sid = api.profile_sid(name)
        try:
            text = api.sid_string(sid)
            return text, api.folder(text)
        finally:
            api.adv.FreeSid(sid)

    def windows_delete_profile(name: str) -> None:
        _Win().uenv.DeleteAppContainerProfile(name)

    def windows_profile_sid(name: str) -> str:
        """The SID of AppContainer profile ``name`` (it follows from the name; nothing is created)."""
        api = _Win()
        sid = api.profile_sid(name, create=False)
        try:
            return api.sid_string(sid)
        finally:
            api.adv.FreeSid(sid)

    def windows_acl_sids(path: str, *, inherited: bool = False) -> list[str]:
        return _Win().acl_sids(path, inherited=inherited)

    def windows_revoke(path: str, sids: Iterable[str]) -> list[str]:
        """Take the entries for ``sids`` off ``path`` (and off what inherited them). Returns the SIDs removed."""
        api = _Win()
        with _grant_lock(api):  # never at the same time as a launcher granting: see _grant_lock
            present = set(api.acl_sids(path))
            removed = []
            for sid in sids:
                if sid in present and sid not in removed:
                    api.revoke(path, sid)
                    removed.append(sid)
            return removed

    def windows_shared_with_sandboxes(path: str) -> bool:
        """Whether every sandbox may read ``path`` already (see :func:`shared_with_sandboxes`)."""
        return shared_with_sandboxes(_Win().entries(path))

    def windows_probe() -> bool:
        """Run ``cmd /c exit 7`` in a throwaway AppContainer: True if it ran there and exited with 7."""
        api = _Win()
        name = "Pythonator.Probe"
        sid = api.profile_sid(name)
        try:
            comspec = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "cmd.exe"))
            (process, thread), _pid = api.spawn(f'"{comspec}" /d /c exit 7', sid, [])
            try:
                api.k32.ResumeThread(thread)
                if api.k32.WaitForSingleObject(process, 10_000) != 0:
                    api.k32.TerminateProcess(process, 1)
                    return False
                code = w.DWORD()
                api.k32.GetExitCodeProcess(process, ctypes.byref(code))
                return code.value == 7
            finally:
                api.k32.CloseHandle(thread)
                api.k32.CloseHandle(process)
        finally:
            api.adv.FreeSid(sid)
            api.uenv.DeleteAppContainerProfile(name)

    def _run_windows(plan: dict[str, Any]) -> int:
        container = plan.get("appcontainer")
        if not isinstance(container, dict):
            _say("no AppContainer in the plan")
            return 2
        try:
            api = _Win()
            sid = api.profile_sid(str(container["name"]))
        except (OSError, SandboxError, KeyError) as exc:
            _say(f"{exc}; not starting the service without its sandbox")
            return EXIT_SANDBOX
        # Share the service's console, so the Ctrl+C Pythonator sends to this process reaches the service;
        # the launcher itself ignores it and waits for the service to finish.
        handler_type = ctypes.WINFUNCTYPE(w.BOOL, w.DWORD)  # type: ignore[attr-defined]
        ignore = handler_type(lambda event: event in (0, 1))  # CTRL_C_EVENT, CTRL_BREAK_EVENT
        owned: list[Any] = []  # LocalFree
        try:
            others = [api.string_sid(ALL_APP_PACKAGES)]
            owned.extend(others)
            capabilities = [api.string_sid(c) for c in NETWORK_CAPABILITIES] if container.get("network") else []
            owned.extend(capabilities)
            grants = [(str(p), bool(wr)) for p, wr in container.get("grants", [])]
            _grant_all(api, grants, sid, others, force=False)
            if getattr(sys, "frozen", False):
                # PyInstaller's bootloader points the DLL search at Pythonator's own files, and child processes
                # inherit that (see PyInstaller's "common issues"): the service must find its own DLLs instead.
                ctypes.WinDLL("kernel32", use_last_error=True).SetDllDirectoryW(None)
            api.k32.SetConsoleCtrlHandler(ignore, True)
            # Python's own Ctrl+C handling too: otherwise the Ctrl+C meant for the service is remembered here
            # and raised as KeyboardInterrupt once the wait returns, and the launcher exits with
            # STATUS_CONTROL_C_EXIT (0xC000013A) instead of the service's own exit code.
            for name in ("SIGINT", "SIGBREAK"):
                with contextlib.suppress(AttributeError, OSError, ValueError):
                    signal.signal(getattr(signal, name), signal.SIG_IGN)
            started = time.monotonic()
            code = _supervise(api, api.spawn(str(container["cmdline"]), sid, capabilities))
            if code == STATUS_ACCESS_DENIED and time.monotonic() - started < 10:
                # Its folders' permissions are incomplete (an earlier version could leave them so when services
                # started together): set them again, whatever the folder itself says, and try once more.
                _say("the service couldn't open its own files; repairing its sandbox's permissions and retrying")
                _grant_all(api, grants, sid, others, force=True)
                code = _supervise(api, api.spawn(str(container["cmdline"]), sid, capabilities))
        except (OSError, SandboxError, KeyError) as exc:
            _say(f"{exc}; not starting the service without its sandbox")
            return EXIT_SANDBOX
        finally:
            for item in owned:
                api.k32.LocalFree(item)
            api.adv.FreeSid(sid)
        api.k32.ExitProcess(code)
        return code  # not reached

    def _supervise(api: _Win, started: tuple[tuple[Any, Any], int]) -> int:
        """Run a suspended service to its end and return its exit code."""
        (process, thread), pid = started
        # The console of a process made with CREATE_NO_WINDOW only exists once it runs, so attach after resuming
        # it (a suspended process has none yet: Ctrl+C then reached nobody and every stop became a kill).
        api.k32.ResumeThread(thread)
        api.k32.CloseHandle(thread)
        api.k32.FreeConsole()
        for _attempt in range(500):  # up to ~5 s; normally the first or second try
            if api.k32.AttachConsole(pid):
                # Ignore Ctrl+C on the service's console too, so the Ctrl+C meant for the service can't end
                # the launcher (Pythonator would then see STATUS_CONTROL_C_EXIT, not the service's exit code).
                api.k32.SetConsoleCtrlHandler(None, True)
                break
            if api.k32.WaitForSingleObject(process, 0) == 0:
                break
            time.sleep(0.01)
        api.k32.WaitForSingleObject(process, 0xFFFFFFFF)  # INFINITE
        code = w.DWORD()
        api.k32.GetExitCodeProcess(process, ctypes.byref(code))
        api.k32.CloseHandle(process)
        api.k32.SetConsoleCtrlHandler(None, False)  # inherited: a retried service must get Ctrl+C again
        return int(code.value)

    def _grant_all(api: _Win, grants: list[tuple[str, bool]], sid: Any, others: list[Any], *, force: bool) -> None:
        with _grant_lock(api):
            for path, write in grants:
                _grant(api, path, sid, others, write=write, force=force)

    @contextlib.contextmanager
    def _grant_lock(api: _Win) -> Iterator[None]:
        """One service at a time changes permissions. Services often share folders (the Python install), and
        two launchers rewriting the same folder's permissions at once can each undo the other's change, so
        one of them then fails to start with "access denied" (0xC0000022) although its own check passed."""
        mutex = api.k32.CreateMutexW(None, False, "Local\\Pythonator.SandboxPermissions")
        if not mutex:
            yield
            return
        try:
            # WAIT_OBJECT_0 or WAIT_ABANDONED (a launcher was killed while it held it): ours now either way.
            # After two minutes go ahead anyway: worst case is the old race, not a service that never starts.
            got = api.k32.WaitForSingleObject(mutex, 120_000) in (0, 0x80)
            try:
                yield
            finally:
                if got:
                    api.k32.ReleaseMutex(mutex)
        finally:
            api.k32.CloseHandle(mutex)

    def _grant(api: _Win, path: str, sid: Any, others: list[Any], *, write: bool, force: bool = False) -> None:
        """Let the AppContainer use ``path`` (unless it already can). A failure only means less access."""
        if not Path(path).exists():
            return
        mask = _MODIFY if write else _READ_EXECUTE
        folder = Path(path).is_dir()
        try:
            steps = plan_access(api.entries(path), api.sid_string(sid), [api.sid_string(o) for o in others],
                                write=write, force=force)
        except SandboxError as exc:
            if write:
                _say(f"warning: {exc}")
                steps = ["grant"]
            else:  # can't tell whether the sandbox could write here: never start it read & write by mistake
                raise
        if steps and folder:
            _say(f"giving this service's sandbox {'read & write' if write else 'read-only'} access to {path} "
                 "(first start only, or after you change it; a big folder takes a moment)")
        for step in steps:
            try:
                if step == "set":
                    api.set_only(path, sid, mask, folder=folder)
                elif step == "grant":
                    api.grant(path, sid, mask, folder=folder)
                else:
                    api.deny(path, sid, _WRITE_RIGHTS, folder=folder)
            except SandboxError as exc:
                if write or step == "grant":
                    _say(f"warning: {exc}")  # less access than asked for: the service may fail, but safely
                else:  # the folder would stay writable although it was set to read only: don't start
                    raise SandboxError(f"could not make {path} read only for the sandbox: {exc}") from exc
else:
    def _win_write_std(fd: int, data: bytes) -> None:
        return

    def windows_profile(name: str) -> tuple[str, str]:
        raise OSError("AppContainers exist only on Windows")

    def windows_delete_profile(name: str) -> None:
        raise OSError("AppContainers exist only on Windows")

    def windows_profile_sid(name: str) -> str:
        raise OSError("AppContainers exist only on Windows")

    def windows_acl_sids(path: str, *, inherited: bool = False) -> list[str]:
        raise OSError("Windows permissions exist only on Windows")

    def windows_revoke(path: str, sids: Iterable[str]) -> list[str]:
        raise OSError("Windows permissions exist only on Windows")

    def windows_shared_with_sandboxes(path: str) -> bool:
        return True

    def windows_probe() -> bool:
        return False

    def _run_windows(plan: dict[str, Any]) -> int:
        _say("AppContainers exist only on Windows")
        return 2
