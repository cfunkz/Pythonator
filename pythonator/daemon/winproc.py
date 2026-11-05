"""Windows process control: a Job Object per service, and stopping a console program with Ctrl+C.

* Every service starts suspended, goes into a job with *kill on close* (plus its memory and CPU limits), and is then
  resumed, so the whole tree is in the job before it runs any code. Closing the job ends the tree, however deep.
* Windows has no SIGTERM. Console programs react to Ctrl+C, which can only be sent from a process attached to
  their console, so a tiny helper (``Pythonator --send-ctrl-c <pid>``) attaches to the service's console and sends
  it. Ported from 0.0.9.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
HELPER_FLAG = "--send-ctrl-c"
_KILL_ON_CLOSE = 0x00002000
_JOB_MEMORY = 0x00000200
_EXTENDED_LIMITS = 9
_CPU_RATE = 15  # JobObjectCpuRateControlInformation (Windows 8 and later)
_CPU_RATE_ENABLE, _CPU_RATE_HARD_CAP = 0x1, 0x4


class Job:
    """One service's job object. A no-op everywhere but Windows."""

    def __init__(self, memory_mb: int = 0, cpu_rate: int = 0) -> None:
        self.handle = 0
        if sys.platform != "win32":
            return
        api = _kernel32()
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            raise _error("CreateJobObject")
        self.handle = handle
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE | (_JOB_MEMORY if memory_mb else 0)
        info.JobMemoryLimit = memory_mb * 1024 * 1024
        if not api.SetInformationJobObject(handle, _EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise _error("SetInformationJobObject")
        if cpu_rate:  # a hard cap in 1/100 % of the whole computer, enforced by the kernel
            rate = _CpuRate(_CPU_RATE_ENABLE | _CPU_RATE_HARD_CAP, cpu_rate)
            if not api.SetInformationJobObject(handle, _CPU_RATE, ctypes.byref(rate), ctypes.sizeof(rate)):
                self.close()
                raise _error("Setting the CPU limit")

    def adopt(self, proc: subprocess.Popen[bytes]) -> None:
        """Put a process started with CREATE_SUSPENDED into the job, then let it run."""
        if sys.platform != "win32":
            return
        handle = int(proc._handle)
        try:
            if not _kernel32().AssignProcessToJobObject(self.handle, handle):
                raise _error("AssignProcessToJobObject")
        finally:
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
            ntdll.NtResumeProcess(handle)

    def terminate(self) -> None:
        if sys.platform == "win32" and self.handle:
            _kernel32().TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if sys.platform == "win32" and self.handle:
            _kernel32().CloseHandle(self.handle)
        self.handle = 0


def request_ctrl_c(pid: int) -> bool:
    """Send Ctrl+C to the console of ``pid`` through the helper. True if it was sent."""
    if sys.platform != "win32":
        return False
    from ..paths import self_command

    try:
        done = subprocess.run(self_command(HELPER_FLAG, str(pid)), capture_output=True, timeout=10, check=False,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def helper_main(args: list[str]) -> int:
    """``--send-ctrl-c PID``: attach to the console of PID and press Ctrl+C there."""
    if sys.platform != "win32" or not args or not args[0].isdigit():
        return 64
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.FreeConsole()
    if not k32.AttachConsole(int(args[0])):
        return 1
    try:
        k32.SetConsoleCtrlHandler(None, True)  # don't stop the helper itself
        return 0 if k32.GenerateConsoleCtrlEvent(0, 0) else 2
    finally:
        k32.FreeConsole()


def _error(what: str) -> OSError:
    code = ctypes.get_last_error() if sys.platform == "win32" else 0  # type: ignore[attr-defined,unused-ignore]
    return OSError(code, f"{what} failed ({code})")


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = (("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD))

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t))

    class _CpuRate(ctypes.Structure):  # JOBOBJECT_CPU_RATE_CONTROL_INFORMATION, CpuRate member of its union
        _fields_ = (("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD))

    _api: Any = None

    def _kernel32() -> Any:
        global _api
        if _api is None:
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            h, b, d = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD
            for name, res, argtypes in (
                ("CreateJobObjectW", h, (ctypes.c_void_p, wintypes.LPCWSTR)),
                ("SetInformationJobObject", b, (h, ctypes.c_int, ctypes.c_void_p, d)),
                ("AssignProcessToJobObject", b, (h, h)),
                ("TerminateJobObject", b, (h, wintypes.UINT)),
                ("CloseHandle", b, (h,)),
            ):
                fn = getattr(api, name)
                fn.restype, fn.argtypes = res, argtypes
            _api = api
        return _api
else:
    import ctypes

    _ExtendedLimits: Any = None
    _CpuRate: Any = None

    def _kernel32() -> Any:
        raise OSError("Windows only")
