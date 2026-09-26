"""Small Win32 adapter for pytest process ownership and nonblocking pipe reads."""

import ctypes
from ctypes import wintypes
import msvcrt
import os


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _function(name, arguments, result):
    function = getattr(_kernel32, name)
    function.argtypes = arguments
    function.restype = result
    return function


_create_job = _function("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE)
_open_process = _function("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
_assign_job = _function("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL)
_terminate_job = _function("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
_close_handle = _function("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
        ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
    ]


_snapshot_threads = _function("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE)
_first_thread = _function("Thread32First", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL)
_next_thread = _function("Thread32Next", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL)
_open_thread = _function("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
_resume_thread = _function("ResumeThread", [wintypes.HANDLE], wintypes.DWORD)
_peek_pipe = _function(
    "PeekNamedPipe",
    [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p],
    wintypes.BOOL,
)


class WindowsJob:
    def __init__(self) -> None:
        self.handle = _create_job(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE, as required by assignment.
        process = _open_process(0x0101, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _assign_job(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _close_handle(process)

    def terminate(self) -> None:
        if not _terminate_job(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        _close_handle(self.handle)


def resume_process(pid: int) -> None:
    """Resume the initial thread of a process created with CREATE_SUSPENDED.

    Popen closes its primary-thread handle, so retrieve it using Tool Help.
    The process has not executed code yet and has only its initial thread.
    """
    snapshot = _snapshot_threads(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        found = _first_thread(snapshot, ctypes.byref(entry))
        while found:
            if entry.process_id == pid:
                thread = _open_thread(0x0002, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if _resume_thread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    _close_handle(thread)
                return
            found = _next_thread(snapshot, ctypes.byref(entry))
        raise OSError("pytest initial thread was not found")
    finally:
        _close_handle(snapshot)


def read_pipe(fd: int, size: int) -> bytes | None:
    """Return available bytes, b'' at EOF, or None without blocking."""
    available = wintypes.DWORD()
    if not _peek_pipe(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error == 109:  # ERROR_BROKEN_PIPE
            return b""
        raise ctypes.WinError(error)
    if not available.value:
        return None
    return os.read(fd, min(size, available.value))
