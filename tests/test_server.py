"""HTTP API server (anirss_lib.server) end to end over a real loopback socket.

No network and no qBittorrent: endpoints.fetch_items and the qB login are
monkeypatched, so every route runs through the real Handler + Anirss facade
against fakes.
"""

import http.client
import json
import os
import stat
import threading
import urllib.parse

import pytest

from anirss_lib import endpoints as endpoints_mod
from anirss_lib import server
from anirss_lib.api import Anirss
from anirss_lib.errors import (
    FetchError,
    NotFoundError,
    QbtAuthError,
    ValidationError,
)
from anirss_lib.types import Item

TOKEN = "tok"
ORIGIN = "http://localhost:5173"
MAGNET = "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=x"


def _item(title, link=None, **kw):
    return Item(title, link or f"https://example.test/{abs(hash(title))}.torrent", **kw)


NYAA_ITEMS = [
    _item("[Sub] Frieren - 01 [1080p] [HEVC].mkv", seeders=10, downloads=100),
    _item("[Sub] Frieren - 02 [1080p] [HEVC].mkv", seeders=8, downloads=90),
    _item("[Sub] Frieren - 03 [1080p] [AVC].mkv", seeders=5, downloads=50),
    _item("[Other] Frieren - 01 [720p] [AVC].mkv", seeders=2, downloads=10),
]
RSS_ITEMS = [
    _item("Frieren - 01 [1080p] HEVC"),
    _item("Frieren - 02 [1080p] HEVC"),
    _item("Frieren - 03 [720p] AVC"),
    _item("Frieren - 04 [720p] AVC"),
]


class FakeFetch:
    """Stands in for endpoints.fetch_items: results/errors keyed by endpoint."""

    def __init__(self):
        self.results = {"nyaa": list(NYAA_ITEMS), "erai": list(RSS_ITEMS)}
        self.errors = {}
        self.calls = []

    def __call__(self, ep, query):
        self.calls.append((ep.name, query))
        if ep.name in self.errors:
            raise self.errors[ep.name]
        return list(self.results.get(ep.name, []))


class FakeQbt:
    """Records qB traffic; GET responses come from the `gets` map."""

    def __init__(self):
        self.posts = []
        self.multiparts = []
        self.get_calls = []
        self.gets = {"/api/v2/rss/rules": {}, "/api/v2/rss/items": {},
                     "/api/v2/torrents/info": []}

    def post(self, endpoint, **fields):
        self.posts.append((endpoint, fields))
        return "Ok."

    def post_multipart(self, endpoint, **fields):
        self.multiparts.append((endpoint, fields))
        return "Ok."

    def get_json(self, endpoint, **params):
        self.get_calls.append((endpoint, params))
        return self.gets[endpoint]

    def posted(self, endpoint):
        return [f for e, f in self.posts if e == endpoint]


class Client:
    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None, *, token=TOKEN, headers=None,
             raw=None, ctype="application/json", host=None):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if host is not None:
            h["Host"] = host
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            h.setdefault("Content-Type", ctype)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            try:
                parsed = json.loads(payload) if payload else None
            except ValueError:
                parsed = payload
            return resp.status, parsed, resp
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.call("GET", server.API + path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", server.API + path, body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", server.API + path, **kw)


@pytest.fixture
def env(monkeypatch, tmp_path):
    fetch = FakeFetch()
    qbt = FakeQbt()
    monkeypatch.setattr(endpoints_mod, "fetch_items", fetch)
    monkeypatch.setattr("anirss_lib.api.login_with_password",
                        lambda *a, **kw: qbt)
    monkeypatch.setattr("anirss_lib.api._qbt_reachable", lambda *a, **kw: True)
    save = tmp_path / "save"
    ani = Anirss(config={
        "endpoint": [
            {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.example/"},
            {"name": "erai", "kind": "rss",
             "url": "https://rss.example/feed?s={query}&token=SECRET"},
        ],
        "downloads": {"save_base": str(save), "hidden_base": str(tmp_path / "hid"),
                      "movie_path": str(tmp_path / "movies")},
    })
    srv = server.make_server(ani, "127.0.0.1", 0, TOKEN, cors_origins=[ORIGIN])
    thread = threading.Thread(target=srv.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    env = type("Env", (), {})()
    env.ani, env.fetch, env.qbt, env.save = ani, fetch, qbt, save
    env.port = srv.server_address[1]
    env.c = Client(env.port)
    yield env
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


# ---- auth ----------------------------------------------------------------

def test_health_needs_no_token(env):
    status, body, _ = env.c.get("/health", token=None)
    assert status == 200
    assert body["ok"] is True and body["version"] == env.ani.version
    assert body["features"] == ["qbt_clients", "qbt_env_session", "endpoint_writes"]


@pytest.mark.parametrize("token", [None, "wrong", "to", "tokk"])
def test_other_routes_reject_missing_or_bad_token(env, token):
    for path in ("/endpoints", "/subscriptions", "/qbt/status"):
        status, body, _ = env.c.get(path, token=token)
        assert status == 401, path
        assert body["error"]["type"] == "unauthorized"


def test_non_bearer_scheme_rejected(env):
    status, _, _ = env.c.get("/endpoints", token=None,
                             headers={"Authorization": f"Basic {TOKEN}"})
    assert status == 401


def test_correct_bearer_ok(env):
    status, body, _ = env.c.get("/endpoints")
    assert status == 200
    assert [e["name"] for e in body["endpoints"]] == ["nyaa", "erai"]


def test_unknown_route_without_token_is_401_not_404(env):
    status, _, _ = env.c.get("/nope", token=None)
    assert status == 401


def test_make_server_requires_token(env):
    from anirss_lib.errors import ConfigError
    with pytest.raises(ConfigError):
        server.make_server(env.ani, "127.0.0.1", 0, "")


# ---- Host guard ------------------------------------------------------------

@pytest.mark.parametrize("host", ["localhost:{p}", "127.0.0.1:{p}", "[::1]:{p}",
                                  "LOCALHOST:{p}", "localhost", "[::1]"])
def test_loopback_hosts_allowed(env, host):
    status, _, _ = env.c.get("/health", host=host.format(p=env.port))
    assert status == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80",
                                  "localhost.evil.example", "10.0.0.5:8787", ""])
def test_foreign_host_forbidden(env, host):
    status, body, _ = env.c.get("/health", host=host)
    assert status == 403
    assert body["error"]["type"] == "forbidden"


def test_host_guard_precedes_auth(env):
    status, _, _ = env.c.get("/endpoints", token=None, host="evil.example")
    assert status == 403


def test_host_guard_off_when_bound_beyond_loopback(env):
    """loopback_only is derived from the bind host, not the Host header."""
    srv = server.make_server(env.ani, "127.0.0.1", 0, TOKEN)
    try:
        assert srv.RequestHandlerClass.loopback_only is True
    finally:
        srv.server_close()
    handler = type("H", (server.Handler,), {"loopback_only": False})
    assert handler._host_ok(handler.__new__(handler)) is True


# ---- CORS ------------------------------------------------------------------

def test_preflight_allowed_origin(env):
    status, _, resp = env.c.call(
        "OPTIONS", server.API + "/search", token=None,
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert status == 204
    assert resp.getheader("Access-Control-Allow-Origin") == ORIGIN
    assert "POST" in resp.getheader("Access-Control-Allow-Methods")
    assert "DELETE" in resp.getheader("Access-Control-Allow-Methods")
    assert "Authorization" in resp.getheader("Access-Control-Allow-Headers")
    assert "Content-Type" in resp.getheader("Access-Control-Allow-Headers")
    assert resp.getheader("Vary") == "Origin"


def test_preflight_disallowed_origin(env):
    status, _, resp = env.c.call("OPTIONS", server.API + "/search", token=None,
                                 headers={"Origin": "http://evil.example"})
    assert status == 204
    assert resp.getheader("Access-Control-Allow-Origin") is None
    assert resp.getheader("Access-Control-Allow-Methods") is None


def test_preflight_without_origin(env):
    status, _, resp = env.c.call("OPTIONS", server.API + "/search", token=None)
    assert status == 204
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_normal_responses_echo_allowed_origin(env):
    _, _, resp = env.c.get("/health", headers={"Origin": ORIGIN})
    assert resp.getheader("Access-Control-Allow-Origin") == ORIGIN
    # errors carry it too, or a browser client can't read the 401
    status, _, resp = env.c.get("/endpoints", token=None, headers={"Origin": ORIGIN})
    assert status == 401
    assert resp.getheader("Access-Control-Allow-Origin") == ORIGIN


def test_normal_responses_omit_disallowed_origin(env):
    _, _, resp = env.c.get("/health", headers={"Origin": "http://evil.example"})
    assert resp.getheader("Access-Control-Allow-Origin") is None


# ---- error mapping ---------------------------------------------------------

def test_validation_error_400(env):
    status, body, _ = env.c.get("/search?q=")
    assert status == 400
    assert body["error"]["type"] == "validation_error"
    assert "empty query" in body["error"]["message"]


def test_unknown_endpoint_404(env):
    status, body, _ = env.c.get("/search?q=frieren&endpoint=nope")
    assert status == 404
    assert body["error"]["type"] == "not_found"
    assert "nope" in body["error"]["message"]


def test_fetch_error_502(env):
    env.fetch.errors = {"nyaa": FetchError("nyaa down"),
                        "erai": FetchError("erai down")}
    status, body, _ = env.c.get("/search?q=frieren")
    assert status == 502
    assert body["error"]["type"] == "fetch_error"
    assert "nyaa down" in body["error"]["message"]


def test_qbt_auth_error_503(env, monkeypatch):
    def boom(*a, **kw):
        raise QbtAuthError("bad password")
    monkeypatch.setattr("anirss_lib.api.login_with_password", boom)
    status, body, _ = env.c.get("/subscriptions")
    assert status == 503
    assert body["error"]["type"] == "qbt_auth_required"


@pytest.mark.parametrize("exc, status, kind", [
    (ValidationError("v"), 400, "validation_error"),
    (NotFoundError("n"), 404, "not_found"),
    (QbtAuthError("a"), 503, "qbt_auth_required"),
    (FetchError("f"), 502, "fetch_error"),
])
def test_facade_exceptions_map_to_status(env, monkeypatch, exc, status, kind):
    def boom(*a, **kw):
        raise exc
    monkeypatch.setattr(env.ani, "subscriptions", boom)
    got, body, _ = env.c.get("/subscriptions")
    assert (got, body["error"]["type"]) == (status, kind)


def test_qbt_error_502(env, monkeypatch):
    from anirss_lib.errors import QbtError

    def boom(*a, **kw):
        raise QbtError("qb 500")
    monkeypatch.setattr(env.ani, "subscriptions", boom)
    status, body, _ = env.c.get("/subscriptions")
    assert (status, body["error"]["type"]) == (502, "qbt_error")


def test_unknown_route_404(env):
    status, body, _ = env.c.get("/nope")
    assert status == 404 and body["error"]["type"] == "not_found"


def test_path_outside_api_prefix_404(env):
    status, body, _ = env.c.call("GET", "/health", token=None)
    assert status == 404 and body["error"]["type"] == "not_found"


def test_wrong_method_405(env):
    status, body, _ = env.c.call("DELETE", server.API + "/endpoints")
    assert status == 405
    assert body["error"]["type"] == "method_not_allowed"
    status, _, _ = env.c.get("/subscriptions/preview")
    assert status == 405
    status, _, _ = env.c.delete("/search")
    assert status == 405
    # GET on a /subscriptions/<name> path is a wrong method, not an unknown route
    status, _, _ = env.c.get("/subscriptions/Frieren")
    assert status == 405


def test_trailing_slash_tolerated(env):
    status, _, _ = env.c.get("/endpoints/")
    assert status == 200


def test_unexpected_exception_500_and_server_survives(env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(env.ani, "endpoints", boom)
    status, body, _ = env.c.get("/endpoints")
    assert status == 500
    assert body["error"]["type"] == "internal_error"
    # details stay in the log, never in the response
    assert "RuntimeError" not in body["error"]["message"]
    status, _, _ = env.c.get("/health")
    assert status == 200


# ---- body handling ---------------------------------------------------------

def test_wrong_content_type_400(env):
    status, body, _ = env.c.post("/search", raw=b'{"query": "x"}', ctype="text/plain")
    assert status == 400
    assert "Content-Type" in body["error"]["message"]


def test_content_type_with_charset_ok(env):
    status, _, _ = env.c.post("/search", raw=b'{"query": "frieren"}',
                              ctype="application/json; charset=utf-8")
    assert status == 200


def test_invalid_json_400(env):
    status, body, _ = env.c.post("/search", raw=b"{nope")
    assert status == 400 and "invalid JSON" in body["error"]["message"]


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"str"', b"42", b"null"])
def test_non_object_json_400(env, raw):
    status, body, _ = env.c.post("/search", raw=raw)
    assert status == 400 and "object" in body["error"]["message"]


def test_empty_body_is_empty_object(env):
    status, body, _ = env.c.post("/search")
    assert status == 400
    assert "missing `query`" in body["error"]["message"]


def test_oversized_content_length_400(env, monkeypatch):
    monkeypatch.setattr(server, "MAX_JSON_BYTES", 16)
    status, body, _ = env.c.post("/search", raw=b'{"query": "' + b"x" * 64 + b'"}')
    assert status == 400 and "too large" in body["error"]["message"]


def test_bad_content_length_400(env):
    status, body, _ = env.c.post("/search", raw=b"{}",
                                 headers={"Content-Length": "abc"})
    # http.client overwrites nothing when we set it ourselves
    assert status == 400 and "Content-Length" in body["error"]["message"]


def test_too_many_items_400(env, monkeypatch):
    monkeypatch.setattr(server, "MAX_ITEMS", 2)
    items = [{"title": f"t{i}", "link": f"https://x/{i}"} for i in range(3)]
    status, body, _ = env.c.post("/enrich", {"items": items})
    assert status == 400 and "too many items" in body["error"]["message"]


@pytest.mark.parametrize("items", [
    [{"title": "x"}],                                   # no link
    [{"title": 1, "link": "https://x"}],               # non-string title
    ["not-a-dict"],
    [{"title": "x", "link": "https://x", "seeders": "5"}],
    [{"title": "x", "link": "https://x", "seeders": True}],
    [{"title": "x", "link": "https://x", "size": 5}],
    "nope",
])
def test_bad_items_400(env, items):
    status, body, _ = env.c.post("/refine/analyze", {"query": "q", "items": items})
    assert status == 400 and body["error"]["type"] == "validation_error"


def test_non_string_field_400(env):
    status, body, _ = env.c.post("/search", {"query": 5})
    assert status == 400 and "must be a string" in body["error"]["message"]


def test_bad_boolean_400(env):
    status, body, _ = env.c.get("/search?q=frieren&fallback=maybe")
    assert status == 400 and "boolean" in body["error"]["message"]


def test_unknown_item_keys_ignored(env):
    status, body, _ = env.c.post("/refine/analyze", {
        "query": "Frieren", "endpoint": "erai", "auto_resolution": False,
        "items": [{"title": "Frieren - 01", "link": "https://x/1", "bogus": 1}]})
    assert status == 200 and len(body["items"]) == 1


# ---- search / endpoints ------------------------------------------------------

def test_search_get(env):
    status, body, _ = env.c.get("/search?q=Frieren%201080p&endpoint=nyaa")
    assert status == 200
    assert body["query"] == "Frieren 1080p"
    assert body["endpoint"] == "nyaa"
    assert len(body["items"]) == 4
    assert body["items"][0]["title"] == NYAA_ITEMS[0].title
    assert body["fell_back"] is False
    assert env.fetch.calls == [("nyaa", "Frieren 1080p")]


def test_search_get_fallback_default_on(env):
    env.fetch.results["nyaa"] = []
    status, body, _ = env.c.get("/search?q=frieren")
    assert status == 200
    assert body["endpoint"] == "erai" and body["fell_back"] is True
    assert all(it["source"] == "erai" for it in body["items"])
    assert [t["status"] for t in body["tried"]] == ["empty", "ok"]


def test_search_get_fallback_zero(env):
    env.fetch.results["nyaa"] = []
    status, body, _ = env.c.get("/search?q=frieren&endpoint=nyaa&fallback=0")
    assert status == 200
    assert body["items"] == [] and body["fell_back"] is False
    assert env.fetch.calls == [("nyaa", "frieren")]


def test_search_post(env):
    status, body, _ = env.c.post("/search", {"query": "frieren", "endpoint": "erai",
                                             "fallback": False})
    assert status == 200
    assert body["endpoint"] == "erai"
    assert [i["title"] for i in body["items"]] == [i.title for i in RSS_ITEMS]
    assert env.fetch.calls == [("erai", "frieren")]


def test_search_defaults_to_first_endpoint(env):
    _, body, _ = env.c.post("/search", {"query": "frieren"})
    assert body["endpoint"] == "nyaa"


def test_search_enrich_flag_calls_enrich(env, monkeypatch):
    seen = []
    monkeypatch.setattr(env.ani, "enrich",
                        lambda items: seen.append(len(items)) or items)
    status, _, _ = env.c.get("/search?q=frieren&enrich=1")
    assert status == 200 and seen == [4]


def test_endpoints_route_shape(env):
    status, body, _ = env.c.get("/endpoints")
    assert status == 200
    assert set(body) == {"endpoints"}
    nyaa, erai = body["endpoints"]
    assert set(nyaa) == set(erai) == {"name", "kind", "url", "client_side_refine"}
    assert (nyaa["name"], nyaa["kind"], nyaa["client_side_refine"]) == ("nyaa", "nyaa", False)
    assert (erai["name"], erai["kind"], erai["client_side_refine"]) == ("erai", "rss", True)
    assert nyaa["url"] == "https://nyaa.example/"


def test_endpoints_route_masks_secret_query_params(env):
    status, body, _ = env.c.get("/endpoints")
    assert status == 200
    erai = body["endpoints"][1]
    assert "token=***" in erai["url"]
    assert "{query}" in erai["url"]          # placeholder survives masking
    assert "SECRET" not in json.dumps(body)
    # masking is display-only: the configured URL is still what gets fetched
    assert env.ani.endpoints()[1].url.endswith("token=SECRET")


def test_qbt_status_route(env):
    status, body, _ = env.c.get("/qbt/status")
    assert status == 200
    assert body["reachable"] is True and body["authenticated"] is False


def test_feed_route(env, monkeypatch):
    monkeypatch.setattr("anirss_lib.api.fetch_rss", lambda url, *a: RSS_ITEMS[:2])
    status, body, _ = env.c.post("/feed", {"url": "https://feed.example/rss"})
    assert status == 200 and len(body["items"]) == 2


def test_feed_route_rejects_non_http(env):
    status, _, _ = env.c.post("/feed", {"url": "file:///etc/passwd"})
    assert status == 400


def test_enrich_route(env, monkeypatch):
    monkeypatch.setattr(env.ani, "enrich", lambda items: [i._replace(seeders=99) for i in items])
    status, body, _ = env.c.post("/enrich", {"items": [
        {"title": "x", "link": "https://x"}]})
    assert status == 200 and body["items"][0]["seeders"] == 99


# ---- refine ----------------------------------------------------------------

def _dump(items):
    return [i._asdict() for i in items]


def test_refine_analyze(env):
    status, body, _ = env.c.post("/refine/analyze", {
        "query": "Frieren", "items": _dump(NYAA_ITEMS), "endpoint": "nyaa"})
    assert status == 200
    # every item has a resolution, so auto-resolution appends the highest
    assert body["auto_resolution"]["applied"] is True
    assert body["auto_resolution"]["token"] == "1080p"
    assert body["query"] == "Frieren 1080p"
    assert len(body["items"]) == 3
    assert body["client_side"] is False
    assert body["endpoint"] == "nyaa"
    assert {"label", "tokens", "member_count", "members"} <= set(body["groups"][0])
    assert "available" in body["best_fit"]


def test_refine_analyze_no_auto_resolution(env):
    _, body, _ = env.c.post("/refine/analyze", {
        "query": "Frieren", "items": _dump(NYAA_ITEMS), "endpoint": "erai",
        "auto_resolution": False})
    assert body["auto_resolution"]["applied"] is False
    assert body["query"] == "Frieren" and len(body["items"]) == 4
    assert body["client_side"] is True


def test_refine_analyze_unknown_endpoint_404(env):
    status, _, _ = env.c.post("/refine/analyze", {
        "query": "q", "items": [], "endpoint": "zzz"})
    assert status == 404


def test_refine_apply_client_side_tokens(env):
    status, body, _ = env.c.post("/refine/apply", {
        "query": "Frieren", "endpoint": "erai", "items": _dump(RSS_ITEMS),
        "op": {"type": "tokens", "tokens": ["1080p"]}})
    assert status == 200
    assert body["applied"] is True and body["mode"] == "client"
    assert body["previous_count"] == 4 and body["delta"] == -2
    assert [i["title"] for i in body["items"]] == [i.title for i in RSS_ITEMS[:2]]
    assert "1080p" in body["query"]
    assert body["endpoint"] == "erai"
    assert isinstance(body["groups"], list) and "best_fit" in body
    assert env.fetch.calls == []          # client-side: no refetch


def test_refine_apply_term_and_exclude(env):
    _, body, _ = env.c.post("/refine/apply", {
        "query": "Frieren", "endpoint": "erai", "items": _dump(RSS_ITEMS),
        "op": {"type": "term", "term": "avc"}})
    assert body["applied"] and len(body["items"]) == 2
    _, body, _ = env.c.post("/refine/apply", {
        "query": "Frieren", "endpoint": "erai", "items": _dump(RSS_ITEMS),
        "op": {"type": "exclude", "term": "avc"}})
    assert body["applied"] and len(body["items"]) == 2
    assert body["query"].endswith("-avc")


def test_refine_apply_noop_reports_reason(env):
    _, body, _ = env.c.post("/refine/apply", {
        "query": "Frieren", "endpoint": "erai", "items": _dump(RSS_ITEMS),
        "op": {"type": "term", "term": "zzzz"}})
    assert body["applied"] is False and body["reason"] == "no_match"
    assert body["delta"] == 0 and len(body["items"]) == 4


def test_refine_apply_refetch_on_nyaa(env):
    env.fetch.results["nyaa"] = NYAA_ITEMS[:2]
    _, body, _ = env.c.post("/refine/apply", {
        "query": "Frieren", "endpoint": "nyaa", "items": _dump(NYAA_ITEMS),
        "op": {"type": "tokens", "tokens": ["HEVC"]}})
    assert body["applied"] and body["mode"] == "refetch" and body["delta"] == -2
    assert env.fetch.calls and env.fetch.calls[0][0] == "nyaa"


def test_refine_apply_bad_op(env):
    for op in ("nope", None, {"type": "bogus"}, {"type": "tokens"},
               {"type": "tokens", "tokens": "1080p"}):
        status, body, _ = env.c.post("/refine/apply", {
            "query": "q", "endpoint": "erai", "items": _dump(RSS_ITEMS), "op": op})
        assert status == 400, op
        assert body["error"]["type"] == "validation_error"


# ---- subscriptions ---------------------------------------------------------

def test_subscribe_with_query_and_items(env):
    status, body, _ = env.c.post("/subscriptions", {
        "query": "Frieren 1080p", "items": _dump(NYAA_ITEMS), "name": "Frieren"})
    assert status == 201
    assert body["name"] == "Frieren"
    assert body["save_path"] == os.path.join(str(env.save), "Frieren")
    assert body["feed_url"].startswith("https://nyaa.example/?")
    assert "q=Frieren+1080p" in body["feed_url"]
    assert body["tags"] == ["Anime"]
    (feed,) = env.qbt.posted("/api/v2/rss/addFeed")
    assert feed == {"url": body["feed_url"], "path": "Frieren"}
    (rule,) = env.qbt.posted("/api/v2/rss/setRule")
    rule_def = json.loads(rule["ruleDef"])
    assert rule["ruleName"] == "Frieren"
    assert rule_def["savePath"] == os.path.join(str(env.save), "Frieren")
    assert rule_def["affectedFeeds"] == [body["feed_url"]]
    assert rule_def["torrentParams"]["tags"] == ["Anime"]
    assert env.fetch.calls == []          # items given: no search


def test_subscribe_name_defaults_to_show_name(env):
    _, body, _ = env.c.post("/subscriptions", {
        "query": "Frieren", "items": _dump(NYAA_ITEMS)})
    assert body["name"] == "Frieren"


def test_subscribe_hidden_uses_hidden_base(env, tmp_path):
    _, body, _ = env.c.post("/subscriptions", {
        "query": "Frieren", "items": _dump(NYAA_ITEMS), "hidden": True})
    assert body["save_path"] == os.path.join(str(tmp_path / "hid"), "Frieren")


def test_subscribe_without_items_searches(env):
    status, body, _ = env.c.post("/subscriptions", {"query": "Frieren"})
    assert status == 201 and body["name"] == "Frieren"
    assert env.fetch.calls == [("nyaa", "Frieren")]


def test_subscribe_no_results_404(env):
    env.fetch.results["nyaa"] = []
    status, body, _ = env.c.post("/subscriptions", {"query": "Frieren"})
    assert status == 404 and body["error"]["type"] == "not_found"
    assert env.qbt.posts == []


@pytest.mark.parametrize("name", ["a/b", "..", "a\\b"])
def test_subscribe_rejects_path_names(env, name):
    status, _, _ = env.c.post("/subscriptions", {
        "query": "Frieren", "items": _dump(NYAA_ITEMS), "name": name})
    assert status == 400, name
    assert env.qbt.posts == []


def test_subscribe_missing_query_400(env):
    status, _, _ = env.c.post("/subscriptions", {"items": _dump(NYAA_ITEMS)})
    assert status == 400


def test_subscribe_with_feed_url(env, monkeypatch):
    monkeypatch.setattr("anirss_lib.api.fetch_rss", lambda url, *a: RSS_ITEMS)
    status, body, _ = env.c.post("/subscriptions", {
        "feed_url": "https://feed.example/rss", "name": "Feedy"})
    assert status == 201
    assert body["feed_url"] == "https://feed.example/rss"
    assert body["save_path"] == os.path.join(str(env.save), "Feedy")
    (feed,) = env.qbt.posted("/api/v2/rss/addFeed")
    assert feed["url"] == "https://feed.example/rss"


def test_subscribe_with_feed_url_empty_feed_404(env, monkeypatch):
    monkeypatch.setattr("anirss_lib.api.fetch_rss", lambda url, *a: [])
    status, _, _ = env.c.post("/subscriptions", {"feed_url": "https://f.example/rss"})
    assert status == 404


def test_subscribe_preview_does_not_touch_qbt(env):
    status, body, _ = env.c.post("/subscriptions/preview", {
        "query": "Frieren -dub", "items": _dump(NYAA_ITEMS)})
    assert status == 200
    assert set(body) == {"feed_url", "must_contain", "must_not_contain",
                         "endpoint_name"}
    assert body["endpoint_name"] == "nyaa"
    assert env.qbt.posts == [] and env.qbt.get_calls == []


def test_subscribe_preview_searches_without_items_and_no_fallback(env):
    env.fetch.results["nyaa"] = []
    status, body, _ = env.c.post("/subscriptions/preview", {"query": "Frieren"})
    assert status == 200 and body["endpoint_name"] == "nyaa"
    assert env.fetch.calls == [("nyaa", "Frieren")]     # no fallback to erai


def test_subscribe_preview_rss_endpoint_moves_exclusions(env):
    _, body, _ = env.c.post("/subscriptions/preview", {
        "query": "Frieren -dub", "endpoint": "erai", "items": _dump(RSS_ITEMS)})
    assert body["must_not_contain"] == "dub"
    assert body["feed_url"].startswith("https://rss.example/feed?s=Frieren")


RULE_URL = "https://nyaa.example/?q=Frieren"


def _seed_rule(env, name="Frieren"):
    sp = os.path.join(str(env.save), name)
    env.qbt.gets["/api/v2/rss/rules"] = {
        name: {"affectedFeeds": [RULE_URL], "savePath": sp, "enabled": True}}
    env.qbt.gets["/api/v2/rss/items"] = {name: {"url": RULE_URL}}
    env.qbt.gets["/api/v2/torrents/info"] = [
        {"hash": "h1", "save_path": sp, "name": "ep1"},
        {"hash": "h2", "save_path": "/elsewhere", "name": "other"}]
    return sp


def test_list_subscriptions(env):
    _seed_rule(env)
    status, body, _ = env.c.get("/subscriptions")
    assert status == 200
    (sub,) = body["subscriptions"]
    assert sub["name"] == "Frieren" and sub["feed_url"] == RULE_URL
    assert sub["torrent_count"] == 1 and sub["rule_enabled"] is True


def test_list_subscriptions_empty(env):
    _, body, _ = env.c.get("/subscriptions")
    assert body == {"subscriptions": []}


def test_delete_dry_run_makes_no_destructive_posts(env):
    _seed_rule(env, "Sousou no Frieren")
    name = urllib.parse.quote("Sousou no Frieren")
    status, body, _ = env.c.delete(f"/subscriptions/{name}?dry_run=1&torrents=1&files=1")
    assert status == 200
    assert body["dry_run"] is True
    (plan,) = body["plans"]
    assert plan["name"] == "Sousou no Frieren"
    assert plan["feed_path"] == "Sousou no Frieren"
    assert [t["hash"] for t in plan["torrents"]] == ["h1"]
    assert env.qbt.posts == []


def test_delete_real_removes_feed_and_rule(env):
    _seed_rule(env)
    status, body, _ = env.c.delete("/subscriptions/Frieren")
    assert status == 200 and body["dry_run"] is False
    assert env.qbt.posted("/api/v2/rss/removeItem") == [{"path": "Frieren"}]
    assert env.qbt.posted("/api/v2/rss/removeRule") == [{"ruleName": "Frieren"}]
    assert env.qbt.posted("/api/v2/torrents/delete") == []


def test_delete_with_torrents_posts_torrent_delete(env):
    _seed_rule(env)
    env.c.delete("/subscriptions/Frieren?torrents=1")
    assert env.qbt.posted("/api/v2/torrents/delete") == [
        {"hashes": "h1", "deleteFiles": "false"}]


def test_delete_unknown_404(env):
    status, body, _ = env.c.delete("/subscriptions/ghost")
    assert status == 404 and body["error"]["type"] == "not_found"
    assert env.qbt.posts == []


def test_delete_file_removal_confined_to_download_roots(env, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    env.qbt.gets["/api/v2/rss/rules"] = {
        "Evil": {"affectedFeeds": [RULE_URL], "savePath": str(outside)}}
    status, body, _ = env.c.delete("/subscriptions/Evil?files=1")
    assert status == 400 and "outside the download roots" in body["error"]["message"]
    assert outside.exists() and env.qbt.posts == []


def test_remove_post(env):
    _seed_rule(env)
    status, body, _ = env.c.post("/subscriptions/remove", {
        "names": ["Frieren"], "dry_run": True})
    assert status == 200 and body["dry_run"] is True
    assert env.qbt.posts == []
    status, body, _ = env.c.post("/subscriptions/remove", {"names": ["Frieren"]})
    assert status == 200 and body["dry_run"] is False
    assert env.qbt.posted("/api/v2/rss/removeRule") == [{"ruleName": "Frieren"}]


@pytest.mark.parametrize("body", [{}, {"names": []}, {"names": "Frieren"},
                                  {"names": [1]}])
def test_remove_post_validates_names(env, body):
    status, _, _ = env.c.post("/subscriptions/remove", body)
    assert status == 400


# ---- downloads / movie / torrents / upload -----------------------------------

def test_download_links(env):
    status, body, _ = env.c.post("/downloads", {
        "links": [MAGNET, "https://x.example/a.torrent"], "name": "Frieren"})
    assert status == 201
    sp = os.path.join(str(env.save), "Frieren")
    assert body["count"] == 2 and body["save_path"] == sp
    assert body["hashes"] == ["ab" * 20]
    (add,) = env.qbt.posted("/api/v2/torrents/add")
    assert add["urls"] == f"{MAGNET}\nhttps://x.example/a.torrent"
    assert add["savepath"] == sp and add["tags"] == "Anime"


def test_download_links_need_name(env):
    status, body, _ = env.c.post("/downloads", {"links": [MAGNET]})
    assert status == 400 and "missing `name`" in body["error"]["message"]


@pytest.mark.parametrize("links", [[], ["ftp://x/y"], ["javascript:alert(1)"]])
def test_download_bad_links(env, links):
    status, _, _ = env.c.post("/downloads", {"links": links, "name": "n"})
    assert status == 400
    assert env.qbt.posts == []


def test_download_items_default_name(env):
    items = [{"title": "[Sub] Frieren - 01 [1080p].mkv", "link": MAGNET}]
    status, body, _ = env.c.post("/downloads", {"items": items})
    assert status == 201
    assert body["save_path"] == os.path.join(str(env.save), "Frieren")


def test_download_items_empty_400(env):
    status, _, _ = env.c.post("/downloads", {"items": []})
    assert status == 400


def test_download_hidden(env, tmp_path):
    _, body, _ = env.c.post("/downloads", {"links": [MAGNET], "name": "n",
                                           "hidden": True})
    assert body["save_path"] == os.path.join(str(tmp_path / "hid"), "n")


def test_movie(env):
    status, body, _ = env.c.post("/movie", {"link": MAGNET})
    assert status == 201
    assert body["save_path"] == env.ani.config["downloads"]["movie_path"]
    (add,) = env.qbt.posted("/api/v2/torrents/add")
    assert add["savepath"] == body["save_path"] and add["urls"] == MAGNET


def test_movie_bad_link(env):
    assert env.c.post("/movie", {"link": "nope"})[0] == 400
    assert env.c.post("/movie", {})[0] == 400


def test_torrents_by_name(env):
    sp = _seed_rule(env)
    status, body, _ = env.c.get("/torrents?name=Frieren")
    assert status == 200
    assert [t["hash"] for t in body["torrents"]] == ["h1"]
    assert body["torrents"][0]["save_path"] == sp
    assert set(body["torrents"][0]) >= {"hash", "name", "state", "progress"}


def test_torrents_all_and_by_hashes(env):
    _seed_rule(env)
    _, body, _ = env.c.get("/torrents")
    assert len(body["torrents"]) == 2
    env.c.get("/torrents?hashes=h1|h2")
    assert env.qbt.get_calls[-1] == ("/api/v2/torrents/info", {"hashes": "h1|h2"})


def test_torrents_bad_name_400(env):
    status, _, _ = env.c.get("/torrents?name=" + urllib.parse.quote("a/b"))
    assert status == 400


TORRENT = b"d8:announce20:http://tracker.test/4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:" \
          + b"a" * 20 + b"ee"


def test_upload_torrent(env):
    status, body, _ = env.c.call(
        "POST", server.API + "/torrents/upload?file_name=ep1.torrent&name=My%20Show",
        raw=TORRENT, ctype="application/x-bittorrent",
        headers={"Origin": ORIGIN})
    assert status == 201
    sp = os.path.join(str(env.save), "My Show")
    assert body["count"] == 1 and body["save_path"] == sp
    assert len(body["hashes"]) == 1 and len(body["hashes"][0]) == 40
    (endpoint, fields), = env.qbt.multiparts
    assert endpoint == "/api/v2/torrents/add"
    assert fields["file_bytes"] == TORRENT
    assert fields["file_name"] == "ep1.torrent" and fields["savepath"] == sp


def test_upload_octet_stream_and_default_name(env):
    status, body, _ = env.c.call("POST", server.API + "/torrents/upload",
                                 raw=TORRENT, ctype="application/octet-stream")
    assert status == 201
    assert body["save_path"] == os.path.join(str(env.save), "file")


def test_upload_file_name_path_is_stripped(env):
    env.c.call("POST", server.API + "/torrents/upload?file_name=../../etc/x.torrent",
               raw=TORRENT, ctype="application/x-bittorrent")
    ((_, fields),) = env.qbt.multiparts
    assert fields["file_name"] == "x.torrent"


def test_upload_wrong_content_type_400(env):
    status, body, _ = env.c.call("POST", server.API + "/torrents/upload",
                                 raw=TORRENT, ctype="application/json")
    assert status == 400 and "x-bittorrent" in body["error"]["message"]
    assert env.qbt.multiparts == []


def test_upload_empty_body_400(env):
    status, body, _ = env.c.call("POST", server.API + "/torrents/upload",
                                 raw=b"", ctype="application/x-bittorrent")
    assert status == 400 and "empty" in body["error"]["message"]


def test_upload_oversized_400(env, monkeypatch):
    monkeypatch.setattr(server, "MAX_TORRENT_BYTES", 8)
    status, _, _ = env.c.call("POST", server.API + "/torrents/upload",
                              raw=TORRENT, ctype="application/x-bittorrent")
    assert status == 400


def test_upload_garbage_torrent_still_uploads_without_hashes(env):
    status, body, _ = env.c.call("POST", server.API + "/torrents/upload",
                                 raw=b"not a torrent", ctype="application/x-bittorrent")
    assert status == 201 and body["hashes"] == []


# ---- connection hygiene --------------------------------------------------------

def test_keepalive_connection_usable_after_unauthorized_post(env):
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=5)
    try:
        body = json.dumps({"query": "frieren"}).encode()
        conn.request("POST", server.API + "/search", body=body,
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer wrong"})
        r = conn.getresponse()
        r.read()
        assert r.status == 401
        conn.request("GET", server.API + "/health")
        r = conn.getresponse()
        r.read()
        assert r.status == 200
    finally:
        conn.close()


def test_keepalive_connection_usable_after_oversized_body(env, monkeypatch):
    monkeypatch.setattr(server, "MAX_JSON_BYTES", 16)
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=5)
    try:
        conn.request("POST", server.API + "/search",
                     body=b'{"query": "' + b"x" * 64 + b'"}',
                     headers={"Content-Type": "application/json",
                              "Authorization": f"Bearer {TOKEN}"})
        r = conn.getresponse()
        r.read()
        assert r.status == 400
        conn.request("GET", server.API + "/health")
        r = conn.getresponse()
        r.read()
        assert r.status == 200
    finally:
        conn.close()


def test_keepalive_reuses_connection_on_success(env):
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=5)
    try:
        for _ in range(3):
            conn.request("GET", server.API + "/health")
            r = conn.getresponse()
            r.read()
            assert r.status == 200
    finally:
        conn.close()


# ---- resolve_token ---------------------------------------------------------------

@pytest.fixture
def token_paths(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr(server, "STATE_DIR", state)
    monkeypatch.setattr(server, "TOKEN_PATH", state / "api.token")
    return state


def test_resolve_token_env_wins(token_paths):
    server.TOKEN_PATH.parent.mkdir(parents=True)
    server.TOKEN_PATH.write_text("saved\n")
    assert server.resolve_token("cfg", "envtok") == ("envtok", "ANIRSS_API_TOKEN")


def test_resolve_token_configured_beats_saved(token_paths):
    server.TOKEN_PATH.parent.mkdir(parents=True)
    server.TOKEN_PATH.write_text("saved\n")
    assert server.resolve_token("cfg", None) == ("cfg", "[server].token")
    assert server.resolve_token("cfg", "") == ("cfg", "[server].token")


def test_resolve_token_saved_file(token_paths):
    server.TOKEN_PATH.parent.mkdir(parents=True)
    server.TOKEN_PATH.write_text("  saved-token \n")
    assert server.resolve_token("", None) == ("saved-token", str(server.TOKEN_PATH))


def test_resolve_token_generates_and_saves_0600(token_paths):
    token, source = server.resolve_token("", None)
    assert len(token) >= 32
    assert "generated" in source and str(server.TOKEN_PATH) in source
    assert server.TOKEN_PATH.read_text() == token + "\n"
    assert stat.S_IMODE(server.TOKEN_PATH.stat().st_mode) == 0o600
    # second call reuses it
    assert server.resolve_token("", None) == (token, str(server.TOKEN_PATH))


def test_resolve_token_tightens_existing_empty_file_mode(token_paths):
    server.TOKEN_PATH.parent.mkdir(parents=True)
    server.TOKEN_PATH.write_text("")
    server.TOKEN_PATH.chmod(0o644)
    token, _ = server.resolve_token("", None)
    assert stat.S_IMODE(server.TOKEN_PATH.stat().st_mode) == 0o600
    assert server.TOKEN_PATH.read_text().strip() == token


def test_resolve_token_unwritable_state_dir(token_paths, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(server, "STATE_DIR", blocker / "sub")
    monkeypatch.setattr(server, "TOKEN_PATH", blocker / "sub" / "api.token")
    token, source = server.resolve_token("", None)
    assert token and source == "generated (not saved)"


# ---- _parse_serve_args ---------------------------------------------------------

CFG = {"server": {"host": "127.0.0.1", "port": 8787}}


def test_parse_serve_args_defaults_from_cfg():
    assert server._parse_serve_args([], CFG) == ("127.0.0.1", 8787)


def test_parse_serve_args_overrides():
    assert server._parse_serve_args(["--host", "0.0.0.0", "--port", "9000"], CFG) \
        == ("0.0.0.0", 9000)
    assert server._parse_serve_args(["--port", "1"], CFG) == ("127.0.0.1", 1)
    assert server._parse_serve_args(["--host", "::1"], CFG) == ("::1", 8787)


def test_parse_serve_args_cfg_port_may_be_string():
    assert server._parse_serve_args([], {"server": {"host": "h", "port": "81"}}) \
        == ("h", 81)


@pytest.mark.parametrize("argv", [["--port", "abc"], ["--port"], ["--host"],
                                  ["--bogus"], ["--port", "1", "extra"],
                                  ["--port", "8.5"]])
def test_parse_serve_args_errors(argv):
    with pytest.raises(ValidationError):
        server._parse_serve_args(argv, CFG)


@pytest.mark.parametrize("port", ["-1", "70000"])
def test_parse_serve_args_rejects_out_of_range_port(port):
    with pytest.raises(ValidationError):
        server._parse_serve_args(["--port", port], CFG)


# ---- endpoint writes -----------------------------------------------------

@pytest.fixture
def writable(env, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[endpoint]]\nname = "nyaa"\nkind = "nyaa"\n'
                    'url = "https://nyaa.example/"\n')
    env.ani.config_file = path
    return path


def test_post_endpoint_adds_then_replaces(env, writable):
    ep = {"name": "extra", "kind": "rss", "url": "https://x.example/r?q={query}&apikey=K"}
    status, body, _ = env.c.post("/endpoints", ep)
    assert status == 201 and body["replaced"] is False
    assert body["endpoint"]["url"] == "https://x.example/r?q={query}&apikey=***"
    status, body, _ = env.c.post("/endpoints", dict(ep, url="https://y.example/r?q={query}"))
    assert status == 200 and body["replaced"] is True
    _, body, _ = env.c.get("/endpoints")
    assert [e["name"] for e in body["endpoints"]] == ["nyaa", "extra"]
    assert "y.example" in writable.read_text()


def test_post_endpoint_refuses_commands(env, writable):
    before = writable.read_text()
    status, body, _ = env.c.post("/endpoints", {"name": "x", "kind": "nyaa",
                                                "url": "https://x.example/", "start": "id"})
    assert status == 400 and body["error"]["type"] == "validation_error"
    assert writable.read_text() == before


def test_post_endpoint_needs_the_token(env, writable):
    status, _, _ = env.c.post("/endpoints", {"name": "x", "kind": "nyaa",
                                             "url": "https://x.example/"}, token=None)
    assert status == 401


def test_delete_endpoint(env, writable):
    status, body, _ = env.c.delete("/endpoints/nyaa")
    assert status == 200 and body == {"removed": "nyaa"}
    assert env.ani.endpoints() == []
    status, body, _ = env.c.get("/search?q=Frieren")
    assert status == 409 and body["error"]["type"] == "no_endpoints"
    status, body, _ = env.c.delete("/endpoints/nyaa")
    assert status == 404


def test_endpoint_writes_without_a_config_file_are_config_errors(env):
    status, body, _ = env.c.post("/endpoints", {"name": "x", "kind": "nyaa",
                                                "url": "https://x.example/"})
    assert status == 500 and body["error"]["type"] == "config_error"
