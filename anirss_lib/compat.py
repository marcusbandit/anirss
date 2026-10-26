"""The few places where Windows needs a different call than POSIX.

Everything else in anirss runs unchanged on both. Linux and macOS take the
same code path as before; each helper here is a thin branch on `IS_WINDOWS`.
"""

import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def lock_file(fh) -> None:
    """Take an exclusive lock on the open file `fh`, waiting for it."""
    if IS_WINDOWS:
        # LK_LOCK gives up with OSError after ten one-second tries; keep going.
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue
    else:
        fcntl.flock(fh, fcntl.LOCK_EX)


def unlock_file(fh) -> None:
    if IS_WINDOWS:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists."""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    # On Windows os.kill(pid, 0) would terminate the process, so ask the OS.
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means it exists; anything else means it does not.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def child_flags() -> dict:
    """Popen/run kwargs so a helper child opens no console window."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def detached_flags() -> dict:
    """Popen kwargs that start a child outliving this process."""
    if IS_WINDOWS:
        return {"creationflags": (subprocess.DETACHED_PROCESS
                                  | subprocess.CREATE_NEW_PROCESS_GROUP)}
    return {"start_new_session": True}


def utf8_stdio() -> None:
    """Windows opens piped stdio in the ANSI code page, which cannot print
    every release title. Switch it to UTF-8 and never raise on a character."""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
