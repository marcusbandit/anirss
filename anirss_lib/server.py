"""`anirss --serve`: the library API (anirss_lib.api.Anirss) over JSON HTTP.

Stdlib only (http.server). One Anirss instance serves every request thread.
Routes live under /api/v1 and are documented in docs/agents/api-reference.md.

Security model: binds 127.0.0.1 by default; every route except /health needs
`Authorization: Bearer <token>`. The token comes from ANIRSS_API_TOKEN or
[server].token, else one is generated once into STATE_DIR/api.token (0600)
and printed at startup. A page in the user's browser can reach loopback, so
loopback is not trusted on its own.
"""

import hmac
import json
import secrets
import sys
import traceback
import urllib.parse
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from anirss_lib.ansi import C_BLD, C_DIM, C_GRN, C_OFF
from anirss_lib.api import (
    Anirss,
    client_info,
    endpoint_info,
    item_from_dict,
    to_jsonable,
)
from anirss_lib.config import STATE_DIR
from anirss_lib.errors import (
    AnirssError,
    ConfigError,
    FetchError,
    NotFoundError,
    QbtAuthError,
    QbtError,
    ValidationError,
)
from anirss_lib.logging import log

API = "/api/v1"
TOKEN_PATH = STATE_DIR / "api.token"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_TORRENT_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 2000
LOOPBACK = ("127.0.0.1", "localhost", "::1")


# ---- token ---------------------------------------------------------------

def resolve_token(configured: str, env: str | None) -> tuple[str, str]:
    """(token, where it came from). Generates + persists one if none is set."""
    if env:
        return env, "ANIRSS_API_TOKEN"
    if configured:
        return configured, "[server].token"
    try:
        saved = TOKEN_PATH.read_text().strip()
    except OSError:
        saved = ""
    if saved:
        return saved, str(TOKEN_PATH)
    token = secrets.token_urlsafe(32)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.touch(mode=0o600, exist_ok=True)
        TOKEN_PATH.chmod(0o600)
        TOKEN_PATH.write_text(token + "\n")
    except OSError as e:
        log("WARN", f"couldn't save API token to {TOKEN_PATH}: {e}")
        return token, "generated (not saved)"
    return token, f"generated, saved to {TOKEN_PATH}"


# ---- request parsing helpers -----------------------------------------------

def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ValidationError(f"expected a boolean, got {v!r}")


def _str(body: dict, key: str, *, required: bool = False) -> str | None:
    v = body.get(key)
    if v is None:
        if required:
            raise ValidationError(f"missing `{key}`")
        return None
    if not isinstance(v, str):
        raise ValidationError(f"`{key}` must be a string")
    return v


def _str_list(body: dict, key: str) -> list[str] | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValidationError(f"`{key}` must be a list of strings")
    return v


def _items(body: dict, *, required: bool = True):
    raw = body.get("items")
    if raw is None and not required:
        return None
    if not isinstance(raw, list):
        raise ValidationError("`items` must be a list")
    if len(raw) > MAX_ITEMS:
        raise ValidationError(f"too many items (max {MAX_ITEMS})")
    return [item_from_dict(d) for d in raw]


def _search_json(res) -> dict:
    return {**to_jsonable(res), "fell_back": res.fell_back}


def _outcome_json(ani: Anirss, out, endpoint: str | None) -> dict:
    d = {"applied": out.applied, "reason": out.reason, "mode": out.mode,
         "query": out.query, "items": to_jsonable(out.items),
         "previous_count": out.previous_count, "delta": out.delta}
    # Fresh groups / best-fit for the new state, so a client can render the
    # next refine screen without a second round trip.
    nxt = ani.analyze(out.query, out.items, endpoint, auto_resolution=False)
    d["groups"] = to_jsonable(nxt["groups"])
    d["best_fit"] = to_jsonable(nxt["best_fit"])
    d["endpoint"] = nxt["endpoint"]
    return d


_ERRORS: list[tuple[type, int, str]] = [
    (ValidationError, 400, "validation_error"),
    (NotFoundError, 404, "not_found"),
    (QbtAuthError, 503, "qbt_auth_required"),
    (QbtError, 502, "qbt_error"),
    (FetchError, 502, "fetch_error"),
    (ConfigError, 500, "config_error"),
    (AnirssError, 400, "error"),
]


# ---- handler -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "anirss"
    protocol_version = "HTTP/1.1"
    timeout = 120   # per-connection socket timeout

    # set by make_server()
    ani: Anirss
    token: str
    cors_origins: tuple[str, ...] = ()
    loopback_only: bool = True

    # -- plumbing --

    def log_message(self, format: str, *args) -> None:
        log("INFO", f"api {self.address_string()} {format % args}")

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self.cors_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self._cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, kind: str, message: str) -> None:
        # An error can be sent before the request body was read; drop the
        # connection so unread body bytes aren't parsed as the next request.
        self.close_connection = True
        self._send(status, {"error": {"type": kind, "message": message}})

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        """Errors http.server raises itself (bad request line, 501, 414) in
        the same JSON envelope as everything else."""
        try:
            short = HTTPStatus(code).phrase
        except ValueError:
            short = "error"
        self.close_connection = True
        self._error(code, short.lower().replace(" ", "_"), message or short)

    def _read_body(self, limit: int) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValidationError("bad Content-Length") from None
        if n > limit:
            raise ValidationError(f"body too large (max {limit} bytes)")
        return self.rfile.read(n) if n > 0 else b""

    def _json_body(self) -> dict:
        raw = self._read_body(MAX_JSON_BYTES)
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        try:
            body = json.loads(raw)
        except ValueError as e:
            raise ValidationError(f"invalid JSON: {e}") from None
        if not isinstance(body, dict):
            raise ValidationError("JSON body must be an object")
        return body

    def _host_ok(self) -> bool:
        """DNS-rebinding guard: on a loopback bind, only loopback Host names."""
        if not self.loopback_only:
            return True
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            name = host[1:host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name.lower() in LOOPBACK

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        scheme, _, given = auth.partition(" ")
        # Headers arrive latin-1 decoded; compare in that encoding so a
        # non-ASCII token still matches itself.
        try:
            given_b = given.strip().encode("latin-1")
        except UnicodeEncodeError:
            return False
        return scheme.lower() == "bearer" and hmac.compare_digest(
            given_b, self.token.encode())

    # -- dispatch --

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        if self.headers.get("Origin") in self.cors_origins:
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                self._error(403, "forbidden", "unexpected Host header")
                return
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[-1] for k, v in
                     urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
            if not path.startswith(API):
                self._error(404, "not_found", f"no route {path}")
                return
            route = path[len(API):] or "/"
            if route != "/health" and not self._authorized():
                self._error(401, "unauthorized", "missing or bad bearer token")
                return
            handler, arg = self._route(method, route)
            if handler is None:
                self._error(404 if arg is None else 405, "not_found" if arg is None
                            else "method_not_allowed", f"{method} {route}")
                return
            status, payload = handler(query, arg)
            self._send(status, payload)
        except BaseException as e:  # noqa: BLE001 - a request must never kill its thread silently
            for cls, status, kind in _ERRORS:
                if isinstance(e, cls):
                    self._error(status, kind, str(e))
                    return
            log("ERROR", f"api {method} {self.path}: {traceback.format_exc()}")
            try:
                # Details go to the log only; they can carry paths and URLs.
                self._error(500, "internal_error", "internal error (see anirss.log)")
            except OSError:
                pass

    def _route(self, method: str, route: str
               ) -> tuple[Callable[[dict, str | None], tuple[int, Any]] | None, str | None]:
        """(handler, path-arg). (None, None) = unknown route; (None, "")
        = known route, wrong method."""
        table: dict[str, dict[str, Callable]] = {
            "/health": {"GET": self.h_health},
            "/qbt/status": {"GET": self.h_qbt_status},
            "/clients": {"GET": self.h_clients},
            "/endpoints": {"GET": self.h_endpoints},
            "/search": {"GET": self.h_search_get, "POST": self.h_search_post},
            "/feed": {"POST": self.h_feed},
            "/enrich": {"POST": self.h_enrich},
            "/refine/analyze": {"POST": self.h_analyze},
            "/refine/apply": {"POST": self.h_apply},
            "/subscriptions": {"GET": self.h_subs_list, "POST": self.h_subscribe},
            "/subscriptions/preview": {"POST": self.h_subscribe_preview},
            "/subscriptions/remove": {"POST": self.h_remove_post},
            "/downloads": {"POST": self.h_download},
            "/movie": {"POST": self.h_movie},
            "/torrents": {"GET": self.h_torrents},
            "/torrents/upload": {"POST": self.h_upload},
        }
        if route in table:
            fn = table[route].get(method)
            return (fn, None) if fn else (None, "")
        if route.startswith("/subscriptions/"):
            name = urllib.parse.unquote(route[len("/subscriptions/"):])
            if method == "DELETE":
                return self.h_remove_one, name
            return None, ""
        return None, None

    # -- handlers: (query-params, path-arg) -> (status, payload) --

    def h_health(self, q, _):
        return 200, {"ok": True, "version": self.ani.version}

    def h_qbt_status(self, q, _):
        return 200, self.ani.qbt_status(_str(q, "client"))

    def h_clients(self, q, _):
        return 200, {"clients": [client_info(c) for c in self.ani.clients()]}

    def h_endpoints(self, q, _):
        return 200, {"endpoints": [endpoint_info(ep) for ep in self.ani.endpoints()]}

    def h_search_get(self, q, _):
        res = self.ani.search(q.get("q", ""), q.get("endpoint") or None,
                              fallback=_bool(q.get("fallback"), True),
                              enrich=_bool(q.get("enrich")))
        return 200, _search_json(res)

    def h_search_post(self, q, _):
        b = self._json_body()
        res = self.ani.search(_str(b, "query", required=True) or "",
                              _str(b, "endpoint"),
                              fallback=_bool(b.get("fallback"), True),
                              enrich=_bool(b.get("enrich")))
        return 200, _search_json(res)

    def h_feed(self, q, _):
        b = self._json_body()
        items = self.ani.fetch_feed(_str(b, "url", required=True) or "",
                                    enrich=_bool(b.get("enrich")))
        return 200, {"items": to_jsonable(items)}

    def h_enrich(self, q, _):
        b = self._json_body()
        return 200, {"items": to_jsonable(self.ani.enrich(_items(b)))}

    def h_analyze(self, q, _):
        b = self._json_body()
        res = self.ani.analyze(_str(b, "query", required=True) or "", _items(b),
                               _str(b, "endpoint"),
                               auto_resolution=_bool(b.get("auto_resolution"), True))
        return 200, to_jsonable(res)

    def h_apply(self, q, _):
        b = self._json_body()
        op = b.get("op")
        if not isinstance(op, dict):
            raise ValidationError("`op` must be an object {type, tokens?, term?}")
        endpoint = _str(b, "endpoint")
        out = self.ani.refine(_str(b, "query", required=True) or "", _items(b),
                              _str(op, "type", required=True) or "",
                              endpoint=endpoint, tokens=_str_list(op, "tokens"),
                              term=_str(op, "term") or "",
                              enrich=_bool(b.get("enrich")))
        return 200, _outcome_json(self.ani, out, endpoint)

    def h_subs_list(self, q, _):
        return 200, {"subscriptions": to_jsonable(
            self.ani.subscriptions(client=_str(q, "client")))}

    def h_subscribe(self, q, _):
        b = self._json_body()
        hidden = _bool(b.get("hidden"))
        client = _str(b, "client")
        feed_url = _str(b, "feed_url")
        if feed_url:
            res = self.ani.subscribe_feed(feed_url, name=_str(b, "name"),
                                          hidden=hidden, client=client)
        else:
            res = self.ani.subscribe(_str(b, "query", required=True) or "",
                                     endpoint=_str(b, "endpoint"),
                                     items=_items(b, required=False),
                                     name=_str(b, "name"), hidden=hidden,
                                     client=client)
        return 201, to_jsonable(res)

    def h_subscribe_preview(self, q, _):
        b = self._json_body()
        query = _str(b, "query", required=True) or ""
        endpoint = _str(b, "endpoint")
        items = _items(b, required=False)
        if items is None:
            items = self.ani.search(query, endpoint, fallback=False).items
        return 200, to_jsonable(self.ani.subscription_spec(query, items, endpoint))

    def _remove(self, names: list[str], opts: dict) -> tuple[int, Any]:
        dry = _bool(opts.get("dry_run"))
        plans = self.ani.remove(names, drop_torrents=_bool(opts.get("torrents")),
                                drop_files=_bool(opts.get("files")),
                                hidden=_bool(opts.get("hidden")), dry_run=dry,
                                client=_str(opts, "client"))
        return 200, {"dry_run": dry, "plans": to_jsonable(plans)}

    def h_remove_one(self, q, name):
        return self._remove([name], q)

    def h_remove_post(self, q, _):
        b = self._json_body()
        names = _str_list(b, "names")
        if not names:
            raise ValidationError("`names` must be a non-empty list of strings")
        return self._remove(names, b)

    def h_download(self, q, _):
        b = self._json_body()
        hidden = _bool(b.get("hidden"))
        client = _str(b, "client")
        items = _items(b, required=False)
        if items is not None:
            res = self.ani.download_items(items, name=_str(b, "name"), hidden=hidden,
                                          client=client)
        else:
            links = _str_list(b, "links")
            name = _str(b, "name", required=True) or ""
            res = self.ani.download(links or [], name=name, hidden=hidden, client=client)
        return 201, to_jsonable(res)

    def h_movie(self, q, _):
        b = self._json_body()
        return 201, to_jsonable(self.ani.movie(_str(b, "link", required=True) or "",
                                               client=_str(b, "client")))

    def h_torrents(self, q, _):
        hashes = [h for h in (q.get("hashes") or "").split("|") if h] or None
        return 200, {"torrents": self.ani.torrents(
            name=q.get("name") or None, hashes=hashes, hidden=_bool(q.get("hidden")),
            client=_str(q, "client"))}

    def h_upload(self, q, _):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype not in ("application/x-bittorrent", "application/octet-stream"):
            raise ValidationError("upload body must be application/x-bittorrent")
        data = self._read_body(MAX_TORRENT_BYTES)
        res = self.ani.upload_torrent(data, file_name=q.get("file_name") or "file.torrent",
                                      name=q.get("name") or None,
                                      hidden=_bool(q.get("hidden")),
                                      client=_str(q, "client"))
        return 201, to_jsonable(res)


# ---- server ------------------------------------------------------------------

def make_server(ani: Anirss, host: str, port: int, token: str,
                cors_origins: list[str] | None = None) -> ThreadingHTTPServer:
    """A ready-to-run server (call .serve_forever()); port 0 picks a free one."""
    if not token:
        raise ConfigError("the API server needs a token")
    handler = type("AnirssHandler", (Handler,), {
        "ani": ani, "token": token,
        "cors_origins": tuple(cors_origins or ()),
        "loopback_only": host in LOOPBACK,
    })
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def _parse_serve_args(argv: list[str], cfg) -> tuple[str, int]:
    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    it = iter(argv)
    for a in it:
        if a in ("--host", "--port"):
            v = next(it, None)
            if v is None:
                raise ValidationError(f"{a} needs a value")
            if a == "--host":
                host = v
            else:
                try:
                    port = int(v)
                except ValueError:
                    raise ValidationError(f"--port expects a number, got {v!r}") from None
                if not 0 <= port <= 65535:
                    raise ValidationError(f"--port must be 0-65535, got {port}")
        else:
            raise ValidationError(f"unknown --serve option {a!r} (use --host / --port)")
    return host, port


def serve(argv: list[str], cfg) -> None:
    """`anirss --serve [--host H] [--port P]`: runs until Ctrl-C."""
    import os

    from anirss_lib.logging import init_log
    host, port = _parse_serve_args(argv, cfg)
    init_log(cfg["logging"]["log_path"])
    token, source = resolve_token(cfg["server"].get("token") or "",
                                  os.environ.get("ANIRSS_API_TOKEN"))
    ani = Anirss(cfg)
    srv = make_server(ani, host, port, token, cfg["server"].get("cors_origins"))
    bound_host, bound_port = srv.server_address[:2]
    print(f"{C_GRN}anirss API{C_OFF} on {C_BLD}http://{bound_host}:{bound_port}{API}{C_OFF}",
          flush=True)
    if source.startswith("generated"):
        print(f"{C_DIM}token ({source}): {C_OFF}{token}", flush=True)
    else:
        print(f"{C_DIM}token: from {source}{C_OFF}", flush=True)
    if host not in LOOPBACK:
        print(f"{C_DIM}listening beyond loopback; put TLS in front if this "
              f"leaves your machine{C_OFF}", file=sys.stderr)
    log("INFO", f"api serving on {bound_host}:{bound_port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
