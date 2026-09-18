"""Tie a child process's lifetime to this process (Windows Job Object).

A headless ``kicad-cli api-server`` is a plain child process: when the MCP
host is killed (not closed) nothing stops it and it lingers until someone
runs taskkill. On Windows a Job Object with KILL_ON_JOB_CLOSE fixes that:
the child is assigned to a job whose only handle this process holds, and the
kernel closes that handle - killing every process in the job - when this
process ends for any reason, clean or not.

    guard = ChildGuard.bind(process.pid)     # OSError with the reason on failure
    ...
    guard.close()                            # after the child was stopped

``supported()`` is False off Windows, where ``bind`` raises OSError; a
caller keeps the reason and carries on, because a session without the
guard still works (it just relies on a clean close).
"""
from __future__ import annotations

import ctypes
import sys

PLATFORM = sys.platform
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

__all__ = ["ChildGuard", "supported"]


def supported() -> bool:
    """Whether this platform has Job Objects (Windows only)."""
    return PLATFORM == "win32"


if sys.platform == "win32":
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.CreateJobObjectW.restype = wintypes.HANDLE
    _K32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _K32.SetInformationJobObject.restype = wintypes.BOOL
    _K32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                             wintypes.DWORD)
    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _K32.AssignProcessToJobObject.restype = wintypes.BOOL
    _K32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _K32.CloseHandle.restype = wintypes.BOOL
    _K32.CloseHandle.argtypes = (wintypes.HANDLE,)


def _win_error(what: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(f"{what} failed (Windows error {code}: {ctypes.FormatError(code).strip()})")


def _new_job() -> int:
    """A job whose processes die when its last handle closes."""
    job = _K32.CreateJobObjectW(None, None)
    if not job:
        raise _win_error("CreateJobObject")
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _K32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                        ctypes.byref(limits), ctypes.sizeof(limits)):
        error = _win_error("SetInformationJobObject")
        _K32.CloseHandle(job)
        raise error
    return int(job)


def _assign(job: int, pid: int) -> None:
    process = _K32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not process:
        raise _win_error(f"OpenProcess({pid})")
    try:
        if not _K32.AssignProcessToJobObject(job, process):
            raise _win_error(f"AssignProcessToJobObject({pid})")
    finally:
        _K32.CloseHandle(process)


class ChildGuard:
    """One job holding one child process; ``close()`` releases the job, which
    kills the child if it is still running."""

    def __init__(self, pid: int, handle: int) -> None:
        self.pid = pid
        self._handle: int | None = handle

    @classmethod
    def bind(cls, pid: int) -> ChildGuard:
        """Put ``pid`` into a fresh kill-on-close job; OSError names the step
        that failed (off Windows, or a process that cannot be opened)."""
        if not supported():
            raise OSError("Job Objects exist on Windows only; the child is not guarded")
        job = _new_job()
        try:
            _assign(job, pid)
        except OSError:
            _K32.CloseHandle(job)
            raise
        return cls(pid, job)

    @property
    def active(self) -> bool:
        return self._handle is not None

    def close(self) -> None:
        """Release the job handle (idempotent); a child still inside dies."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _K32.CloseHandle(handle)

    def __repr__(self) -> str:
        return f"ChildGuard(pid={self.pid}, active={self.active})"
