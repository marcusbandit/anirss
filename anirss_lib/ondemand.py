"""On-demand endpoints: bring an endpoint's backing service up only while
something needs it.

An [[endpoint]] with a `start` command is on-demand. Every process that
uses it (the interactive CLI, the hourly refresh job) takes a lease: an
empty `<name>.<pid>.lease` file under ~/.local/state/anirss/ondemand/. All
start/stop decisions happen under an flock on `<name>.lock`, so two
processes never race each other. The service is stopped only when the last
live lease goes away AND anirss was the one that started it (the
`<name>.started` marker); a service someone started by hand is left alone.
Leases of dead processes are pruned on every release.
"""

import atexit
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from anirss_lib.ansi import C_DIM, C_GRN, C_OFF
from anirss_lib.compat import child_flags, lock_file, pid_alive, unlock_file
from anirss_lib.config import STATE_DIR
from anirss_lib.logging import log
from anirss_lib.errors import FetchError

LEASE_DIR = STATE_DIR / "ondemand"
READY_POLL_S = 0.5


class StartError(FetchError):
    """An on-demand endpoint's service could not be brought up."""


_held: dict[str, object] = {}   # endpoint name -> Endpoint, leases this process holds
_hooks_installed = False


def is_ready(ep, timeout: float = 2.0) -> bool:
    if not ep.ready:
        return False
    try:
        with urllib.request.urlopen(ep.ready, timeout=timeout) as resp:
            resp.read(0)
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def usable(ep) -> bool:
    """Plain endpoints always; on-demand ones only while already running.
    For callers that must never trigger a (slow) start themselves."""
    return not ep.on_demand or is_ready(ep, timeout=1.0)


@contextmanager
def _locked(name: str):
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEASE_DIR / f"{name}.lock", "w") as fh:
        lock_file(fh)
        try:
            yield
        finally:
            unlock_file(fh)


def _lease(name: str, pid: int | None = None):
    return LEASE_DIR / f"{name}.{pid or os.getpid()}.lease"


def _marker(name: str):
    return LEASE_DIR / f"{name}.started"


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _live_leases(name: str) -> list[int]:
    live: list[int] = []
    for path in LEASE_DIR.glob(f"{name}.*.lease"):
        try:
            pid = int(path.name[len(name) + 1:-len(".lease")])
        except ValueError:
            continue
        if _pid_alive(pid):
            live.append(pid)
        else:
            path.unlink(missing_ok=True)
    return live


def _run(cmd: str, timeout: float) -> subprocess.CompletedProcess:
    log("INFO", f"on-demand: $ {cmd}")
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL, check=False,
                          **child_flags())


def _install_exit_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    atexit.register(release_all)
    if threading.current_thread() is not threading.main_thread():
        return

    def to_exit(signum, _frame):
        raise SystemExit(128 + signum)

    # SIGHUP does not exist on Windows.
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        if signal.getsignal(sig) in (signal.SIG_DFL, None):
            signal.signal(sig, to_exit)


def ensure(ep, say=print) -> None:
    """Take a lease on `ep` and make sure its service answers, starting it
    if needed. `say` gets the human status lines. Raises StartError."""
    if not ep.on_demand:
        return
    _install_exit_hooks()
    with _locked(ep.name):
        _lease(ep.name).touch()
        _held[ep.name] = ep
        if is_ready(ep):
            return
        say(f"{C_DIM}{ep.name}: starting connection...{C_OFF}")
        t0 = time.monotonic()
        try:
            proc = _run(ep.start, ep.timeout)
        except subprocess.TimeoutExpired:
            proc = None
        if proc is None or proc.returncode != 0:
            if proc is None:
                detail = "timed out"
            else:
                lines = (proc.stderr or proc.stdout).strip().splitlines()
                detail = lines[-1] if lines else f"exit {proc.returncode}"
            log("ERROR", f"on-demand: {ep.name} start failed: {detail}")
            err = StartError(f"{ep.name}: couldn't start ({detail})")
        else:
            _marker(ep.name).touch()
            deadline = t0 + ep.timeout
            err = None
            while not is_ready(ep):
                if time.monotonic() > deadline:
                    err = StartError(f"{ep.name}: not ready after {ep.timeout:.0f}s")
                    break
                time.sleep(READY_POLL_S)
    if err is not None:
        release(ep)
        raise err
    took = time.monotonic() - t0
    log("INFO", f"on-demand: {ep.name} ready in {took:.1f}s")
    say(f"{C_GRN}{ep.name}: connected{C_OFF} {C_DIM}({took:.0f}s){C_OFF}")


def release(ep) -> None:
    """Drop this process's lease; stop the service if it was ours and no
    live lease is left."""
    with _locked(ep.name):
        _lease(ep.name).unlink(missing_ok=True)
        _held.pop(ep.name, None)
        if _live_leases(ep.name) or not _marker(ep.name).exists():
            return
        _marker(ep.name).unlink(missing_ok=True)
        if not ep.stop:
            return
        try:
            proc = _run(ep.stop, 60)
            if proc.returncode != 0:
                log("WARN", f"on-demand: {ep.name} stop failed: {proc.stderr.strip()}")
        except subprocess.TimeoutExpired:
            log("WARN", f"on-demand: {ep.name} stop timed out")


def release_all() -> None:
    for ep in list(_held.values()):
        try:
            release(ep)
        except OSError as e:
            log("WARN", f"on-demand: release {ep.name}: {e}")


# -------- links --------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    return bool(pa.netloc) and (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def _magnet_for(link: str, timeout: float) -> str:
    """The magnet a proxied download link redirects to, or `link` itself
    when it doesn't (a real .torrent body, an error, ...)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(link, timeout=timeout) as resp:
            resp.read(0)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "") if e.headers else ""
        if location.startswith("magnet:"):
            return location
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log("WARN", f"on-demand: resolving {link[:80]}: {e}")
    return link


def resolve_links(links: list[str], endpoints) -> list[str]:
    """Swap links served by an on-demand endpoint for the magnets they
    redirect to, so qBittorrent never needs the service once anirss exits."""
    owners = {i: ep for i, link in enumerate(links)
              for ep in endpoints
              if ep.on_demand and _same_origin(link, ep.url)}
    if not owners:
        return links
    out = list(links)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {i: pool.submit(_magnet_for, links[i], ep.timeout)
                   for i, ep in owners.items()}
        for i, fut in futures.items():
            out[i] = fut.result()
    return out
