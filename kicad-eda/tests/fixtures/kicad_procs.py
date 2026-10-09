"""Which kicad-cli processes a test run owns, for the api-server leak checks.

Other agents and tools run kicad-cli on the same machine at the same time, so a
machine-wide PID diff reports their servers as our leaks. A process counts as
ours when its parent chain reaches this test process, or when its direct parent
is gone: an orphan is the only way a server we started outlives its starter, and
a live foreign process always has a live parent.

Windows only (Toolhelp32 snapshot through ctypes, stdlib); elsewhere the
helpers return None and the leak checks are skipped as before.
"""
from __future__ import annotations

import ctypes
import os
import sys

_MAX_DEPTH = 64
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value


if sys.platform == "win32":
    from ctypes import wintypes

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_table() -> dict[int, tuple[int, str]]:
    """{pid: (parent pid, lower-case image name)} for every process (Windows)."""
    if sys.platform != "win32":
        raise OSError("process_table needs Windows")
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap is None or snap == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        table: dict[int, tuple[int, str]] = {}
        more = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            more = _k32.Process32NextW(snap, ctypes.byref(entry))
        return table
    finally:
        _k32.CloseHandle(snap)


def _descends_from(pid: int, root: int, table: dict[int, tuple[int, str]]) -> bool:
    seen: set[int] = set()
    cur = table[pid][0]
    for _ in range(_MAX_DEPTH):
        if cur == root:
            return True
        if cur in seen or cur not in table:
            return False
        seen.add(cur)
        cur = table[cur][0]
    return False


def owned_pids(image: str = "kicad-cli.exe", root_pid: int | None = None) -> set[int] | None:
    """PIDs of `image` processes started (directly or not) by `root_pid`
    (default: this process), plus orphans whose direct parent has exited.
    None off Windows."""
    if sys.platform != "win32":
        return None
    root = os.getpid() if root_pid is None else root_pid
    table = process_table()
    name = image.lower()
    return {pid for pid, (ppid, img) in table.items()
            if img == name and pid != root
            and (ppid not in table or _descends_from(pid, root, table))}
