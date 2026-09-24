"""Small Win32 adapter for pytest process ownership and nonblocking pipe reads."""

import ctypes
from ctypes import wintypes
import msvcrt
import os
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_ulonglong * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
for _name, _args, _result in [
    ("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
    ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
    ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID], wintypes.BOOL),
    ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
    ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
    ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ("PeekNamedPipe", [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID], wintypes.BOOL),
]:
    _function = getattr(_kernel, _name)
    _function.argtypes = _args
    _function.restype = _result


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class WindowsJob:
    def __init__(self) -> None:
        self.handle = _check(_kernel.CreateJobObjectW(None, None))
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        try:
            _check(_kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, pid: int) -> None:
        process = _check(_kernel.OpenProcess(0x0100 | 0x0001, False, pid))  # SET_QUOTA | TERMINATE
        try:
            _check(_kernel.AssignProcessToJobObject(self.handle, process))
        finally:
            _kernel.CloseHandle(process)

    def terminate(self, timeout: float) -> None:
        _check(_kernel.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + timeout
        while True:
            counts = _Accounting()
            _check(_kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(counts), ctypes.sizeof(counts), None))
            if counts.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("pytest job cleanup did not finish")
            time.sleep(0.01)

    def close(self) -> None:
        if self.handle:
            _kernel.CloseHandle(self.handle)
            self.handle = None


def read_available(fd: int, size: int) -> bytes:
    available = wintypes.DWORD()
    if not _kernel.PeekNamedPipe(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error == 109:  # ERROR_BROKEN_PIPE: writer has closed its handle
            return b""
        raise ctypes.WinError(error)
    return os.read(fd, min(size, available.value)) if available.value else b""
