"""Make the running process show up as AniRSS instead of python3.

Two separate things carry a process name on Linux and they need setting
independently:

  * ``/proc/PID/comm`` -- the kernel's short name, capped at 15 characters. This
    is what ``ps -o comm=``, ``pidof``, ``killall``, ``pkill`` and htop's
    process-name column read. ``prctl(PR_SET_NAME)`` sets it through ctypes with
    no third-party dependency.
  * ``/proc/PID/cmdline`` -- the full argv, which is what ``ps aux`` and htop's
    default Command column print. Changing it means overwriting argv's own
    buffer in place, which needs the setproctitle extension module.

So setproctitle is used when it's importable and prctl always runs as the
guaranteed floor. Neither is load-bearing: a cosmetic name is never worth
taking the tool down for, so every failure here is swallowed.
"""

import ctypes
import ctypes.util
import sys

PROC_NAME = "AniRSS"

# prctl(2) option number for PR_SET_NAME. The kernel copies at most 16 bytes
# including the terminating NUL, so the name itself is capped at 15.
_PR_SET_NAME = 15
_COMM_MAX = 15


def _set_comm(name: str) -> None:
    """Set /proc/self/comm via prctl. No-op off Linux or on any failure."""
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                           use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        libc.prctl(_PR_SET_NAME, name[:_COMM_MAX].encode(), 0, 0, 0)
    except Exception:  # noqa: BLE001 - cosmetic only, never fatal
        pass


def _set_cmdline(name: str) -> bool:
    """Rewrite argv so `ps aux` shows `name`. False when setproctitle is absent."""
    try:
        from setproctitle import setproctitle
    except ImportError:
        return False
    try:
        setproctitle(name)
        return True
    except Exception:  # noqa: BLE001 - cosmetic only, never fatal
        return False


def apply(name: str = PROC_NAME) -> None:
    """Name this process `name` as far as the platform allows."""
    _set_cmdline(name)
    # Unconditional: setproctitle truncates comm the same way, and when it isn't
    # installed this is the only thing that renames the process at all.
    _set_comm(name)
