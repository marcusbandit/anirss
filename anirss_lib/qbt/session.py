"""qBittorrent WebUI session + SID cookie persistence."""

import getpass
import http.cookiejar
import json
import re
import secrets
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from anirss_lib.ansi import C_BLD, C_CYN, C_GRN, C_OFF, C_RED, C_YEL
from anirss_lib.config import PASS_PATH, QbtConfig, SID_PATH, STATE_DIR
from anirss_lib.errors import AnirssError, QbtAuthError, QbtError
from anirss_lib.logging import log
from anirss_lib.qbt.clients import DEFAULT_CLIENT


def _build_multipart_body(boundary: str, fields: dict, file_field: str,
                          file_name: str, file_bytes: bytes,
                          file_content_type: str) -> bytes:
    """Assemble a multipart/form-data body with one file part + N text fields."""
    parts: list[bytes] = []
    for key, val in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(str(val).encode())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    # A quote or line break in the filename would end the header and let the
    # rest inject extra form fields (a second savepath, …); neutralize them.
    safe_name = "".join("_" if c in '"\\' or ord(c) < 32 or ord(c) == 127 else c
                        for c in file_name)
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{safe_name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {file_content_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def _raise_http(endpoint: str, e: Exception):
    """Turn a urllib failure into QbtError (QbtAuthError for 401/403, the
    status qBittorrent answers once a session cookie has expired)."""
    if isinstance(e, urllib.error.HTTPError):
        body = e.read().decode(errors="replace") if e.fp else ""
        msg = f"qBittorrent {endpoint} -> HTTP {e.code}: {body or e.reason}"
        log("ERROR", f"  -> HTTP {e.code}: {body or e.reason}")
        cls = QbtAuthError if e.code in (401, 403) else QbtError
        raise cls(msg) from e
    log("ERROR", f"  -> {e}")
    raise QbtError(f"qBittorrent {endpoint}: {e}") from e


class QbtSession:
    """Authenticated qBittorrent WebUI session."""

    def __init__(self, opener: urllib.request.OpenerDirector, base_url: str):
        self.opener = opener
        self.base_url = base_url

    def post(self, endpoint: str, **fields) -> str:
        log("INFO", f"POST {endpoint} {fields}")
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(f"{self.base_url}{endpoint}", data=data)
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = resp.read().decode()
        except (urllib.error.URLError, OSError) as e:
            _raise_http(endpoint, e)
        log("INFO", f"  -> {body[:200]!r}{'...' if len(body) > 200 else ''}")
        return body

    def post_multipart(self, endpoint: str, *, file_field: str,
                       file_name: str, file_bytes: bytes,
                       file_content_type: str = "application/x-bittorrent",
                       **fields) -> str:
        """POST endpoint as multipart/form-data with one file attached.

        Needed for `/api/v2/torrents/add` when uploading a local .torrent
        file, its `urls=` field only takes http/magnet, and a file path
        on the anirss host means nothing to a remote qBittorrent.
        """
        boundary = "----anirss-" + secrets.token_hex(16)
        log("INFO",
            f"POST {endpoint} (multipart) fields={fields} "
            f"file={file_name} bytes={len(file_bytes)}")
        body_bytes = _build_multipart_body(
            boundary, fields, file_field, file_name, file_bytes, file_content_type,
        )
        req = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=body_bytes,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with self.opener.open(req, timeout=30) as resp:
                body = resp.read().decode()
        except (urllib.error.URLError, OSError) as e:
            _raise_http(endpoint, e)
        log("INFO", f"  -> {body[:200]!r}{'...' if len(body) > 200 else ''}")
        return body

    def get(self, endpoint: str, **params) -> str:
        log("INFO", f"GET {endpoint} {params}")
        url = f"{self.base_url}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = resp.read().decode()
        except (urllib.error.URLError, OSError) as e:
            _raise_http(endpoint, e)
        log("INFO", f"  -> {body[:200]!r}{'...' if len(body) > 200 else ''}")
        return body

    def get_json(self, endpoint: str, **params):
        return json.loads(self.get(endpoint, **params))

    def is_alive(self) -> bool:
        """Cheap auth check: hit /api/v2/app/version. Returns True on 200."""
        url = f"{self.base_url}/api/v2/app/version"
        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=5) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError):  # HTTPError, timeouts too
            return False


# -------- SID persistence --------

def _client_file(base: Path, client: str) -> Path:
    """Where `client`'s session or saved password lives. The default client
    keeps the names from before there were several (qbt.sid); a named one gets
    its own beside them (qbt-seedbox.sid), so using one never logs another out."""
    if not client or client == DEFAULT_CLIENT:
        return base
    return base.with_name(f"{base.stem}-{client}{base.suffix}")


def _make_qbt_opener(base_url: str
                     ) -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("Referer", base_url)]
    return opener, jar


def _is_sid_cookie_name(name: str) -> bool:
    """qBittorrent <5 used `SID`; v5+ uses `QBT_SID_<port>`."""
    return name == "SID" or name.startswith("QBT_SID_")


def _save_sid(jar: http.cookiejar.CookieJar, client: str = "") -> None:
    """Extract the session cookie from `jar` and write `name\\tvalue` to the
    client's SID file (mode 600)."""
    pair: tuple[str, str] | None = None
    for cookie in jar:
        if _is_sid_cookie_name(cookie.name):
            pair = (cookie.name, cookie.value)
            break
    if pair is None:
        return
    path = _client_file(SID_PATH, client)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            STATE_DIR.chmod(0o700)
        except OSError:
            pass
        path.write_text(f"{pair[0]}\t{pair[1]}")
        path.chmod(0o600)
        log("INFO", f"saved SID ({pair[0]}) to {path}")
    except OSError as e:
        log("WARN", f"couldn't save SID to {path}: {e}")


def _load_sid(client: str = "") -> tuple[str, str] | None:
    """Return (cookie_name, value) or None. Legacy format (bare value) is read as ('SID', value)."""
    try:
        raw = _client_file(SID_PATH, client).read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    if "\t" in raw:
        name, value = raw.split("\t", 1)
        return (name, value) if value else None
    return ("SID", raw)


def _drop_sid(client: str = "") -> None:
    path = _client_file(SID_PATH, client)
    try:
        path.unlink()
        log("INFO", f"dropped stale SID at {path}")
    except OSError:
        pass


# -------- password persistence --------
# Stored at the same protection level as the SID cookie above (mode-600 file in
# a 700 state dir). A valid SID is already full WebUI access, so the password
# sits at the trust level the cache already assumes.

def _save_password(password: str, client: str = "") -> bool:
    """Write `password` to the client's password file (mode 600). Returns True on success."""
    path = _client_file(PASS_PATH, client)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            STATE_DIR.chmod(0o700)
        except OSError:
            pass
        path.write_text(password)
        path.chmod(0o600)
        log("INFO", f"saved qBittorrent password to {path}")
        return True
    except OSError as e:
        log("WARN", f"couldn't save password to {path}: {e}")
        return False


def _load_password(client: str = "") -> str | None:
    """Return the saved password, or None if there isn't one. Tolerates a
    trailing newline so `echo pw > qbt.pass` also works."""
    try:
        raw = _client_file(PASS_PATH, client).read_text()
    except OSError:
        return None
    return raw.rstrip("\n") or None


def _drop_password(client: str = "") -> None:
    path = _client_file(PASS_PATH, client)
    try:
        path.unlink()
        log("INFO", f"dropped saved password at {path}")
    except OSError:
        pass


def _offer_to_save_password(password: str, client: str = "") -> None:
    """After a manual login, ask whether to remember the password. Only reached
    when nothing usable was saved, so a 'yes' here is always a fresh save."""
    try:
        answer = input("Save this password so you won't be asked again? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer not in ("y", "yes"):
        return
    if _save_password(password, client):
        print(f"{C_GRN}saved.{C_OFF} Future logins reuse it until it stops working.")
    else:
        print(f"{C_YEL}couldn't save the password. Check the log; you'll be asked again next time{C_OFF}")


def _effective_cookie_host(host: str) -> str:
    """Mirror cookielib's `eff_request_host()` munging.

    cookielib internally rewrites bare hostnames (no dots, non-IPv4) by
    appending `.local`, so a request to `http://localhost/` is matched
    against the effective host `localhost.local`. If our cookie's domain is
    just `localhost`, the suffix-match in the cookie policy fails and the
    cookie is silently dropped on every outgoing request. Pre-applying the
    same transform here so domain matches really happen.
    """
    if not host or "." in host:
        return host
    return host + ".local"


def _make_sid_cookie(host: str, name: str, value: str, *, https: bool
                     ) -> http.cookiejar.Cookie:
    """Build a CookieJar-eligible session cookie for qBittorrent's WebUI.

    Three flags matter and got it wrong before:
      * `domain` must match cookielib's effective request host (see
        `_effective_cookie_host`, needed for `localhost`).
      * `domain_specified=True` so the policy recognises this cookie's domain
        as the host the cookie will be sent to.
      * `discard=False` so the jar keeps it for subsequent requests.
    """
    return http.cookiejar.Cookie(
        version=0, name=name, value=value,
        port=None, port_specified=False,
        domain=_effective_cookie_host(host), domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True,
        secure=https,
        expires=None, discard=False,
        comment=None, comment_url=None,
        rest={}, rfc2109=False,
    )


def _try_qbt_sid(base_url: str, *, persist: bool = True,
                 client: str = "") -> QbtSession | None:
    """Resume a qBittorrent session from the client's cached SID. None if no SID
    or it's dead (a dead one is deleted unless persist=False)."""
    loaded = _load_sid(client)
    if loaded is None:
        return None
    name, value = loaded
    opener, jar = _make_qbt_opener(base_url)
    host = urllib.parse.urlparse(base_url).hostname or ""
    jar.set_cookie(_make_sid_cookie(host, name, value, https=base_url.startswith("https://")))
    sess = QbtSession(opener, base_url)
    if sess.is_alive():
        log("INFO", "resumed qBittorrent session via cached SID")
        return sess
    if persist:
        _drop_sid(client)
    return None


# -------- reachability + local auto-start --------
# A login failure and an unreachable server are different problems. If we don't
# separate them, a qBittorrent that simply isn't running looks exactly like a
# wrong password: the saved password gets wiped and the prompt loop retries
# against a dead port. So gate the whole login flow on an actual reachability
# probe first, and for a local instance offer to start it.

def _qbt_reachable(base_url: str, timeout: float = 3.0) -> bool:
    """True if something answers HTTP at base_url. Any HTTP status counts as
    reachable (even 401/403 means the WebUI is up); only a connection failure
    or timeout is a miss."""
    req = urllib.request.Request(f"{base_url}/api/v2/app/version")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _is_local_url(base_url: str) -> bool:
    """True when base_url points at this machine, so we could start qBittorrent."""
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _qbt_start_command(qbt_cfg: QbtConfig) -> list[str] | None:
    """The command to launch a local qBittorrent, or None if we can't find one.
    An explicit `start_command` in config wins; otherwise auto-detect, preferring
    the headless qbittorrent-nox over the GUI qbittorrent."""
    custom = qbt_cfg.get("start_command")
    if custom:
        return shlex.split(custom)
    for binary in ("qbittorrent-nox", "qbittorrent"):
        path = shutil.which(binary)
        if path:
            return [path]
    return None


def _launch_qbt(cmd: list[str]) -> None:
    """Spawn qBittorrent detached so it keeps running after anirss exits."""
    log("INFO", f"launching qBittorrent: {cmd}")
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_until_reachable(base_url: str, timeout: float = 30.0) -> bool:
    """Poll base_url until it answers or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _qbt_reachable(base_url, timeout=2.0):
            return True
        time.sleep(1.0)
    return False


def _print_notice(msg: str) -> None:
    print(msg)


def _ensure_qbt_reachable(qbt_cfg: QbtConfig, *, autostart: bool = True,
                          notify: Callable[[str], None] | None = _print_notice
                          ) -> None:
    """Make sure the WebUI answers before any login attempt.

    A local instance that isn't running is simply started: needing qBittorrent
    up is a precondition of every operation anirss performs, so asking first
    only ever produced one sensible answer. Non-interactive runs get the same
    treatment, which is what makes `anirss -S <url>` usable from scripts and
    keybinds. Never returns until the server is up; raises QbtError
    otherwise. `autostart=False` skips the launch (a server process should
    not spawn qBittorrent behind its callers' backs); `notify` receives the
    progress lines (None = silent).
    """
    notify = notify or (lambda _msg: None)
    base_url = qbt_cfg["url"]
    if _qbt_reachable(base_url):
        return

    if not _is_local_url(base_url):
        raise QbtError(f"can't reach qBittorrent at {base_url}. It looks remote, so anirss "
                       f"can't start it for you. Is it running and reachable from here?")
    if not autostart:
        raise QbtError(f"can't reach qBittorrent at {base_url}. Is it running?")

    cmd = _qbt_start_command(qbt_cfg)
    if cmd is None:
        raise QbtError(f"qBittorrent isn't running at {base_url}, and no qbittorrent-nox or "
                       f"qbittorrent binary was found on PATH. Start it yourself, or set "
                       f"[qbittorrent] start_command in your config.")

    notify(f"{C_YEL}qBittorrent isn't running at {base_url}.{C_OFF}")
    notify(f"{C_CYN}==>{C_OFF} starting it ({C_BLD}{' '.join(cmd)}{C_OFF}), "
           f"waiting for the WebUI…")
    _launch_qbt(cmd)
    if not _wait_until_reachable(base_url):
        raise QbtError(f"started qBittorrent but {base_url} still isn't answering after 30s. "
                       f"Make sure the WebUI is enabled on that port.")
    notify(f"{C_GRN}OK:{C_OFF} qBittorrent is up.")


# -------- login flow --------

def qbt_login(base_url: str, username: str, password: str, *,
              persist: bool = True, client: str = ""
              ) -> tuple[QbtSession | None, str | None]:
    """(session, None) on success, (None, error) otherwise. `persist=False`
    leaves the client's shared SID cache file alone."""
    opener, jar = _make_qbt_opener(base_url)
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(f"{base_url}/api/v2/auth/login", data=data)
    log("INFO", f"POST /api/v2/auth/login user={username}")
    try:
        with opener.open(req, timeout=10) as resp:
            body = resp.read().decode().strip()
            set_cookie_headers = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return None, f"login: HTTP {e.code} {e.reason}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"can't reach qBittorrent at {base_url}: {e}"
    log("INFO", f"  -> body={body!r} set-cookie={set_cookie_headers!r}")
    # qBittorrent <5 returned "Ok."/"Fails."; v5+ returns HTTP 200 + empty body on success.
    # cookielib silently drops the session cookie for `localhost` (eff-host munging, see
    # `_effective_cookie_host`), so we extract it from the raw Set-Cookie headers and
    # re-inject through `_make_sid_cookie` which mirrors that munging. The cookie name is
    # `SID` for v<5 and `QBT_SID_<port>` for v5+.
    sid_name: str | None = None
    sid_value: str | None = None
    for raw in set_cookie_headers:
        m = re.match(r"\s*([^=\s;]+)=([^;]*)", raw)
        if m and _is_sid_cookie_name(m.group(1)):
            sid_name, sid_value = m.group(1), m.group(2)
            break
    if sid_name is None or sid_value is None:
        return None, f"login: no SID cookie in response (body={body!r})"
    host = urllib.parse.urlparse(base_url).hostname or ""
    jar.set_cookie(_make_sid_cookie(host, sid_name, sid_value,
                                    https=base_url.startswith("https://")))
    if persist:
        _save_sid(jar, client)
    return QbtSession(opener, base_url), None


def login_with_retry(qbt_cfg: QbtConfig) -> QbtSession:
    """Interactive login: SID cache, then a saved password, then prompt with retry.

    When the cached session cookie is dead we try a saved password first so the
    common case is silent. If that password is rejected (e.g. it changed on the
    server) we say so, drop it, and fall through to the prompt loop, offering to
    remember whatever the user types next.
    """
    base_url = qbt_cfg["url"]
    username = qbt_cfg["username"]
    retries = int(qbt_cfg["login_retries"])
    use_saved = qbt_cfg["save_password"]
    client = qbt_cfg.get("name", "")
    # Which of several clients is asking, so a prompt is never ambiguous.
    whose = f" for {client}" if client and client != DEFAULT_CLIENT else ""
    _ensure_qbt_reachable(qbt_cfg)
    sess = _try_qbt_sid(base_url, client=client)
    if sess is not None:
        return sess

    if use_saved:
        saved = _load_password(client)
        if saved:
            qbt, err = qbt_login(base_url, username, saved, client=client)
            if qbt is not None:
                log("INFO", "logged in with saved password")
                return qbt
            # Only treat this as a bad password if the server actually answered.
            # If it went away between the reachability probe and now, the saved
            # password is not at fault, so keep it and stop rather than wipe it.
            if not _qbt_reachable(base_url):
                raise QbtError(f"can't reach qBittorrent at {base_url}. It went away "
                               f"mid-login. Left the saved password in place.")
            print(f"{C_YEL}saved qBittorrent password{whose} was rejected "
                  f"(did it change on the server?). Removing it{C_OFF}")
            log("WARN", f"saved password rejected: {err}")
            _drop_password(client)

    for attempt in range(1, retries + 1):
        try:
            password = getpass.getpass(f"qBittorrent password{whose} ({attempt}/{retries}): ")
        except (EOFError, KeyboardInterrupt):
            raise AnirssError("cancelled")
        if not password:
            print(f"{C_YEL}empty, try again{C_OFF}")
            continue
        qbt, err = qbt_login(base_url, username, password, client=client)
        if qbt is not None:
            if use_saved:
                _offer_to_save_password(password, client)
            return qbt
        print(f"{C_RED}{err}{C_OFF}")
    raise QbtAuthError(f"login failed after {retries} attempts")


def login_with_password(qbt_cfg: QbtConfig, password: str, *,
                        autostart: bool = True,
                        notify: Callable[[str], None] | None = _print_notice,
                        persist: bool = True) -> QbtSession:
    """Non-interactive login: SID cache, then the given password, then a saved
    password. No prompts, no retry loop. Used by the non-interactive flag flow
    and the library/server (anirss_lib.api), which pass notify=None.
    """
    client = qbt_cfg.get("name", "")
    _ensure_qbt_reachable(qbt_cfg, autostart=autostart, notify=notify)
    sess = _try_qbt_sid(qbt_cfg["url"], persist=persist, client=client)
    if sess is not None:
        return sess
    if not password and qbt_cfg["save_password"]:
        password = _load_password(client) or ""
    if not password:
        # A WebUI that skips the login for this client (localhost auth off, or
        # a whitelisted subnet) answers without any session at all.
        sess = QbtSession(_make_qbt_opener(qbt_cfg["url"])[0], qbt_cfg["url"])
        if sess.is_alive():
            log("INFO", "qBittorrent needs no login from here")
            return sess
        raise QbtAuthError("qBittorrent login required (set ANIRSS_QBT_PASSWORD, use "
            "--password-stdin, or save a password from an interactive run)")
    qbt, err = qbt_login(qbt_cfg["url"], qbt_cfg["username"], password,
                         persist=persist, client=client)
    if qbt is None:
        if err and err.startswith("can't reach"):
            raise QbtError(err)
        raise QbtAuthError(err or "qBittorrent login failed", rejected=True)
    return qbt


def apply_no_seed(qbt_cfg: QbtConfig) -> None:
    print(f"{C_BLD}Applying global no-seed:{C_OFF} max_ratio=0, action=pause")
    qbt = login_with_retry(qbt_cfg)
    prefs = {
        "max_ratio_enabled": True,
        "max_ratio": 0.0,
        "max_ratio_act": 0,
    }
    qbt.post("/api/v2/app/setPreferences", json=json.dumps(prefs))
    print(f"{C_GRN}done.{C_OFF} Existing seeders pause on next qBittorrent check.")
