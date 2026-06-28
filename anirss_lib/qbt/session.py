"""qBittorrent WebUI session + SID cookie persistence."""

import getpass
import http.cookiejar
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from anirss_lib.ansi import C_BLD, C_GRN, C_OFF, C_RED, C_YEL
from anirss_lib.config import PASS_PATH, QbtConfig, SID_PATH, STATE_DIR
from anirss_lib.logging import die, log


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
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {file_content_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


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
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if e.fp else ""
            log("ERROR", f"  -> HTTP {e.code}: {body or e.reason}")
            die(f"qBittorrent {endpoint} -> HTTP {e.code}: {body or e.reason}")
        log("INFO", f"  -> {body[:200]!r}{'...' if len(body) > 200 else ''}")
        return body

    def post_multipart(self, endpoint: str, *, file_field: str,
                       file_name: str, file_bytes: bytes,
                       file_content_type: str = "application/x-bittorrent",
                       **fields) -> str:
        """POST endpoint as multipart/form-data with one file attached.

        Needed for `/api/v2/torrents/add` when uploading a local .torrent
        file — its `urls=` field only takes http/magnet, and a file path
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
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if e.fp else ""
            log("ERROR", f"  -> HTTP {e.code}: {body or e.reason}")
            die(f"qBittorrent {endpoint} -> HTTP {e.code}: {body or e.reason}")
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
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if e.fp else ""
            log("ERROR", f"  -> HTTP {e.code}: {body or e.reason}")
            die(f"qBittorrent {endpoint} -> HTTP {e.code}: {body or e.reason}")
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
        except (urllib.error.HTTPError, urllib.error.URLError):
            return False


# -------- SID persistence --------

def _make_qbt_opener(base_url: str
                     ) -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("Referer", base_url)]
    return opener, jar


def _is_sid_cookie_name(name: str) -> bool:
    """qBittorrent <5 used `SID`; v5+ uses `QBT_SID_<port>`."""
    return name == "SID" or name.startswith("QBT_SID_")


def _save_sid(jar: http.cookiejar.CookieJar) -> None:
    """Extract the session cookie from `jar` and write `name\\tvalue` to SID_PATH (mode 600)."""
    pair: tuple[str, str] | None = None
    for cookie in jar:
        if _is_sid_cookie_name(cookie.name):
            pair = (cookie.name, cookie.value)
            break
    if pair is None:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            STATE_DIR.chmod(0o700)
        except OSError:
            pass
        SID_PATH.write_text(f"{pair[0]}\t{pair[1]}")
        SID_PATH.chmod(0o600)
        log("INFO", f"saved SID ({pair[0]}) to {SID_PATH}")
    except OSError as e:
        log("WARN", f"couldn't save SID to {SID_PATH}: {e}")


def _load_sid() -> tuple[str, str] | None:
    """Return (cookie_name, value) or None. Legacy format (bare value) is read as ('SID', value)."""
    try:
        raw = SID_PATH.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    if "\t" in raw:
        name, value = raw.split("\t", 1)
        return (name, value) if value else None
    return ("SID", raw)


def _drop_sid() -> None:
    try:
        SID_PATH.unlink()
        log("INFO", f"dropped stale SID at {SID_PATH}")
    except OSError:
        pass


# -------- password persistence --------
# Stored at the same protection level as the SID cookie above (mode-600 file in
# a 700 state dir). A valid SID is already full WebUI access, so the password
# sits at the trust level the cache already assumes.

def _save_password(password: str) -> bool:
    """Write `password` to PASS_PATH (mode 600). Returns True on success."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            STATE_DIR.chmod(0o700)
        except OSError:
            pass
        PASS_PATH.write_text(password)
        PASS_PATH.chmod(0o600)
        log("INFO", f"saved qBittorrent password to {PASS_PATH}")
        return True
    except OSError as e:
        log("WARN", f"couldn't save password to {PASS_PATH}: {e}")
        return False


def _load_password() -> str | None:
    """Return the saved password, or None if there isn't one. Tolerates a
    trailing newline so `echo pw > qbt.pass` also works."""
    try:
        raw = PASS_PATH.read_text()
    except OSError:
        return None
    return raw.rstrip("\n") or None


def _drop_password() -> None:
    try:
        PASS_PATH.unlink()
        log("INFO", f"dropped saved password at {PASS_PATH}")
    except OSError:
        pass


def _offer_to_save_password(password: str) -> None:
    """After a manual login, ask whether to remember the password. Only reached
    when nothing usable was saved, so a 'yes' here is always a fresh save."""
    try:
        answer = input("Save this password so you won't be asked again? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer not in ("y", "yes"):
        return
    if _save_password(password):
        print(f"{C_GRN}saved.{C_OFF} Future logins reuse it until it stops working.")
    else:
        print(f"{C_YEL}couldn't save the password — check the log; you'll be asked again next time{C_OFF}")


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
        `_effective_cookie_host` — needed for `localhost`).
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


def _try_qbt_sid(base_url: str) -> QbtSession | None:
    """Resume a qBittorrent session from the cached SID. None if no SID or it's dead."""
    loaded = _load_sid()
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
    _drop_sid()
    return None


# -------- login flow --------

def qbt_login(base_url: str, username: str, password: str
              ) -> tuple[QbtSession | None, str | None]:
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
    except urllib.error.URLError as e:
        return None, f"can't reach qBittorrent at {base_url}: {e}"
    log("INFO", f"  -> body={body!r} set-cookie={set_cookie_headers!r}")
    # qBittorrent <5 returned "Ok."/"Fails."; v5+ returns HTTP 200 + empty body on success.
    # cookielib silently drops the session cookie for `localhost` (eff-host munging — see
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
    _save_sid(jar)
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
    sess = _try_qbt_sid(base_url)
    if sess is not None:
        return sess

    if use_saved:
        saved = _load_password()
        if saved:
            qbt, err = qbt_login(base_url, username, saved)
            if qbt is not None:
                log("INFO", "logged in with saved password")
                return qbt
            print(f"{C_YEL}saved qBittorrent password was rejected "
                  f"(did it change on the server?) — removing it{C_OFF}")
            log("WARN", f"saved password rejected: {err}")
            _drop_password()

    for attempt in range(1, retries + 1):
        try:
            password = getpass.getpass(f"qBittorrent password ({attempt}/{retries}): ")
        except (EOFError, KeyboardInterrupt):
            die("cancelled")
        if not password:
            print(f"{C_YEL}empty — try again{C_OFF}")
            continue
        qbt, err = qbt_login(base_url, username, password)
        if qbt is not None:
            if use_saved:
                _offer_to_save_password(password)
            return qbt
        print(f"{C_RED}{err}{C_OFF}")
    die(f"login failed after {retries} attempts")


def login_with_password(qbt_cfg: QbtConfig, password: str) -> QbtSession:
    """Non-interactive login: SID cache, then the given password, then a saved
    password. No prompts, no retry loop. Used by the non-interactive flag flow.
    """
    sess = _try_qbt_sid(qbt_cfg["url"])
    if sess is not None:
        return sess
    if not password and qbt_cfg["save_password"]:
        password = _load_password() or ""
    if not password:
        die("qBittorrent login required (set ANIRSS_QBT_PASSWORD, use "
            "--password-stdin, or save a password from an interactive run)")
    qbt, err = qbt_login(qbt_cfg["url"], qbt_cfg["username"], password)
    if qbt is None:
        die(err or "qBittorrent login failed")
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
