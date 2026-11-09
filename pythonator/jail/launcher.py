"""In-jail launcher. Standard library only; runs as `python -I -S launcher.py <config-json> -- <argv...>`.
It applies the last layers that must be set from inside the jail (no_new_privs, Landlock, seccomp), then replaces itself
with the service's program. If a layer the config asks for can't be applied, it exits with 126 and says why:
it never runs the program with less protection than was planned."""

import ctypes
import json
import os
import sys

PR_SET_NO_NEW_PRIVS = 38
SYS_CREATE, SYS_ADD, SYS_RESTRICT = 444, 445, 446  # x86_64 and aarch64 share these numbers
RULE_PATH_BENEATH = 1

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR, MAKE_REG = 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8
MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM, REFER, TRUNCATE, IOCTL_DEV = (1 << i for i in range(9, 16))

class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]

class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

def die(msg):
    sys.stderr.write("pythonator launcher: " + msg + "\n")
    sys.stderr.flush()
    os._exit(126)

def handled_for(abi):
    h = (1 << 13) - 1  # ABI 1: bits 0..12
    if abi >= 2:
        h |= REFER
    if abi >= 3:
        h |= TRUNCATE
    if abi >= 5:
        h |= IOCTL_DEV
    return h

def apply_landlock(rules, abi):
    libc = ctypes.CDLL(None, use_errno=True)
    handled = handled_for(abi)
    ro = EXECUTE | READ_FILE | READ_DIR
    rw = handled & ~(MAKE_CHAR | MAKE_BLOCK)
    attr = RulesetAttr(handled)
    fd = libc.syscall(SYS_CREATE, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd < 0:
        die("Landlock ruleset failed (errno %d)" % ctypes.get_errno())
    for path, write in rules:
        try:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY)
        except OSError:
            continue  # a mount that isn't there grants nothing
        try:
            pb = PathBeneath(rw if write else ro, pfd)
            if (
                libc.syscall(SYS_ADD, ctypes.c_int(fd), ctypes.c_int(RULE_PATH_BENEATH), ctypes.byref(pb), ctypes.c_uint32(0))
                != 0
            ):
                die("Landlock rule for %s failed (errno %d)" % (path, ctypes.get_errno()))
        finally:
            os.close(pfd)
    if libc.syscall(SYS_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
        die("Landlock restrict failed (errno %d)" % ctypes.get_errno())
    os.close(fd)

# seccomp through libseccomp (what Docker, systemd and Flatpak build their filters with): it knows every
# architecture's numbers and refuses other ABIs. Each call below is a way out of a sandbox or a big kernel surface.
DENIED = (
    "ptrace", "process_vm_readv", "process_vm_writev", "keyctl", "add_key", "request_key", "bpf", "perf_event_open",
    "userfaultfd", "mount", "umount2", "pivot_root", "chroot", "setns", "open_tree", "move_mount", "fsopen",
    "fsconfig", "fsmount", "fspick", "mount_setattr", "kexec_load", "kexec_file_load", "init_module", "finit_module",
    "delete_module", "open_by_handle_at", "swapon", "swapoff", "reboot", "acct", "syslog", "quotactl", "vhangup",
    "lookup_dcookie", "uselib", "iopl", "ioperm",
)  # fmt: skip
# ENOSYS so programs fall back: io_uring opens files and sockets past these checks; clone3's flags can't be read.
UNSUPPORTED = ("io_uring_setup", "io_uring_enter", "io_uring_register", "clone3", "openat2")
NUMBERS = {"fchmodat2": 452, "openat2": 437, "clone3": 435}  # for a libseccomp too old to know the names
ALLOW, MASKED_EQ, GT = 0x7FFF0000, 7, 6
CLONE_NEWUSER, LOW32 = 0x10000000, 0xFFFFFFFF

def errno_(code):
    return 0x00050000 | code

def seccomp_rules(block_net):
    """[(action, syscall, [(arg, op, a, b)])]. Under bubblewrap: its PID namespace ends the tree, so no setsid rule."""
    r: list = [(errno_(1), n, []) for n in DENIED] + [(errno_(38), n, []) for n in UNSUPPORTED]
    r.append((errno_(1), "clone", [(0, MASKED_EQ, CLONE_NEWUSER, CLONE_NEWUSER)]))
    r.append((errno_(1), "unshare", [(0, MASKED_EQ, CLONE_NEWUSER, CLONE_NEWUSER)]))
    r += [(errno_(1), "ioctl", [(1, MASKED_EQ, LOW32, c)]) for c in (0x5412, 0x541C)]  # TIOCSTI, TIOCLINUX
    for name, arg in (("chmod", 1), ("fchmod", 1), ("fchmodat", 2), ("fchmodat2", 2)):  # no setuid, setgid, o+w
        r += [(errno_(0), name, [(arg, MASKED_EQ, b, b)]) for b in (0o4000, 0o2000, 0o0002)]
    for name, flags, mode in (("open", 1, 2), ("openat", 2, 3)):
        r += [
            (errno_(1), name, [(flags, MASKED_EQ, k, k), (mode, MASKED_EQ, b, b)])
            for k in (0o100, 0o20000000)
            for b in (0o4000, 0o2000)
        ]
    for name, mode in (("creat", 1), ("mknod", 1), ("mknodat", 2)):  # creat() is open(O_CREAT) without the flag
        r += [(errno_(1), name, [(mode, MASKED_EQ, b, b)]) for b in (0o4000, 0o2000)]
    if block_net:  # every family but Unix (1) and netlink (16)
        r += [(errno_(13), "socket", [(0, MASKED_EQ, LOW32, f)]) for f in range(2, 16)]
        r.append((errno_(13), "socket", [(0, GT, 16, 0)]))
    return r

def apply_seccomp(block_net):
    class ArgCmp(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]

    lib: ctypes.CDLL | None = None
    for name in ("libseccomp.so.2", "/usr/lib/x86_64-linux-gnu/libseccomp.so.2", "/usr/lib/aarch64-linux-gnu/libseccomp.so.2",
                 "/usr/lib64/libseccomp.so.2", "/usr/lib/libseccomp.so.2", "/lib/x86_64-linux-gnu/libseccomp.so.2"):  # fmt: skip
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        die("libseccomp (the libseccomp2 package) couldn't be loaded")
        return
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgCmp)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(ALLOW)
    if not ctx or lib.seccomp_attr_set(ctx, 2, errno_(38)) != 0:  # foreign-architecture calls: ENOSYS
        die("libseccomp couldn't start a filter")
    for action, name, cmps in seccomp_rules(block_net):
        nr = lib.seccomp_syscall_resolve_name(name.encode())
        if nr < 0:
            nr = NUMBERS.get(name, -1)
            if nr < 0:
                continue  # not on this architecture (iopl on arm64)
        arr = (ArgCmp * max(1, len(cmps)))(*(ArgCmp(*c) for c in cmps))
        if lib.seccomp_rule_add_array(ctx, action, nr, len(cmps), arr) != 0:
            die("libseccomp refused the rule for " + name)
    if lib.seccomp_load(ctx) != 0:
        die("couldn't install the seccomp filter")

def relay(cfg):
    """A child that stays in the jail: 127.0.0.1:PORT -> the gate's Unix socket (traffic to allowed hosts), and the
    jail's Unix socket per open port -> 127.0.0.1:PORT (the gate passes connections in). It has no other way out."""
    import socket
    import threading

    def pipe(a, b):
        def copy(x, y):
            try:
                while True:
                    data = x.recv(65536)
                    if not data:
                        break
                    y.sendall(data)
            except OSError:
                pass
            finally:
                for s_ in (x, y):
                    try:
                        s_.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=copy, args=(a, b), daemon=True).start()
        copy(b, a)
        a.close()
        b.close()

    def serve(listener, connect):
        while True:
            c, _ = listener.accept()

            def handle(c=c):
                try:
                    up = connect()
                except OSError:
                    c.close()
                    return
                pipe(c, up)

            threading.Thread(target=handle, daemon=True).start()

    def unix_to(path):
        def f():
            u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            u.connect(path)
            return u

        return f

    servers = []
    if cfg.get("out"):
        port, path = cfg["out"]
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("127.0.0.1", port))
        ls.listen(64)
        servers.append((ls, unix_to(path)))
    for port, path in cfg.get("in", []):
        try:
            os.unlink(path)
        except OSError:
            pass
        ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ls.bind(path)
        ls.listen(64)
        servers.append((ls, lambda port=port: socket.create_connection(("127.0.0.1", port))))
    for ls, connect in servers:
        threading.Thread(target=serve, args=(ls, connect), daemon=True).start()
    threading.Event().wait()

BLACKHOLES = (("169.254.0.0", 16, 2), ("100.100.100.200", 32, 2), ("fd00:ec2::254", 128, 10), ("fe80::", 10, 10))
LAN = (("10.0.0.0", 8, 2), ("172.16.0.0", 12, 2), ("192.168.0.0", 16, 2), ("100.64.0.0", 10, 2), ("fc00::", 7, 10))

def net_guard(routes):
    """Inside pasta's namespace, before bubblewrap: blackhole routes for link-local (cloud metadata) addresses, so a
    service with traffic Anywhere still can't reach them. IPv6 may be off in the namespace; IPv4 must work."""
    import socket
    import struct

    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    for seq, (dst, plen, fam) in enumerate(routes, 1):
        data = socket.inet_pton(socket.AF_INET if fam == 2 else socket.AF_INET6, dst)
        body = struct.pack("BBBBBBBBI", fam, plen, 0, 0, 254, 3, 0, 6, 0) + struct.pack("HH", 4 + len(data), 1) + data
        s.send(struct.pack("IHHII", 16 + len(body), 24, 0x1 | 0x4 | 0x400 | 0x200, seq, 0) + body)  # RTM_NEWROUTE
        err = struct.unpack("i", s.recv(4096)[16:20])[0]
        if err and not (fam == 10 and err in (-95, -97, -101)) and err != -17:  # IPv6 off; already there
            die("couldn't block cloud metadata addresses (netlink error %d)" % err)
    s.close()

def main(argv):
    if argv[1:2] == ["--net-guard"] and "--" in argv:
        i = argv.index("--")
        net_guard(BLACKHOLES + (LAN if "--block-lan" in argv[2:i] else ()))
        try:
            os.execvp(argv[i + 1], argv[i + 1 :])  # noqa: S606
        except (OSError, IndexError) as e:
            die("can't start the jail: %s" % e)
    if len(argv) < 4 or argv[2] != "--":
        die("usage: launcher.py CONFIG -- PROGRAM [ARGS...]")
    cfg = json.loads(argv[1])
    prog = argv[3:]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        die("no_new_privs failed")
    if cfg.get("landlock"):
        apply_landlock(cfg["landlock"], int(cfg.get("abi", 1)))
    if cfg.get("seccomp"):
        apply_seccomp(bool(cfg.get("block_net")))
    if cfg.get("relay"):
        if os.fork() == 0:  # the jail's PID 1 is bwrap's init: the relay ends with the service
            try:
                os.setsid()
                relay(cfg["relay"])
            finally:
                os._exit(0)
    try:
        os.execvp(prog[0], prog)  # noqa: S606 - exec is the point of the launcher
    except OSError as e:
        sys.stderr.write("pythonator launcher: can't start %s: %s\n" % (prog[0], e.strerror))
        os._exit(127)

if __name__ == "__main__":
    main(sys.argv)
