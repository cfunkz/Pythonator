# mypy: ignore-errors
"""Windows jail launcher. Standard library only; runs as `python -I -S winjail.py CONFIG`.
Every Windows service (Sandbox and Full access) starts here. The launcher first puts itself in a new job object
with *kill on close* and the service's limits, so everything the service starts is in the job too: Stop ends the
whole tree, however deep, and memory, CPU and process limits are enforced by the kernel. In Sandbox mode it then
starts the program in the service's own AppContainer: an identity with no access to your files, other processes
or (without network capabilities) the network. Before that it grants the AppContainer exactly the folders the
plan lists; the background service has written each grant to the ownership ledger first, and takes it back with
`--revoke` when the service is removed. If a layer can't be applied, it exits 126 and the service never runs.
Ported from the 0.0.9 launcher, which passed on Windows 10 and 11."""

import contextlib
import json
import os
import sys
import time

EXIT_JAIL = 126
NETWORK_CAPABILITIES = ("S-1-15-3-1", "S-1-15-3-2", "S-1-15-3-3")  # internetClient(Server), privateNetwork
ALL_APP_PACKAGES = "S-1-15-2-1"
STATUS_ACCESS_DENIED = 0xC0000022  # a process's exit code when it couldn't load its own files
READ_EXECUTE = 0x001200A9
MODIFY = 0x001301BF
WRITE_RIGHTS = 0x000D0156  # write / append data, attributes, delete child, delete, change permissions, owner
WRITE_BITS = WRITE_RIGHTS | 0x10000000 | 0x40000000  # plus GENERIC_ALL / GENERIC_WRITE held unmapped
ALLOW, DENY = 0, 1
INHERIT_ONLY, INHERITED = 0x08, 0x10

# Python 3.12.4+ gives a folder made with os.mkdir(path, 0o700) an ACL without the AppContainer (the fix for
# CVE-2024-4030), so tempfile, ensurepip and pip fail in the sandbox. Every Python in the sandbox loads this first.
SITECUSTOMIZE = '''\
"""Pythonator: makes Python work in the Windows sandbox."""
import os
import sys

if os.name == "nt" and os.environ.get("PYTHONATOR_SANDBOX") == "1":
    _mkdir = os.mkdir

    def mkdir(path, mode=0o777, **kwargs):
        return _mkdir(path, 0o777 if mode == 0o700 else mode, **kwargs)

    os.mkdir = mkdir

def _next_sitecustomize():
    """The project's own sitecustomize, which this one hides: run it too."""
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or ".") != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

_next_sitecustomize()
'''

class JailError(Exception):
    pass

def say(msg):
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write("pythonator jail: " + msg + "\n")
        sys.stderr.flush()

def plan_access(aces, sid, others, write, force=False):
    """The steps that give AppContainer `sid` exactly read & write (`write`) or read-only access to a folder whose
    entries are `aces` [(kind, flags, mask, sid)]: "set" replaces its own entries, "grant" adds an allow entry,
    "deny" adds a deny entry for every write right (needed when an inherited entry, or one for every AppContainer
    in `others`, still lets it write). Windows checks a folder's own entries before inherited ones."""
    mine = [a for a in aces if a[3] == sid and not a[1] & INHERIT_ONLY]
    if write:
        if any(a[0] == DENY and not a[1] & INHERITED for a in mine):
            return ["set"]
        if not force and any(a[0] == ALLOW and a[2] & MODIFY == MODIFY for a in mine):
            return []
        return ["grant"]
    steps = []
    if any(a[0] == ALLOW and not a[1] & INHERITED and a[2] & WRITE_BITS for a in mine):
        steps.append("set")
    elif force or not any(
        a[0] == ALLOW and not a[1] & INHERIT_ONLY and a[2] & READ_EXECUTE == READ_EXECUTE and a[3] in (sid, *others) for a in aces
    ):
        steps.append("grant")
    replaced = "set" in steps
    writers = [
        a
        for a in aces
        if a[0] == ALLOW
        and not a[1] & INHERIT_ONLY
        and a[2] & WRITE_BITS
        and a[3] in (sid, *others)
        and not (replaced and a[3] == sid and not a[1] & INHERITED)
    ]
    denied = not replaced and any(a[0] == DENY and not a[1] & INHERITED and a[2] & WRITE_RIGHTS == WRITE_RIGHTS for a in mine)
    if writers and not denied:
        steps.append("deny")
    return steps

def env_block(env):
    """A CreateProcess environment block (CREATE_UNICODE_ENVIRONMENT): sorted, case-insensitively, as Windows wants."""
    return "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as w

    class _Basic(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD), ("Affinity", ctypes.c_size_t),
            ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD),
        )

    class _Extended(ctypes.Structure):
        _fields_ = (
            ("Basic", _Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    class _CpuRate(ctypes.Structure):
        _fields_ = (("ControlFlags", w.DWORD), ("CpuRate", w.DWORD))

    class _SidAttr(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", w.DWORD))

    class _SecCaps(ctypes.Structure):
        _fields_ = (
            ("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.POINTER(_SidAttr)),
            ("CapabilityCount", w.DWORD), ("Reserved", w.DWORD),
        )

    class _Startup(ctypes.Structure):
        _fields_ = (
            ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR), ("dwX", w.DWORD),
            ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
            ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD), ("wShowWindow", w.WORD),
            ("cbReserved2", w.WORD), ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
            ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE),
        )

    class _StartupEx(ctypes.Structure):
        _fields_ = (("StartupInfo", _Startup), ("lpAttributeList", ctypes.c_void_p))

    class _ProcInfo(ctypes.Structure):
        _fields_ = (("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD))

    class _AclSize(ctypes.Structure):
        _fields_ = (("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD), ("AclBytesFree", w.DWORD))

    class _Trustee(ctypes.Structure):
        _fields_ = (
            ("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
            ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p),
        )

    class _Explicit(ctypes.Structure):
        _fields_ = (
            ("grfAccessPermissions", w.DWORD), ("grfAccessMode", ctypes.c_int), ("grfInheritance", w.DWORD),
            ("Trustee", _Trustee),
        )

    class Win:
        """The Windows calls the jail needs, with their signatures."""

        def __init__(self):
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
            self.uenv = ctypes.WinDLL("userenv", use_last_error=True)
            self.ole = ctypes.WinDLL("ole32", use_last_error=True)
            vp, h, b, d, hr = ctypes.c_void_p, w.HANDLE, w.BOOL, w.DWORD, ctypes.c_long
            for lib, name, res, args in (
                (self.uenv, "CreateAppContainerProfile", hr, (w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, vp, d, ctypes.POINTER(vp))),
                (self.uenv, "DeriveAppContainerSidFromAppContainerName", hr, (w.LPCWSTR, ctypes.POINTER(vp))),
                (self.uenv, "DeleteAppContainerProfile", hr, (w.LPCWSTR,)),
                (self.uenv, "GetAppContainerFolderPath", hr, (w.LPCWSTR, ctypes.POINTER(w.LPWSTR))),
                (self.ole, "CoTaskMemFree", None, (vp,)),
                (self.adv, "FreeSid", vp, (vp,)),
                (self.adv, "ConvertSidToStringSidW", b, (vp, ctypes.POINTER(w.LPWSTR))),
                (self.adv, "ConvertStringSidToSidW", b, (w.LPCWSTR, ctypes.POINTER(vp))),
                (
                    self.adv, "GetNamedSecurityInfoW", d,
                    (w.LPCWSTR, ctypes.c_int, d, vp, vp, ctypes.POINTER(vp), vp, ctypes.POINTER(vp)),
                ),
                (self.adv, "SetNamedSecurityInfoW", d, (w.LPWSTR, ctypes.c_int, d, vp, vp, vp, vp)),
                (self.adv, "SetEntriesInAclW", d, (w.ULONG, vp, vp, ctypes.POINTER(vp))),
                (self.adv, "GetAclInformation", b, (vp, vp, d, ctypes.c_int)),
                (self.adv, "GetAce", b, (vp, d, ctypes.POINTER(vp))),
                (self.k32, "LocalFree", vp, (vp,)),
                (self.k32, "CloseHandle", b, (h,)),
                (self.k32, "GetStdHandle", h, (d,)),
                (self.k32, "GetCurrentProcess", h, ()),
                (self.k32, "SetHandleInformation", b, (h, d, d)),
                (self.k32, "CreateJobObjectW", h, (vp, w.LPCWSTR)),
                (self.k32, "SetInformationJobObject", b, (h, ctypes.c_int, vp, d)),
                (self.k32, "AssignProcessToJobObject", b, (h, h)),
                (self.k32, "InitializeProcThreadAttributeList", b, (vp, d, d, ctypes.POINTER(ctypes.c_size_t))),
                (self.k32, "UpdateProcThreadAttribute", b, (vp, d, ctypes.c_size_t, vp, ctypes.c_size_t, vp, vp)),
                (self.k32, "DeleteProcThreadAttributeList", None, (vp,)),
                (self.k32, "CreateProcessW", b, (w.LPCWSTR, w.LPWSTR, vp, vp, b, d, vp, w.LPCWSTR, vp, vp)),
                (self.k32, "ResumeThread", d, (h,)),
                (self.k32, "WaitForSingleObject", d, (h, d)),
                (self.k32, "GetExitCodeProcess", b, (h, ctypes.POINTER(d))),
                (self.k32, "TerminateProcess", b, (h, w.UINT)),
                (self.k32, "CreateMutexW", h, (vp, b, w.LPCWSTR)),
                (self.k32, "ReleaseMutex", b, (h,)),
                (self.k32, "SetConsoleCtrlHandler", b, (vp, b)),
            ):
                fn = getattr(lib, name)
                fn.restype, fn.argtypes = res, args

        def fail(self, what):
            code = ctypes.get_last_error()
            return JailError(f"{what} failed: {ctypes.FormatError(code).strip()} ({code})")

        # job object ------------------------------------------------------------------------------------------
        def job(self, memory_mb, cpu, processes):
            """A job with kill-on-close and the limits, holding this process (so every child is in it too)."""
            job = self.k32.CreateJobObjectW(None, None)
            if not job:
                raise self.fail("CreateJobObject")
            info = _Extended()
            # KILL_ON_JOB_CLOSE | JOB_MEMORY | ACTIVE_PROCESS | DIE_ON_UNHANDLED_EXCEPTION (no error dialogs)
            info.Basic.LimitFlags = 0x2000 | (0x200 if memory_mb else 0) | (0x8 if processes else 0) | 0x400
            info.Basic.ActiveProcessLimit = processes + 1 if processes else 0  # +1: this launcher
            info.JobMemoryLimit = memory_mb * 1024 * 1024
            if not self.k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise self.fail("Setting the job limits")
            if cpu:
                rate = min(10000, max(1, int(cpu * 10000 / (os.cpu_count() or 1))))
                ctl = _CpuRate(0x1 | 0x4, rate)  # ENABLE | HARD_CAP, in 1/100 % of the whole computer
                if not self.k32.SetInformationJobObject(job, 15, ctypes.byref(ctl), ctypes.sizeof(ctl)):
                    raise self.fail("Setting the CPU limit")
            if not self.k32.AssignProcessToJobObject(job, self.k32.GetCurrentProcess()):
                raise self.fail("AssignProcessToJobObject")
            return job  # never closed: it closes when this process ends, and takes the tree with it

        # SIDs and profiles --------------------------------------------------------------------------------------
        def profile_sid(self, name):
            sid = ctypes.c_void_p()
            r = self.uenv.CreateAppContainerProfile(name, name, "A Pythonator service sandbox", None, 0, ctypes.byref(sid))
            if r >= 0:
                return sid
            if r & 0xFFFFFFFF != 0x800700B7:  # already exists
                raise JailError(f"could not create the AppContainer profile {name} (0x{r & 0xFFFFFFFF:08X})")
            return self.derive_sid(name)

        def derive_sid(self, name):
            sid = ctypes.c_void_p()
            r = self.uenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
            if r < 0:
                raise JailError(f"could not find the AppContainer profile {name} (0x{r & 0xFFFFFFFF:08X})")
            return sid

        def sid_text(self, sid):
            text = w.LPWSTR()
            if not self.adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise self.fail("ConvertSidToStringSid")
            try:
                return str(text.value)
            finally:
                self.k32.LocalFree(text)

        def text_sid(self, text):
            sid = ctypes.c_void_p()
            if not self.adv.ConvertStringSidToSidW(text, ctypes.byref(sid)):
                raise self.fail(f"ConvertStringSidToSid({text})")
            return sid

        def folder(self, sid_text):
            path = w.LPWSTR()
            r = self.uenv.GetAppContainerFolderPath(sid_text, ctypes.byref(path))
            if r < 0:
                raise JailError(f"could not find the AppContainer folder (0x{r & 0xFFFFFFFF:08X})")
            try:
                return str(path.value)
            finally:
                self.ole.CoTaskMemFree(path)

        # ACLs ---------------------------------------------------------------------------------------------------
        def entries(self, path):
            dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
            err = self.adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
            if err:
                raise JailError(f"could not read the permissions of {path}: {ctypes.FormatError(err).strip()}")
            try:
                if not dacl.value:
                    return []
                info = _AclSize()
                if not self.adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
                    raise self.fail("GetAclInformation")
                out = []
                for i in range(info.AceCount):
                    ace = ctypes.c_void_p()
                    if not self.adv.GetAce(dacl, i, ctypes.byref(ace)) or not ace.value:
                        continue
                    kind = ctypes.c_ubyte.from_address(ace.value).value
                    if kind in (ALLOW, DENY):
                        out.append(
                            (
                                kind, ctypes.c_ubyte.from_address(ace.value + 1).value,
                                w.DWORD.from_address(ace.value + 4).value,
                                self.sid_text(ctypes.c_void_p(ace.value + 8)),
                            )
                        )
                return out
            finally:
                self.k32.LocalFree(sd)

        def set_entry(self, path, sid, mask, mode, folder):
            """mode: 1 GRANT, 2 SET, 3 DENY, 4 REVOKE. Folders pass it on to what's inside."""
            entry = _Explicit(mask, mode, 3 if folder and mode != 4 else 0, _Trustee(None, 0, 0, 5, sid.value))
            dacl, sd, new = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            err = self.adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
            if err:
                raise JailError(f"could not read the permissions of {path}: {ctypes.FormatError(err).strip()}")
            try:
                err = self.adv.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new))
                if err:
                    raise JailError(f"could not build the permissions for {path}: {ctypes.FormatError(err).strip()}")
                try:
                    err = self.adv.SetNamedSecurityInfoW(ctypes.create_unicode_buffer(path), 1, 4, None, None, new, None)
                    if err:
                        raise JailError(f"could not change the permissions of {path}: {ctypes.FormatError(err).strip()}")
                finally:
                    self.k32.LocalFree(new)
            finally:
                self.k32.LocalFree(sd)

        @contextlib.contextmanager
        def acl_lock(self):
            """One launcher at a time changes permissions: two rewriting a shared folder (the Python install) at
            once can undo each other's change."""
            m = self.k32.CreateMutexW(None, False, "Local\\Pythonator.SandboxPermissions")
            if not m:
                yield
                return
            try:
                got = self.k32.WaitForSingleObject(m, 120_000) in (0, 0x80)
                try:
                    yield
                finally:
                    if got:
                        self.k32.ReleaseMutex(m)
            finally:
                self.k32.CloseHandle(m)

        def grant_all(self, grants, sid, force):
            me = self.sid_text(sid)
            with self.acl_lock():
                for path, write in grants:
                    if not os.path.exists(path):
                        continue
                    folder = os.path.isdir(path)
                    try:
                        steps = plan_access(self.entries(path), me, [ALL_APP_PACKAGES], write, force)
                    except JailError as e:
                        if not write:
                            raise  # can't tell whether it could write: never start it read-write by mistake
                        say(f"warning: {e}")
                        steps = ["grant"]
                    if steps and folder:
                        say(f"giving the sandbox {'read & write' if write else 'read-only'} access to {path}")
                    for step in steps:
                        try:
                            if step == "deny":
                                self.set_entry(path, sid, WRITE_RIGHTS, 3, folder)
                            else:
                                self.set_entry(path, sid, MODIFY if write else READ_EXECUTE, 2 if step == "set" else 1, folder)
                        except JailError as e:
                            if step == "deny" or (step == "set" and not write):
                                raise JailError(f"could not make {path} read only for the sandbox: {e}") from e
                            say(f"warning: {e}")

        # processes ----------------------------------------------------------------------------------------------
        def spawn(self, cmdline, env, cwd, sid=None, caps=()):
            """Start `cmdline` suspended (in the AppContainer when `sid` is given): (process, thread, pid)."""
            std = [self.k32.GetStdHandle((-n) & 0xFFFFFFFF) or 0 for n in (10, 11, 12)]
            inherit = [hd for i, hd in enumerate(std) if hd and hd != w.HANDLE(-1).value and hd not in std[:i]]
            for hd in inherit:
                self.k32.SetHandleInformation(hd, 1, 1)
            count = (1 if sid is not None else 0) + (1 if inherit else 0)
            size = ctypes.c_size_t()
            self.k32.InitializeProcThreadAttributeList(None, max(1, count), 0, ctypes.byref(size))
            attrs = (ctypes.c_byte * size.value)()
            if not self.k32.InitializeProcThreadAttributeList(attrs, max(1, count), 0, ctypes.byref(size)):
                raise self.fail("InitializeProcThreadAttributeList")
            handles = (w.HANDLE * max(1, len(inherit)))(*inherit)
            capv = (_SidAttr * max(1, len(caps)))(*(_SidAttr(c.value, 0x4) for c in caps))
            sec = _SecCaps(sid.value if sid is not None else None, None, len(caps), 0)
            if caps:
                sec.Capabilities = ctypes.cast(capv, ctypes.POINTER(_SidAttr))
            try:
                if sid is not None and not self.k32.UpdateProcThreadAttribute(
                    attrs, 0, 0x00020009, ctypes.byref(sec), ctypes.sizeof(sec), None, None
                ):
                    raise self.fail("UpdateProcThreadAttribute(security capabilities)")
                if inherit and not self.k32.UpdateProcThreadAttribute(
                    attrs, 0, 0x00020002, handles, ctypes.sizeof(w.HANDLE) * len(inherit), None, None
                ):
                    raise self.fail("UpdateProcThreadAttribute(handle list)")
                si = _StartupEx()
                si.StartupInfo.cb = ctypes.sizeof(_StartupEx)
                si.StartupInfo.dwFlags = 0x100 | 0x80  # USESTDHANDLES | FORCEOFFFEEDBACK (no busy pointer)
                si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = std
                si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
                pi = _ProcInfo()
                block = ctypes.create_unicode_buffer(env_block(env))
                # EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
                flags = 0x80000 | 0x4 | 0x08000000 | 0x400
                if not self.k32.CreateProcessW(
                    None, ctypes.create_unicode_buffer(cmdline), None, None, bool(inherit), flags, block, cwd,
                    ctypes.byref(si), ctypes.byref(pi),
                ):
                    raise self.fail("CreateProcess")
                return pi.hProcess, pi.hThread, int(pi.dwProcessId)
            finally:
                self.k32.DeleteProcThreadAttributeList(attrs)

        def wait(self, started):
            process, thread, _pid = started
            self.k32.ResumeThread(thread)
            self.k32.CloseHandle(thread)
            self.k32.WaitForSingleObject(process, 0xFFFFFFFF)
            code = w.DWORD()
            self.k32.GetExitCodeProcess(process, ctypes.byref(code))
            self.k32.CloseHandle(process)
            return int(code.value)

def sandbox_env(env, base):
    """The service's home, app data and temp folders inside its AppContainer folder, and the sitecustomize."""
    home = os.path.join(base, "home")
    local = os.path.join(home, "Local")
    roaming = os.path.join(home, "Roaming")
    temp = os.path.join(local, "Temp")
    site = os.path.join(base, "pythonator-site")
    for d in (home, local, roaming, temp, site):
        os.makedirs(d, exist_ok=True)
    sc = os.path.join(site, "sitecustomize.py")
    with contextlib.suppress(OSError):
        if not os.path.isfile(sc) or open(sc, encoding="utf-8").read() != SITECUSTOMIZE:  # noqa: SIM115
            with open(sc, "w", encoding="utf-8") as f:
                f.write(SITECUSTOMIZE)
    out = dict(env)
    out.update(
        USERPROFILE=home, HOME=home, APPDATA=roaming, LOCALAPPDATA=local, TEMP=temp, TMP=temp, PYTHONATOR_SANDBOX="1",
        PYTHONPATH=site,
    )
    # Node: no realpath of module paths, which would stat C:\ and every folder down (an AppContainer may not).
    out["NODE_OPTIONS"] = "--preserve-symlinks --preserve-symlinks-main"
    return out

def run(cfg):
    api = Win()
    try:
        lim = cfg.get("job") or {}
        job = api.job(int(lim.get("memory_mb", 0)), float(lim.get("cpu", 0)), int(lim.get("processes", 0)))
        if cfg.get("box"):  # Sandbox: no clipboard, desktops, global atoms, other windows' handles or display changes
            ui = ctypes.c_uint32(0xFF)  # every JOB_OBJECT_UILIMIT_* flag
            if not api.k32.SetInformationJobObject(job, 4, ctypes.byref(ui), 4):
                raise api.fail("Setting the job's UI restrictions")
    except (OSError, JailError) as e:
        say(f"{e}; not starting the service without its job object")
        return EXIT_JAIL
    env, cwd, cmdline = dict(os.environ), cfg["cwd"], cfg["cmdline"]
    box = cfg.get("box")
    if not box:
        try:
            return api.wait(api.spawn(cmdline, env, cwd))
        except (OSError, JailError) as e:
            say(f"can't start the service: {e}")
            return 127
    owned = []
    try:
        sid = api.profile_sid(box["name"])
        env = sandbox_env(env, api.folder(api.sid_text(sid)))
        caps = [api.text_sid(c) for c in box.get("caps", []) if c in NETWORK_CAPABILITIES]
        owned.extend(caps)
        grants = [(str(p), bool(wr)) for p, wr in box.get("grants", [])]
        api.grant_all(grants, sid, False)
        started = time.monotonic()
        code = api.wait(api.spawn(cmdline, env, cwd, sid, caps))
        if code == STATUS_ACCESS_DENIED and time.monotonic() - started < 10:
            say("the service couldn't open its own files; setting its folders' permissions again and retrying")
            api.grant_all(grants, sid, True)
            code = api.wait(api.spawn(cmdline, env, cwd, sid, caps))
        return code
    except (OSError, JailError, KeyError) as e:
        say(f"{e}; not starting the service without its sandbox")
        return EXIT_JAIL
    finally:
        for c in owned:
            api.k32.LocalFree(c)

def revoke(cfg):
    """Take a removed service's AppContainer off every folder it was given, and delete its profile."""
    api = Win()
    name = cfg["name"]
    try:
        sid = api.derive_sid(name)
    except JailError as e:
        say(str(e))
        return 1
    me = api.sid_text(sid)
    failed = 0
    with api.acl_lock():
        for path in cfg.get("paths", []):
            if not os.path.exists(path):
                continue
            try:
                if any(a[3] == me and not a[1] & INHERITED for a in api.entries(path)):
                    api.set_entry(path, sid, 0, 4, os.path.isdir(path))
            except JailError as e:
                say(str(e))
                failed += 1
    api.uenv.DeleteAppContainerProfile(name)
    return 1 if failed else 0

def probe():
    """Run `cmd /c exit 7` in a throwaway AppContainer inside a job: prints {"appcontainer": true} when it worked."""
    ok = False
    try:
        api = Win()
        sid = api.profile_sid("Pythonator.Probe")
        try:
            comspec = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "cmd.exe")
            ok = api.wait(api.spawn(f'"{comspec}" /d /c exit 7', dict(os.environ), None, sid, [])) == 7
        finally:
            api.uenv.DeleteAppContainerProfile("Pythonator.Probe")
    except (OSError, JailError) as e:
        say(str(e))
    sys.stdout.write(json.dumps({"appcontainer": ok}))
    return 0

def ctrl_c(pid):
    """Attach to the console of `pid` and press Ctrl+C there: Windows' graceful stop for console programs. The
    background service ends the job after the grace period if the program doesn't exit."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return 1
    try:
        k32.SetConsoleCtrlHandler(None, True)  # not this helper
        return 0 if k32.GenerateConsoleCtrlEvent(0, 0) else 2
    finally:
        k32.FreeConsole()

def main(argv):
    if sys.platform != "win32":
        say("this launcher is for Windows")
        return 2
    if argv[1:2] == ["--probe"]:
        return probe()
    if argv[1:2] == ["--ctrl-c"] and len(argv) == 3 and argv[2].isdigit():
        return ctrl_c(int(argv[2]))
    try:
        if argv[1:2] == ["--revoke"]:
            return revoke(json.loads(argv[2]))
        return run(json.loads(argv[1]))
    except (IndexError, ValueError, TypeError) as e:
        say(f"missing or invalid plan: {e}")
        return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv))
