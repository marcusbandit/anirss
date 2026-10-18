"""The library layer: anirss_lib.api.Anirss, refinement.apply, and the qB
action cores it drives. Everything is faked: no network, no real qBittorrent,
and no reads/writes under the real ~/.config or ~/.local/state."""

import base64
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

import anirss_lib
from anirss_lib import api as api_mod
from anirss_lib import config as config_mod
from anirss_lib import endpoints as endpoints_mod
from anirss_lib import refine as refine_mod
from anirss_lib import refinement
from anirss_lib import stats as stats_mod
from anirss_lib.api import (
    Anirss,
    SearchResult,
    item_from_dict,
    to_jsonable,
)
from anirss_lib.config import DEFAULT_CONFIG, build_config, load_config
from anirss_lib.endpoints import Endpoint, EndpointState
from anirss_lib.errors import (
    AnirssError,
    ConfigError,
    FetchError,
    NotFoundError,
    QbtAuthError,
    QbtError,
    ValidationError,
)
from anirss_lib.qbt import actions
from anirss_lib.qbt import session as session_mod
from anirss_lib.types import Item, Pick

HEX40 = "a1" * 20

NYAA_EP = {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.example/"}
RSS1_EP = {"name": "rss1", "kind": "rss", "url": "https://rss1.example/f?q={query}"}
RSS2_EP = {"name": "rss2", "kind": "rss", "url": "https://rss2.example/f?q={query}"}


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Nothing in this file may touch the real config/state dirs."""
    state = tmp_path / "state"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "cfg" / "config.toml")
    monkeypatch.setattr(config_mod, "STATE_DIR", state)
    monkeypatch.setattr(actions, "STATE_DIR", state)
    monkeypatch.setattr(actions, "CONFIG_PATH", tmp_path / "cfg" / "config.toml")
    for name, fname in (("SID_PATH", "qbt.sid"), ("PASS_PATH", "qbt.pass")):
        monkeypatch.setattr(config_mod, name, state / fname)
        monkeypatch.setattr(session_mod, name, state / fname)
    monkeypatch.setattr(session_mod, "STATE_DIR", state)
    monkeypatch.setattr(stats_mod, "CACHE_PATH", state / "stats.json")
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD", raising=False)


@pytest.fixture(autouse=True)
def fetched(monkeypatch):
    """What fetch_torrents downloads: nothing unless a test fills in `files`,
    so links reach qB as URLs and no test touches the network."""
    files: dict[str, bytes] = {}
    calls: list[list[str]] = []

    def fake(urls, *, deadline_s):
        calls.append(list(urls))
        return {u: files[u] for u in urls if u in files}

    monkeypatch.setattr(stats_mod, "fetch_torrents", fake)
    return SimpleNamespace(files=files, calls=calls)


class FakeQbt:
    """Stand-in for QbtSession: records every call, answers from canned data."""

    def __init__(self, rules=None, feed_tree=None, torrents=None,
                 add_reply="Ok.", auth_failures=0):
        self.rules = rules if rules is not None else {}
        self.feed_tree = feed_tree if feed_tree is not None else {}
        self.torrents = torrents if torrents is not None else []
        self.add_reply = add_reply
        self.auth_failures = auth_failures
        self.posts: list[tuple[str, dict]] = []
        self.multiparts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def _maybe_fail(self):
        if self.auth_failures > 0:
            self.auth_failures -= 1
            raise QbtAuthError("HTTP 403")

    def get_json(self, endpoint, **params):
        self.gets.append((endpoint, params))
        self._maybe_fail()
        if endpoint == "/api/v2/rss/rules":
            return self.rules
        if endpoint == "/api/v2/rss/items":
            return self.feed_tree
        if endpoint == "/api/v2/torrents/info":
            return self.torrents
        raise AssertionError(f"unexpected GET {endpoint}")

    def post(self, endpoint, **fields):
        self._maybe_fail()
        self.posts.append((endpoint, fields))
        return self.add_reply if endpoint == "/api/v2/torrents/add" else ""

    def post_multipart(self, endpoint, *, file_field, file_name, file_bytes,
                       file_content_type="application/x-bittorrent", **fields):
        self._maybe_fail()
        self.multiparts.append((endpoint, {
            "file_field": file_field, "file_name": file_name,
            "file_bytes": file_bytes, **fields}))
        return self.add_reply

    def posts_to(self, endpoint):
        return [f for e, f in self.posts if e == endpoint]


class LoginScript:
    """Replacement for login_with_password: hands out scripted outcomes (a
    session, or an exception to raise) in order and records each call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, qbt_cfg, password, **kw):
        self.calls.append({"cfg": qbt_cfg, "password": password, **kw})
        assert self.outcomes, "unexpected login_with_password call"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def make(tmp_path, monkeypatch):
    """Factory: an Anirss over tmp download dirs, wired to a FakeQbt."""

    def _make(qbt=None, *, config=None, outcomes=None, **kw):
        qbt = qbt if qbt is not None else FakeQbt()
        cfg = {
            "downloads": {"save_base": str(tmp_path / "anime"),
                          "hidden_base": str(tmp_path / "hidden"),
                          "movie_path": str(tmp_path / "movies")},
            "endpoint": [NYAA_EP, RSS1_EP],
        }
        for key, val in (config or {}).items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **val}
            else:
                cfg[key] = val
        script = LoginScript(*(outcomes if outcomes is not None else [qbt]))
        monkeypatch.setattr(api_mod, "login_with_password", script)
        ani = Anirss(config=cfg, **kw)
        return SimpleNamespace(
            ani=ani, qbt=qbt, logins=script,
            save_base=str(tmp_path / "anime"),
            hidden_base=str(tmp_path / "hidden"),
            movie_path=str(tmp_path / "movies"), root=tmp_path)

    return _make


def _frieren(n=2, group="SubsPlease", res="1080p"):
    return [Item(f"[{group}] Frieren - {i:02d} ({res}) [ABCD{i:04d}].mkv",
                 f"magnet:?xt=urn:btih:{i:040x}") for i in range(1, n + 1)]


def _bencode(obj) -> bytes:
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return _bencode(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(v) for v in obj) + b"e"
    return b"d" + b"".join(_bencode(k) + _bencode(obj[k]) for k in sorted(obj)) + b"e"


def _torrent(announce="http://tracker.test/announce", name="x"):
    info = {"length": 5, "name": name, "piece length": 16384, "pieces": b"\x01" * 20}
    top = {"info": info}
    if announce:
        top["announce"] = announce
    return _bencode(top), hashlib.sha1(_bencode(info)).hexdigest()


# ------------------------------------------------------- construction/config


def test_config_dict_builds_without_disk_access(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("config dict must not hit load_config")
    monkeypatch.setattr(api_mod, "load_config", boom)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ani = Anirss(config={"qbittorrent": {"tags": ["x"]}})
    assert ani.config["qbittorrent"]["tags"] == ["x"]
    assert ani.config["qbittorrent"]["url"] == DEFAULT_CONFIG["qbittorrent"]["url"]
    assert [e.name for e in ani.endpoints()] == ["nyaa"]
    assert ani.version == anirss_lib.__version__
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "cfg").exists()
    assert not (tmp_path / "state").exists()


def test_empty_config_dict_is_defaults_not_disk(monkeypatch):
    monkeypatch.setattr(api_mod, "load_config",
                        lambda *a, **k: pytest.fail("must not read disk"))
    assert Anirss(config={}).config["downloads"]["movie_path"].startswith(os.sep)


def test_build_config_does_not_mutate_defaults():
    snapshot = json.dumps(DEFAULT_CONFIG, sort_keys=True)
    cfg = build_config({"qbittorrent": {"tags": ["zzz"]},
                        "endpoint": [NYAA_EP]})
    cfg["qbittorrent"]["tags"].append("more")
    cfg["bestfit"]["preferred_groups"].append("mutated")
    assert json.dumps(DEFAULT_CONFIG, sort_keys=True) == snapshot


def test_build_config_expands_user_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = build_config({"downloads": {"save_base": "~/a", "movie_path": "~/m",
                                      "hidden_base": "~/h"}})
    assert cfg["downloads"]["save_base"] == str(tmp_path / "a")
    assert cfg["downloads"]["movie_path"] == str(tmp_path / "m")
    assert cfg["downloads"]["hidden_base"] == str(tmp_path / "h")


def test_endpoint_list_in_config_replaces_default_list():
    ani = Anirss(config={"endpoint": [RSS1_EP, NYAA_EP]})
    assert [e.name for e in ani.endpoints()] == ["rss1", "nyaa"]
    assert ani.endpoint().name == "rss1"          # first = default


def test_legacy_search_section_synthesizes_endpoint():
    ani = Anirss(config={"search": {"nyaa_url": "https://mirror.example/"}})
    assert ani.endpoint().url == "https://mirror.example/"


def test_endpoints_returns_a_copy():
    ani = Anirss(config={})
    ani.endpoints().clear()
    assert len(ani.endpoints()) == 1


def test_unknown_endpoint_is_not_found():
    ani = Anirss(config={"endpoint": [NYAA_EP, RSS1_EP]})
    with pytest.raises(NotFoundError, match="nope.*nyaa, rss1"):
        ani.endpoint("nope")
    assert ani.endpoint("rss1").kind == "rss"


@pytest.mark.parametrize("call", [
    lambda a: a.search("x", endpoint="nope"),
    lambda a: a.analyze("x", _frieren(), endpoint="nope"),
    lambda a: a.refine("x", _frieren(), "best_fit", endpoint="nope"),
    lambda a: a.subscription_spec("x", _frieren(), endpoint="nope"),
])
def test_unknown_endpoint_everywhere(call, monkeypatch):
    monkeypatch.setattr(endpoints_mod, "fetch_items",
                        lambda *a: pytest.fail("no fetch for unknown endpoint"))
    with pytest.raises(NotFoundError):
        call(Anirss(config={}))


@pytest.mark.parametrize("bad", [
    [{"name": "r", "kind": "rss", "url": "https://x.example/"}],          # no {query}
    [{"name": "r", "kind": "gopher", "url": "https://x/{query}"}],        # bad kind
    [{"name": "a b", "kind": "nyaa", "url": "https://x/"}],               # bad name
    [NYAA_EP, NYAA_EP],                                                   # duplicate
    [{"name": "r", "kind": "nyaa"}],                                      # no url
])
def test_bad_endpoint_config_is_config_error(bad):
    with pytest.raises(ConfigError):
        Anirss(config={"endpoint": bad})


def test_load_config_missing_path_writes_nothing(tmp_path):
    path = tmp_path / "newdir" / "config.toml"
    cfg = load_config(path, bootstrap=False)
    assert not path.exists() and not path.parent.exists()
    assert list(tmp_path.iterdir()) == []
    assert cfg["qbittorrent"]["url"] == DEFAULT_CONFIG["qbittorrent"]["url"]
    assert cfg["endpoint"][0]["name"] == "nyaa"


def test_load_config_missing_path_bootstraps_only_when_asked(tmp_path, capsys):
    path = tmp_path / "newdir" / "config.toml"
    load_config(path)                       # CLI default: bootstrap
    assert path.exists()
    assert "wrote default config" in capsys.readouterr().err


def test_load_config_strict_bad_toml_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[qbittorrent\nurl = ")
    with pytest.raises(ConfigError, match="bad config"):
        load_config(path, bootstrap=False, strict=True)


def test_load_config_lenient_bad_toml_warns_and_uses_defaults(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("not = [valid")
    cfg = load_config(path, bootstrap=False)
    assert "bad config" in capsys.readouterr().err
    assert cfg["qbittorrent"]["url"] == DEFAULT_CONFIG["qbittorrent"]["url"]


def test_anirss_from_missing_config_path_is_defaults(tmp_path):
    ani = Anirss(config_path=tmp_path / "nope" / "config.toml")
    assert ani.endpoint().name == "nyaa"
    assert not (tmp_path / "nope").exists()


def test_anirss_default_path_is_side_effect_free(tmp_path):
    # isolate_state points CONFIG_PATH at a missing tmp file
    Anirss()
    assert not (tmp_path / "cfg").exists()


def test_anirss_reads_config_path_and_is_strict(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text('[[endpoint]]\nname = "e"\nkind = "rss"\n'
                    'url = "https://e.example/?q={query}"\n')
    assert Anirss(config_path=str(good)).endpoint().name == "e"
    bad = tmp_path / "bad.toml"
    bad.write_text("= = =")
    with pytest.raises(ConfigError):
        Anirss(config_path=bad)


def test_error_hierarchy():
    for cls in (ConfigError, ValidationError, NotFoundError, FetchError, QbtError):
        assert issubclass(cls, AnirssError)
    assert issubclass(QbtAuthError, QbtError)


# ------------------------------------------------------------------- search


@pytest.fixture
def fetcher(monkeypatch):
    """Scripted fetch_items: name -> list[Item] | Exception."""
    class F:
        def __init__(self):
            self.plan: dict = {}
            self.calls: list[tuple[str, str]] = []

        def __call__(self, ep, query):
            self.calls.append((ep.name, query))
            out = self.plan.get(ep.name, [])
            if isinstance(out, Exception):
                raise out
            return list(out)

    f = F()
    monkeypatch.setattr(endpoints_mod, "fetch_items", f)
    return f


@pytest.fixture
def ani3():
    return Anirss(config={"endpoint": [NYAA_EP, RSS1_EP, RSS2_EP]})


def test_search_active_hit(ani3, fetcher):
    fetcher.plan["nyaa"] = _frieren(2)
    res = ani3.search("  Frieren 1080p ")
    assert isinstance(res, SearchResult)
    assert res.query == "Frieren 1080p"
    assert res.endpoint == "nyaa"
    assert len(res.items) == 2
    assert all(it.source == "" for it in res.items)
    assert res.tried == [{"endpoint": "nyaa", "status": "ok", "count": 2,
                          "error": None}]
    assert res.fell_back is False
    assert fetcher.calls == [("nyaa", "Frieren 1080p")]


def test_search_falls_back_when_active_is_empty(ani3, fetcher):
    fetcher.plan.update(nyaa=[], rss1=FetchError("boom"), rss2=_frieren(3))
    res = ani3.search("Frieren")
    assert res.endpoint == "rss2"
    assert [it.source for it in res.items] == ["rss2"] * 3
    assert [t["endpoint"] for t in res.tried] == ["nyaa", "rss1", "rss2"]
    assert [t["status"] for t in res.tried] == ["empty", "unreachable", "ok"]
    assert [t["count"] for t in res.tried] == [0, 0, 3]
    assert res.tried[1]["error"] == "boom"
    assert res.tried[0]["error"] is None and res.tried[2]["error"] is None
    assert res.fell_back is True
    assert [c[0] for c in fetcher.calls] == ["nyaa", "rss1", "rss2"]


def test_search_fallback_stops_at_first_hit(ani3, fetcher):
    fetcher.plan.update(nyaa=FetchError("down"), rss1=_frieren(1), rss2=_frieren(5))
    res = ani3.search("Frieren")
    assert res.endpoint == "rss1" and len(res.items) == 1
    assert [c[0] for c in fetcher.calls] == ["nyaa", "rss1"]


def test_search_explicit_endpoint_goes_first(ani3, fetcher):
    fetcher.plan.update(rss1=[], nyaa=[], rss2=_frieren(1))
    res = ani3.search("Frieren", endpoint="rss1")
    assert [c[0] for c in fetcher.calls] == ["rss1", "nyaa", "rss2"]
    assert res.endpoint == "rss2"
    assert res.items[0].source == "rss2"


def test_search_explicit_endpoint_hit_has_no_source(ani3, fetcher):
    fetcher.plan["rss1"] = _frieren(1)
    res = ani3.search("Frieren", endpoint="rss1")
    assert res.endpoint == "rss1" and res.items[0].source == ""


def test_search_fallback_off_returns_empty_without_trying_others(ani3, fetcher):
    fetcher.plan.update(nyaa=[], rss1=_frieren(2))
    res = ani3.search("Frieren", fallback=False)
    assert res.items == [] and res.endpoint == "nyaa"
    assert len(res.tried) == 1 and res.tried[0]["status"] == "empty"
    assert res.fell_back is False
    assert fetcher.calls == [("nyaa", "Frieren")]


def test_search_fallback_off_raises_when_active_unreachable(ani3, fetcher):
    fetcher.plan.update(nyaa=FetchError("down"), rss1=_frieren(2))
    with pytest.raises(FetchError, match="down"):
        ani3.search("Frieren", fallback=False)
    assert len(fetcher.calls) == 1


def test_search_all_unreachable_raises_fetch_error(ani3, fetcher):
    fetcher.plan.update(nyaa=FetchError("e1"), rss1=FetchError("e2"),
                        rss2=FetchError("e3"))
    with pytest.raises(FetchError) as exc:
        ani3.search("Frieren")
    assert all(m in str(exc.value) for m in ("e1", "e2", "e3"))


def test_search_empty_everywhere_is_an_empty_result(ani3, fetcher):
    res = ani3.search("Frieren")
    assert res.items == [] and res.endpoint == "nyaa"
    assert [t["status"] for t in res.tried] == ["empty"] * 3
    assert res.fell_back is False


def test_search_mixed_unreachable_and_empty_does_not_raise(ani3, fetcher):
    fetcher.plan.update(nyaa=[], rss1=FetchError("x"), rss2=[])
    res = ani3.search("Frieren")
    assert res.items == [] and res.endpoint == "nyaa"
    assert [t["status"] for t in res.tried] == ["empty", "unreachable", "empty"]


@pytest.mark.parametrize("q", ["", "   ", "\t\n", None])
def test_search_empty_query_is_validation_error(ani3, fetcher, q):
    with pytest.raises(ValidationError):
        ani3.search(q)
    assert fetcher.calls == []


def test_search_enrich_runs_after_source_tagging(ani3, fetcher, monkeypatch):
    seen = []

    def fake_enrich(items, deadline_s=0):
        seen.append(([it.source for it in items], deadline_s))
        return [it._replace(seeders=9) for it in items]

    monkeypatch.setattr(stats_mod, "enrich", fake_enrich)
    fetcher.plan.update(nyaa=[], rss1=_frieren(2))
    res = ani3.search("Frieren", enrich=True)
    assert seen == [(["rss1", "rss1"], 20.0)]
    assert [it.seeders for it in res.items] == [9, 9]
    # without the flag nothing is scraped
    seen.clear()
    ani3.search("Frieren")
    assert seen == []


def test_fetch_feed_validates_scheme_and_parses(monkeypatch):
    ani = Anirss(config={})
    monkeypatch.setattr(api_mod, "fetch_rss", lambda url: _frieren(1))
    assert len(ani.fetch_feed("https://f.example/rss")) == 1
    for bad in ("ftp://x", "file:///etc/passwd", "", None):
        with pytest.raises(ValidationError):
            ani.fetch_feed(bad)


def test_top_and_best_item_helpers():
    ani = Anirss(config={})
    items = [Item("a", "l1", downloads=1), Item("b", "l2", downloads=50),
             Item("c", "l3")]
    assert [it.title for it in ani.top(items, 2)] == ["b", "a"]
    assert ani.best_item([]) is None
    assert ani.best_item(_frieren(2)) is not None


# ------------------------------------------------------------ refine: apply

ERAI_EP = Endpoint(name="erai", kind="rss", url="https://erai.example/feed/?s={query}")
NYAA = Endpoint(name="nyaa", kind="nyaa", url="https://nyaa.example/")
GENERIC_RSS = Endpoint(name="rss1", kind="rss", url="https://rss1.example/f?q={query}")
BESTFIT = DEFAULT_CONFIG["bestfit"]

TUI_ITEMS = [
    Item("Show - 01 [1080p][Hevc]", "l1"),
    Item("Show - 02 [1080p][Hevc]", "l2"),
    Item("Show - 03 [1080p][Avc]", "l3"),
]


def _apply(ep, query, items, op, fetch=None, **kw):
    return refinement.apply(ep, query, items, op, bestfit_cfg=BESTFIT,
                            fetch=fetch, **kw)


class Fetch:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def __call__(self, ep, query):
        self.calls.append((ep.name, query))
        if isinstance(self.result, Exception):
            raise self.result
        return list(self.result)


def test_apply_nyaa_tokens_refetches_with_rebuilt_query():
    fetch = Fetch(TUI_ITEMS[:2])
    out = _apply(NYAA, "Show 1080p", TUI_ITEMS, "tokens", fetch, tokens=["Hevc"])
    assert out.applied and out.mode == "refetch" and out.reason is None
    assert out.query == "Show 1080p Hevc"
    assert out.items == TUI_ITEMS[:2]
    assert out.previous_count == 3 and out.delta == -1
    assert fetch.calls == [("nyaa", "Show 1080p Hevc")]


def test_apply_nyaa_zero_results_keeps_inputs():
    fetch = Fetch([])
    out = _apply(NYAA, "Show 1080p", TUI_ITEMS, "tokens", fetch, tokens=["Hevc"])
    assert out.applied is False and out.reason == "zero_results"
    assert out.query == "Show 1080p" and out.items is TUI_ITEMS
    assert out.mode == "refetch" and out.delta == 0


@pytest.mark.parametrize("op,kw", [
    ("tokens", {"tokens": ["Hevc"]}),
    ("term", {"term": "Hevc"}),
    ("exclude", {"term": "Avc"}),
])
def test_apply_nyaa_zero_results_for_each_op(op, kw):
    out = _apply(NYAA, "Show 1080p", TUI_ITEMS, op, Fetch([]), **kw)
    assert (out.applied, out.reason) == (False, "zero_results")
    assert out.query == "Show 1080p" and out.items is TUI_ITEMS


def test_apply_nyaa_refetch_error_propagates():
    with pytest.raises(FetchError, match="down"):
        _apply(NYAA, "Show 1080p", TUI_ITEMS, "term", Fetch(FetchError("down")),
               term="Hevc")


def test_apply_nyaa_term_and_exclude_queries():
    items = [*TUI_ITEMS, Item("Show - 04 two words", "l4")]
    fetch = Fetch(items[:1])
    out = _apply(NYAA, "Show", items, "term", fetch, term="  two words ")
    assert out.query == 'Show "two words"'
    out = _apply(NYAA, "Show", items, "exclude", fetch, term="-Hevc")
    assert out.query == "Show -Hevc"
    out = _apply(NYAA, "Show", items, "exclude", fetch, term="a b")
    assert out.query == 'Show -"a b"'
    assert [c[1] for c in fetch.calls] == ['Show "two words"', "Show -Hevc",
                                           'Show -"a b"']


def test_apply_nyaa_enrich_only_applies_to_refetched_items():
    marked = []

    def enrich(items):
        marked.append(len(items))
        return [it._replace(seeders=1) for it in items]

    out = _apply(NYAA, "Show", TUI_ITEMS, "term", Fetch(TUI_ITEMS[:2]),
                 term="Hevc", enrich=enrich)
    assert marked == [2] and [it.seeders for it in out.items] == [1, 1]
    marked.clear()
    _apply(NYAA, "Show", TUI_ITEMS, "term", Fetch([]), term="Hevc", enrich=enrich)
    assert marked == []                       # zero results: nothing to enrich


def test_apply_default_fetch_is_endpoints_fetch_items(monkeypatch):
    calls = []
    monkeypatch.setattr(endpoints_mod, "fetch_items",
                        lambda ep, q: calls.append(q) or list(TUI_ITEMS[:1]))
    out = _apply(NYAA, "Show", TUI_ITEMS, "term", term="Hevc")
    assert calls == ["Show Hevc"] and out.applied


def test_apply_rss_tokens_filters_client_side():
    fetch = Fetch(pytest.fail)
    out = _apply(GENERIC_RSS, "Show 1080p", TUI_ITEMS, "tokens", fetch,
                 tokens=["Hevc"])
    assert out.applied and out.mode == "client"
    assert [it.link for it in out.items] == ["l1", "l2"]
    assert out.query == "Show 1080p Hevc"
    assert out.delta == -1 and fetch.calls == []


def test_apply_rss_tokens_noop_and_empty_tokens():
    out = _apply(GENERIC_RSS, "Show 1080p", TUI_ITEMS, "tokens", tokens=["Nope"])
    assert (out.applied, out.reason) == (False, "noop")
    assert out.items is TUI_ITEMS and out.query == "Show 1080p"
    # a token every item carries changes nothing either
    out = _apply(GENERIC_RSS, "Show", TUI_ITEMS, "tokens", tokens=["1080p"])
    assert (out.applied, out.reason) == (False, "noop")
    for tokens in (None, [], [""]):
        with pytest.raises(ValidationError, match="tokens"):
            _apply(GENERIC_RSS, "Show", TUI_ITEMS, "tokens", tokens=tokens)


def test_apply_rss_tokens_match_erai_metadata():
    items = [Item("Show - 01 [1080p]", "l1", linktype="Torrent"),
             Item("Show - 02 [1080p]", "l2", linktype="Torrent"),
             Item("Show - 03 [1080p]", "l3", linktype="Magnet")]
    out = _apply(ERAI_EP, "Show 1080p", items, "tokens", tokens=["Torrent"])
    assert [it.link for it in out.items] == ["l1", "l2"]


def test_apply_rss_term_filters_and_reports_no_match():
    out = _apply(GENERIC_RSS, "Show", TUI_ITEMS, "term", term="avc")
    assert out.applied and [it.link for it in out.items] == ["l3"]
    assert out.query == "Show avc" and out.mode == "client"
    out = _apply(GENERIC_RSS, "Show", TUI_ITEMS, "term", term="zzz")
    assert (out.applied, out.reason) == (False, "no_match")
    assert out.items is TUI_ITEMS and out.query == "Show"


def test_apply_nyaa_term_no_match_never_refetches():
    fetch = Fetch(pytest.fail)
    out = _apply(NYAA, "Show", TUI_ITEMS, "term", fetch, term="zzz")
    assert (out.applied, out.reason) == (False, "no_match")
    assert fetch.calls == []


@pytest.mark.parametrize("ep", [NYAA, GENERIC_RSS])
@pytest.mark.parametrize("op,term", [("term", ""), ("term", "   "),
                                     ("exclude", ""), ("exclude", "-"),
                                     ("exclude", "---")])
def test_apply_empty_term(ep, op, term):
    fetch = Fetch(pytest.fail)
    out = _apply(ep, "Show", TUI_ITEMS, op, fetch, term=term)
    assert (out.applied, out.reason) == (False, "empty_term")
    assert out.query == "Show" and out.items is TUI_ITEMS
    assert fetch.calls == []


@pytest.mark.parametrize("term,want", [(" -Hevc", "Show -Hevc"), (" - ", "empty_term")])
def test_apply_exclude_strips_whitespace_before_dash(term, want):
    out = _apply(NYAA, "Show", TUI_ITEMS, "exclude", Fetch(TUI_ITEMS[:1]), term=term)
    assert (out.query if out.applied else out.reason) == want


def test_apply_rss_exclude_filters_and_strips_dash():
    out = _apply(GENERIC_RSS, "Show", TUI_ITEMS, "exclude", term="-HEVC")
    assert out.applied and [it.link for it in out.items] == ["l3"]
    assert out.query == "Show -HEVC"


def test_apply_rss_exclude_everything_is_zero_results():
    out = _apply(GENERIC_RSS, "Show", TUI_ITEMS, "exclude", term="Show")
    assert (out.applied, out.reason) == (False, "zero_results")
    assert out.items is TUI_ITEMS


def test_apply_rss_best_fit_filters_to_profile_and_noops():
    items = [Item("[Erai-raws] Show - 01 [1080p][HEVC]", "l1"),
             Item("[Erai-raws] Show - 02 [1080p][HEVC]", "l2"),
             Item("[Other] Show - 03 [720p]", "l3")]
    out = _apply(GENERIC_RSS, "Show", items, "best_fit")
    assert out.applied and out.mode == "client"
    assert [it.link for it in out.items] == ["l1", "l2"]
    assert out.query == refinement.best_fit_info("Show", items, BESTFIT)["query"]
    # nothing to narrow: every release already shares the profile
    out = _apply(GENERIC_RSS, "Show", items[:2], "best_fit")
    assert (out.applied, out.reason) == (False, "noop")


def test_apply_best_fit_noop_when_query_already_pinned():
    items = [Item("[Erai-raws] Show - 01 [1080p][HEVC]", "l1"),
             Item("[Other] Show - 03 [720p]", "l3")]
    pinned = refinement.best_fit_info("Show", items, BESTFIT)["query"]
    fetch = Fetch(pytest.fail)
    out = _apply(NYAA, pinned, items, "best_fit", fetch)
    assert (out.applied, out.reason) == (False, "noop")
    assert fetch.calls == []


def test_apply_nyaa_best_fit_refetches_with_best_query():
    items = [Item("[Erai-raws] Show - 01 [1080p][HEVC]", "l1"),
             Item("[Other] Show - 03 [720p]", "l3")]
    want = refinement.best_fit_info("Show", items, BESTFIT)["query"]
    fetch = Fetch(items[:1])
    out = _apply(NYAA, "Show", items, "best_fit", fetch)
    assert out.applied and out.query == want
    assert fetch.calls == [("nyaa", want)]


def test_apply_best_fit_on_empty_items_is_noop():
    out = _apply(NYAA, "Show", [], "best_fit", Fetch(pytest.fail))
    assert (out.applied, out.reason) == (False, "noop")


@pytest.mark.parametrize("op", ["", "nope", "TOKENS", "custom", "show_all"])
def test_apply_unknown_op(op):
    with pytest.raises(ValidationError, match="unknown refine op"):
        _apply(NYAA, "Show", TUI_ITEMS, op)


def test_op_types_constant():
    assert refinement.OP_TYPES == ("tokens", "term", "exclude", "best_fit")


def test_is_client_side():
    assert refinement.is_client_side(GENERIC_RSS)
    assert refinement.is_client_side(ERAI_EP)
    assert not refinement.is_client_side(NYAA)


def test_anirss_refine_wraps_apply(monkeypatch):
    ani = Anirss(config={"endpoint": [NYAA_EP, RSS1_EP]})
    monkeypatch.setattr(endpoints_mod, "fetch_items",
                        lambda ep, q: pytest.fail("rss endpoint must not refetch"))
    out = ani.refine("Show 1080p", TUI_ITEMS, "tokens", endpoint="rss1",
                     tokens=["Avc"])
    assert out.applied and [it.link for it in out.items] == ["l3"]
    # enrich=True routes through Anirss.enrich on refetched items only
    monkeypatch.setattr(stats_mod, "enrich",
                        lambda items, deadline_s=0: [i._replace(seeders=7) for i in items])
    monkeypatch.setattr(endpoints_mod, "fetch_items",
                        lambda ep, q: list(TUI_ITEMS[:1]))
    out = ani.refine("Show 1080p", TUI_ITEMS, "term", term="Hevc", enrich=True)
    assert out.items[0].seeders == 7
    with pytest.raises(ValidationError):
        ani.refine("Show", TUI_ITEMS, "bogus")


# ----------------------------------- parity with the TUI refine loop (fzf)


def _tui(monkeypatch, ep, pick, items, others=()):
    """Drive refine.refine() through one `pick` and return (query, items)."""
    from anirss_lib.types import PICK_DONE
    monkeypatch.setattr(endpoints_mod, "fetch_items", lambda e, q: list(items))
    monkeypatch.setattr(stats_mod, "enrich", lambda it, **k: it)
    seq = iter([pick, PICK_DONE])
    monkeypatch.setattr(refine_mod, "pick_group", lambda *a, **k: next(seq))
    monkeypatch.setattr(refine_mod, "prompt", lambda *a, **k: pick.tokens[0]
                        if pick.tokens else "Hevc")
    state = EndpointState([ep, *others])
    query, selected, status = refine_mod.refine("Show 1080p", items, state, BESTFIT)
    assert status == "done"
    return query, selected


META_ITEMS = [Item("Show - 01 [1080p]", "l1", linktype="Torrent"),
              Item("Show - 02 [1080p]", "l2", linktype="Torrent"),
              Item("Show - 03 [1080p]", "l3", linktype="Magnet")]

PARITY = [
    # id, endpoint, items, pick, apply-kwargs
    ("rss-tokens", ERAI_EP, TUI_ITEMS, Pick("tokens", ["Hevc"]),
     {"op": "tokens", "tokens": ["Hevc"]}),
    ("rss-tokens-noop", ERAI_EP, TUI_ITEMS, Pick("tokens", ["Nonexistent"]),
     {"op": "tokens", "tokens": ["Nonexistent"]}),
    ("rss-tokens-meta", ERAI_EP, META_ITEMS, Pick("tokens", ["Torrent"]),
     {"op": "tokens", "tokens": ["Torrent"]}),
    ("rss-custom", ERAI_EP, TUI_ITEMS, Pick("custom", ["Hevc"]),
     {"op": "term", "term": "Hevc"}),
    ("rss-custom-nomatch", ERAI_EP, TUI_ITEMS, Pick("custom", ["zzz"]),
     {"op": "term", "term": "zzz"}),
    ("rss-exclude", ERAI_EP, TUI_ITEMS, Pick("exclude", []),
     {"op": "exclude", "term": "Hevc"}),
    ("rss-best-fit", ERAI_EP, TUI_ITEMS, Pick("best_fit", []),
     {"op": "best_fit"}),
    ("nyaa-tokens", NYAA, TUI_ITEMS, Pick("tokens", ["Hevc"]),
     {"op": "tokens", "tokens": ["Hevc"]}),
    ("nyaa-custom", NYAA, TUI_ITEMS, Pick("custom", ["Hevc"]),
     {"op": "term", "term": "Hevc"}),
    ("nyaa-exclude", NYAA, TUI_ITEMS, Pick("exclude", []),
     {"op": "exclude", "term": "Hevc"}),
    ("nyaa-best-fit", NYAA, TUI_ITEMS, Pick("best_fit", []),
     {"op": "best_fit"}),
]


@pytest.mark.parametrize("ep,items,pick,kw",
                         [p[1:] for p in PARITY], ids=[p[0] for p in PARITY])
def test_apply_matches_tui_refine_loop(monkeypatch, ep, items, pick, kw):
    tui_query, tui_items = _tui(monkeypatch, ep, pick, items,
                                others=[NYAA] if ep is not NYAA else [])
    # "Show 1080p" already carries a resolution, so auto-resolution is a no-op
    # and the TUI starts from exactly the inputs apply() receives.
    out = refinement.apply(ep, "Show 1080p", items, kw.pop("op"),
                           bestfit_cfg=BESTFIT,
                           fetch=lambda e, q: list(items), **kw)
    assert (out.query, out.items) == (tui_query, tui_items)


# --------------------------------------------------------------- analyze


def test_analyze_applies_auto_resolution():
    ani = Anirss(config={})
    items = _frieren(2, res="720p") + _frieren(2, res="1080p")
    res = ani.analyze("Frieren", items)
    assert res["query"] == "Frieren 1080p"
    assert res["auto_resolution"] == {"applied": True, "token": "1080p",
                                      "skipped_missing": 0}
    assert res["items"] == items[2:]
    assert res["endpoint"] == "nyaa" and res["client_side"] is False


def test_analyze_skips_auto_resolution_when_a_title_lacks_one():
    ani = Anirss(config={})
    items = [*_frieren(2), Item("[Group] Frieren - 03", "l3")]
    res = ani.analyze("Frieren", items)
    assert res["query"] == "Frieren" and res["items"] == items
    assert res["auto_resolution"] == {"applied": False, "token": "",
                                      "skipped_missing": 1}


def test_analyze_leaves_query_with_resolution_alone():
    ani = Anirss(config={})
    items = _frieren(1, res="720p") + _frieren(1, res="1080p")
    res = ani.analyze(" Frieren 720p ", items)
    assert res["query"] == "Frieren 720p" and res["items"] == items
    assert res["auto_resolution"]["applied"] is False
    assert res["auto_resolution"]["skipped_missing"] == 0


def test_analyze_auto_resolution_can_be_disabled():
    ani = Anirss(config={})
    items = _frieren(1, res="720p") + _frieren(1, res="1080p")
    res = ani.analyze("Frieren", items, auto_resolution=False)
    assert res["query"] == "Frieren" and res["items"] == items
    assert res["auto_resolution"]["applied"] is False


def test_analyze_groups_carry_member_indexes():
    ani = Anirss(config={})
    items = [Item("[SubsPlease] Frieren - 01 (1080p) [A1].mkv", "l1"),
             Item("[SubsPlease] Frieren - 02 (1080p) [A2].mkv", "l2"),
             Item("[Erai-raws] Frieren - 02 [1080p][HEVC].mkv", "l3"),
             Item("[Erai-raws] Frieren - 03 [1080p][HEVC].mkv", "l4"),
             Item("[Judas] Frieren - 04 [1080p].mkv", "l5")]
    res = ani.analyze("Frieren", items)
    groups = res["groups"]
    assert groups, "expected refinable groups"
    by_label = {g["label"]: g for g in groups}
    assert by_label["[SubsPlease]"]["members"] == [0, 1]
    # tokens with identical membership are merged into one group
    assert by_label["[Erai-raws]+HEVC"]["members"] == [2, 3]
    assert by_label["[Erai-raws]+HEVC"]["tokens"] == ["[Erai-raws]", "HEVC"]
    for g in groups:
        assert set(g) == {"label", "tokens", "member_count", "has_poster", "members"}
        assert g["members"] == sorted(g["members"])
        assert len(g["members"]) == g["member_count"]
        assert all(0 <= i < len(res["items"]) for i in g["members"])
        assert 1 < g["member_count"] < len(res["items"])
    # indexes are into the post-auto-resolution items
    assert by_label["[SubsPlease]"]["has_poster"] is True


def test_analyze_member_indexes_follow_filtered_items():
    ani = Anirss(config={})
    items = [Item("[A] Show - 01 (720p)", "x0"),
             Item("[A] Show - 02 (1080p)", "x1"),
             Item("[A] Show - 03 (1080p) [v2]", "x2"),
             Item("[B] Show - 04 (1080p) [v2]", "x3")]
    res = ani.analyze("Show", items)
    assert [it.link for it in res["items"]] == ["x1", "x2", "x3"]
    members = {g["label"]: g["members"] for g in res["groups"]}
    assert members["[A]"] == [0, 1]


def test_group_members_helper():
    items = _frieren(3)
    groups = refinement.compute_groups(items)
    for g in groups:
        assert refinement.group_members(items, g) == sorted(
            refinement.group_members(items, g))
    assert refinement.group_members(items, SimpleNamespace(tokens=[])) == []


def test_analyze_best_fit_dict():
    ani = Anirss(config={})
    items = [Item("[Erai-raws] Frieren - 02 [1080p][HEVC].mkv", "l1"),
             Item("[SubsPlease] Frieren - 01 (720p) [AB].mkv", "l2")]
    res = ani.analyze("Frieren", items)
    best = res["best_fit"]
    assert set(best) == {"available", "query", "item", "profile"}
    assert best["available"] is True
    assert best["item"] in res["items"]
    assert best["query"] and best["query"] != res["query"]
    assert "[Erai-raws]" in best["profile"]
    # when the query already is the best-fit query there is nothing to do
    again = ani.analyze(best["query"], res["items"])
    assert again["best_fit"]["available"] is False
    assert again["best_fit"]["query"] == ""


def test_analyze_client_side_flag():
    ani = Anirss(config={"endpoint": [NYAA_EP, RSS1_EP]})
    assert ani.analyze("x", _frieren(2), endpoint="rss1")["client_side"] is True
    assert ani.analyze("x", _frieren(2))["client_side"] is False


def test_analyze_result_is_jsonable():
    res = Anirss(config={}).analyze("Frieren", _frieren(3))
    json.dumps(to_jsonable(res))


def test_analyze_empty_items_does_not_crash():
    res = Anirss(config={}).analyze("Frieren", [])
    assert res["items"] == [] and res["groups"] == []


# ------------------------------------------------------------ subscribe


def _setrule(ft):
    return json.loads(ft.posts_to("/api/v2/rss/setRule")[0]["ruleDef"])


def test_subscribe_nyaa_registers_feed_and_rule(make):
    e = make()
    res = e.ani.subscribe("Frieren 1080p", items=_frieren(2))
    assert res.name == "Frieren"                       # show name of first result
    assert res.save_path == os.path.join(e.save_base, "Frieren")
    assert res.feed_url.startswith("https://nyaa.example/?page=rss")
    assert "q=Frieren+1080p" in res.feed_url
    assert res.must_contain == "" and res.must_not_contain == ""
    assert res.tags == ["Anime"]
    [feed] = e.qbt.posts_to("/api/v2/rss/addFeed")
    assert feed == {"url": res.feed_url, "path": "Frieren"}
    rule = _setrule(e.qbt)
    assert e.qbt.posts_to("/api/v2/rss/setRule")[0]["ruleName"] == "Frieren"
    assert rule["savePath"] == res.save_path
    assert rule["affectedFeeds"] == [res.feed_url]
    assert rule["torrentParams"] == {"save_path": res.save_path,
                                     "use_auto_tmm": False, "tags": ["Anime"]}
    assert len(e.logins.calls) == 1
    assert e.logins.calls[0]["autostart"] is False
    assert e.logins.calls[0]["notify"] is None
    assert e.logins.calls[0]["persist"] is False


def test_subscribe_hidden_uses_hidden_base(make):
    e = make()
    res = e.ani.subscribe("Frieren", items=_frieren(1), hidden=True)
    assert res.save_path == os.path.join(e.hidden_base, "Frieren")
    assert _setrule(e.qbt)["savePath"] == res.save_path


def test_subscribe_explicit_name_and_default_endpoint_selection(make):
    e = make()
    res = e.ani.subscribe("Frieren", items=_frieren(1), name="  My Name ")
    assert res.name == "My Name"
    assert res.save_path == os.path.join(e.save_base, "My Name")


def test_subscribe_name_falls_back_to_query_when_title_has_no_show_name(make):
    e = make()
    res = e.ani.subscribe("Some Query", items=[Item("", "l1")])
    assert res.name == "Some Query"


def test_subscribe_rss_exclusion_becomes_must_not_contain(make):
    e = make()
    res = e.ani.subscribe('Frieren 1080p -HEVC -"dual audio"', endpoint="rss1",
                          items=_frieren(2))
    assert res.must_not_contain == "HEVC|dual audio"
    assert res.feed_url == "https://rss1.example/f?q=Frieren+1080p"
    rule = _setrule(e.qbt)
    assert rule["mustNotContain"] == "HEVC|dual audio"
    assert rule["affectedFeeds"] == [res.feed_url]


def test_subscribe_nyaa_keeps_exclusion_in_feed_url(make):
    e = make()
    res = e.ani.subscribe("Frieren -HEVC", items=_frieren(2))
    assert res.must_not_contain == ""
    assert "-HEVC" in res.feed_url
    assert _setrule(e.qbt)["mustNotContain"] == ""


def test_subscription_spec_has_no_qbt_side_effects(make):
    e = make()
    spec = e.ani.subscription_spec("Frieren -HEVC", _frieren(1), endpoint="rss1")
    assert spec.must_not_contain == "HEVC" and spec.endpoint_name == "rss1"
    assert e.logins.calls == [] and e.qbt.posts == []


def test_subscribe_name_collision_from_other_endpoint_gets_suffix(make):
    qbt = FakeQbt(rules={"Frieren": {"affectedFeeds": ["https://nyaa.example/old"]}})
    e = make(qbt)
    res = e.ani.subscribe("Frieren", endpoint="rss1", items=_frieren(1))
    assert res.name == "Frieren @rss1"
    assert res.save_path == os.path.join(e.save_base, "Frieren @rss1")
    assert e.qbt.posts_to("/api/v2/rss/addFeed")[0]["path"] == "Frieren @rss1"


def test_subscribe_same_feed_keeps_name(make):
    feed = "https://rss1.example/f?q=Frieren"
    qbt = FakeQbt(rules={"Frieren": {"affectedFeeds": [feed]}})
    e = make(qbt)
    assert e.ani.subscribe("Frieren", endpoint="rss1", items=_frieren(1)).name == "Frieren"


def test_subscribe_tags_cleaned_and_empty_tags_omit_params(make):
    e = make(config={"qbittorrent": {"tags": ["a", " b,c ", "a", ""]}})
    res = e.ani.subscribe("Frieren", items=_frieren(1))
    assert res.tags == ["a", "b", "c"]
    assert _setrule(e.qbt)["torrentParams"]["tags"] == ["a", "b", "c"]
    e = make(config={"qbittorrent": {"tags": []}})
    res = e.ani.subscribe("Frieren", items=_frieren(1))
    assert res.tags == [] and "torrentParams" not in _setrule(e.qbt)


def test_subscribe_validates_before_logging_in(make):
    e = make()
    for q in ("", "  ", None):
        with pytest.raises(ValidationError):
            e.ani.subscribe(q, items=_frieren(1))
    with pytest.raises(NotFoundError, match="no results"):
        e.ani.subscribe("Frieren", items=[])
    with pytest.raises(ValidationError):
        e.ani.subscribe("Frieren", items=_frieren(1), name="../evil")
    assert e.logins.calls == [] and e.qbt.posts == []


def test_subscribe_without_items_searches_active_endpoint_only(make, fetcher):
    e = make()
    fetcher.plan.update(nyaa=_frieren(2), rss1=_frieren(9))
    res = e.ani.subscribe("Frieren")
    assert fetcher.calls == [("nyaa", "Frieren")]
    assert res.name == "Frieren"
    fetcher.calls.clear()
    fetcher.plan["nyaa"] = []
    with pytest.raises(NotFoundError):
        e.ani.subscribe("Frieren")
    assert fetcher.calls == [("nyaa", "Frieren")]    # no fallback to rss1


def test_subscribe_feed_url_as_is(make, monkeypatch):
    e = make()
    monkeypatch.setattr(api_mod, "fetch_rss", lambda url: _frieren(1))
    res = e.ani.subscribe_feed("https://feed.example/rss.xml")
    assert res.feed_url == "https://feed.example/rss.xml"
    assert res.name == "Frieren" and res.must_not_contain == ""
    monkeypatch.setattr(api_mod, "fetch_rss", lambda url: [])
    with pytest.raises(NotFoundError):
        e.ani.subscribe_feed("https://feed.example/rss.xml")
    with pytest.raises(ValidationError):
        e.ani.subscribe_feed("ftp://feed.example/rss.xml")


# ------------------------------------------- download / movie / upload


BAD_NAMES = ["..", ".", "a/b", "a\\b", "/abs", "", "   ", None, "a\x00b",
             "a\nb", "a\tb", "a\x7fb", "../x"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_validate_name_rejects(name):
    with pytest.raises(ValidationError):
        actions.validate_name(name)


def test_validate_name_accepts_and_strips():
    assert actions.validate_name("  Frieren S2 ") == "Frieren S2"
    assert actions.validate_name("a..b") == "a..b"
    assert actions.validate_name("日本語") == "日本語"


@pytest.mark.parametrize("name", [n for n in BAD_NAMES if n is not None])
def test_every_name_taking_call_validates_before_login(make, name):
    e = make()
    ani = e.ani
    magnet = f"magnet:?xt=urn:btih:{HEX40}"
    for call in (
        lambda: ani.download([magnet], name=name),
        lambda: ani.upload_torrent(_torrent()[0], name=name or "x/y"),
        lambda: ani.torrents(name=name) if name else (_ for _ in ()).throw(
            ValidationError("skip")),
    ):
        with pytest.raises(ValidationError):
            call()
    assert e.logins.calls == []


def test_download_posts_links_into_named_dir(make):
    e = make()
    links = [f"magnet:?xt=urn:btih:{HEX40}", "https://x.example/a.torrent"]
    res = e.ani.download(links, name=" Frieren ")
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["urls"] == "\n".join(links)
    assert fields["savepath"] == os.path.join(e.save_base, "Frieren")
    assert fields["tags"] == "Anime"
    assert (res.count, res.save_path, res.tags) == (
        2, os.path.join(e.save_base, "Frieren"), ["Anime"])
    assert res.hashes == [HEX40]          # remote .torrent hashes aren't known


def test_download_hidden_and_no_tags(make):
    e = make(config={"qbittorrent": {"tags": []}})
    e.ani.download(["https://x.example/a.torrent"], name="X", hidden=True)
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["savepath"] == os.path.join(e.hidden_base, "X")
    assert "tags" not in fields


@pytest.mark.parametrize("link", [
    "file:///etc/passwd", "ftp://x.example/a.torrent", "/tmp/a.torrent",
    "a.torrent", "javascript:alert(1)", "", " https://x.example/a",
    "data:text/plain,hi", "bc://bt/x", 12345, None,
    "https://x.example/a\tb.torrent", "magnet:?xt=urn:btih:x&dn=a b",
    "https://ex ample.com/a.torrent", "http://[::1/a.torrent",
])
def test_download_rejects_non_magnet_non_http_links(make, link):
    e = make()
    with pytest.raises(ValidationError):
        e.ani.download([link], name="X")
    with pytest.raises(ValidationError):
        e.ani.download(["https://ok.example/a.torrent", link], name="X")
    assert e.logins.calls == [] and e.qbt.posts == []


def test_download_requires_links(make):
    e = make()
    with pytest.raises(ValidationError, match="no links"):
        e.ani.download([], name="X")
    assert e.logins.calls == []


def test_download_accepts_http_https_and_magnet(make):
    e = make()
    e.ani.download(["http://a.example/x", "https://a.example/y", "magnet:?xt=1"],
                   name="X")
    assert len(e.qbt.posts_to("/api/v2/torrents/add")) == 1


def test_download_encodes_raw_spaces_and_brackets_in_feed_links(make):
    # t.erai-raws.info puts the release name into its <link> verbatim.
    e = make()
    e.ani.download([
        "https://t.erai-raws.info/Torrent/2026/Spring/Mairimashita/[Erai-raws] "
        "Mairimashita Iruma-kun 4th Season - 24 [1080p CR WEB-DL AVC AAC]"
        "[MultiSub].mkv.torrent"], name="X")
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["urls"] == (
        "https://t.erai-raws.info/Torrent/2026/Spring/Mairimashita/%5BErai-raws%5D"
        "%20Mairimashita%20Iruma-kun%204th%20Season%20-%2024%20%5B1080p%20CR%20"
        "WEB-DL%20AVC%20AAC%5D%5BMultiSub%5D.mkv.torrent")


@pytest.mark.parametrize("link,sent", [
    ("https://x.example/dl/a%20b.torrent?token=1&q=a+b", None),
    ("https://user:pw@x.example:8443/a.torrent#f", None),
    ("http://[::1]:8080/a b.torrent", "http://[::1]:8080/a%20b.torrent"),
    ("https://x.example/a.torrent?name=a b", "https://x.example/a.torrent?name=a%20b"),
])
def test_download_leaves_valid_url_parts_alone(make, link, sent):
    e = make()
    e.ani.download([link], name="X")
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["urls"] == (sent or link)


def test_download_rejects_newline_smuggled_urls(make):
    e = make()
    with pytest.raises(ValidationError):
        e.ani.download(["https://a.example/x.torrent\nfile:///etc/passwd"], name="X")
    assert e.qbt.posts == []


def test_download_uploads_fetched_torrents_and_sends_the_rest_as_urls(make, fetched):
    # qB fetches urls= all at once and t.erai-raws.info refuses more than 8
    # at a time, so anirss uploads the .torrent files it could fetch itself.
    a, ha = _torrent(name="a")
    b, hb = _torrent(name="b")
    fetched.files.update({"https://x.example/a%20b.torrent": a,
                          "https://x.example/b.torrent": b})
    magnet = f"magnet:?xt=urn:btih:{HEX40}"
    e = make()
    res = e.ani.download(["https://x.example/a b.torrent", magnet,
                          "https://x.example/b.torrent",
                          "https://x.example/gone.torrent"], name="X")
    assert fetched.calls == [["https://x.example/a%20b.torrent",
                              "https://x.example/b.torrent",
                              "https://x.example/gone.torrent"]]
    ups = [f for _, f in e.qbt.multiparts]
    assert [f["file_bytes"] for f in ups] == [a, b]
    assert [f["file_name"] for f in ups] == ["a b.torrent", "b.torrent"]
    assert {f["savepath"] for f in ups} == {os.path.join(e.save_base, "X")}
    assert {f["tags"] for f in ups} == {"Anime"}
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["urls"] == f"{magnet}\nhttps://x.example/gone.torrent"
    assert res.count == 4 and res.hashes == [ha, HEX40, hb]


def test_download_skips_torrents_qbt_already_has(make, fetched):
    # qB answers a duplicate upload with 409, which would abort the rest.
    a, ha = _torrent(name="a")
    b, hb = _torrent(name="b")
    fetched.files.update({"https://x.example/a.torrent": a,
                          "https://x.example/b.torrent": b,
                          "https://mirror.example/b.torrent": b})
    e = make(FakeQbt(torrents=[{"hash": ha}]))
    res = e.ani.download(["https://x.example/a.torrent", "https://x.example/b.torrent",
                          "https://mirror.example/b.torrent"], name="X")
    assert [f["file_bytes"] for _, f in e.qbt.multiparts] == [b]
    assert e.qbt.posts == []
    assert res.count == 3 and res.hashes == [ha, hb, hb]


def test_movie_uploads_a_fetched_torrent(make, fetched):
    data, h = _torrent()
    fetched.files["https://x.example/m.torrent"] = data
    e = make()
    res = e.ani.movie("https://x.example/m.torrent")
    [(_, f)] = e.qbt.multiparts
    assert f["savepath"] == e.movie_path and e.qbt.posts == []
    assert res.hashes == [h]


def test_download_items_defaults_name_from_show_name(make):
    e = make()
    items = _frieren(2)
    res = e.ani.download_items(items)
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["savepath"] == os.path.join(e.save_base, "Frieren")
    assert fields["urls"] == "\n".join(it.link for it in items)
    assert res.count == 2 and len(res.hashes) == 2


def test_download_items_explicit_and_blank_names(make):
    e = make()
    e.ani.download_items(_frieren(1), name="Custom")
    e.ani.download_items(_frieren(1), name="   ")
    paths = [f["savepath"] for f in e.qbt.posts_to("/api/v2/torrents/add")]
    assert paths == [os.path.join(e.save_base, "Custom"),
                     os.path.join(e.save_base, "Frieren")]


def test_download_items_validation(make):
    e = make()
    with pytest.raises(ValidationError, match="no items"):
        e.ani.download_items([])
    with pytest.raises(ValidationError):
        e.ani.download_items(_frieren(1), name="a/b")
    with pytest.raises(ValidationError):
        e.ani.download_items([Item("x", "file:///etc/passwd")], name="x")
    assert e.logins.calls == []


def test_download_items_name_falls_back_when_title_has_no_show(make):
    e = make()
    e.ani.download_items([Item("", "https://x.example/a.torrent")])
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["savepath"] == os.path.join(e.save_base, "anirss")


def test_movie_goes_straight_into_movie_path(make):
    e = make()
    res = e.ani.movie(f"magnet:?xt=urn:btih:{HEX40}")
    [fields] = e.qbt.posts_to("/api/v2/torrents/add")
    assert fields["savepath"] == e.movie_path
    assert fields["urls"].startswith("magnet:")
    assert res.save_path == e.movie_path and res.hashes == [HEX40]


def test_movie_validates_link(make):
    e = make()
    for bad in ("file:///x", "", None):
        with pytest.raises(ValidationError):
            e.ani.movie(bad)
    assert e.logins.calls == []


@pytest.mark.parametrize("reply", ["Fails.", "garbage", '{"failure_count": 2}',
                                   "[1]"])
def test_add_failure_replies_raise_qbt_error(make, reply):
    e = make(FakeQbt(add_reply=reply))
    with pytest.raises(QbtError):
        e.ani.download(["https://x.example/a.torrent"], name="X")


@pytest.mark.parametrize("reply", ["Ok.", "Ok.\n",
                                   '{"success_count":1,"pending_count":0,"failure_count":0}',
                                   '{"success_count":0,"pending_count":1,"failure_count":0}'])
def test_add_success_replies(make, reply):
    e = make(FakeQbt(add_reply=reply))
    assert e.ani.download(["https://x.example/a.torrent"], name="X").count == 1


def test_upload_torrent_hashes_and_posts_multipart(make):
    e = make()
    data, want = _torrent()
    res = e.ani.upload_torrent(data, file_name="My Show.torrent")
    [(endpoint, f)] = e.qbt.multiparts
    assert endpoint == "/api/v2/torrents/add"
    assert f["file_field"] == "torrents" and f["file_bytes"] == data
    assert f["file_name"] == "My Show.torrent"
    assert f["savepath"] == os.path.join(e.save_base, "My Show")
    assert f["tags"] == "Anime"
    assert res.hashes == [want] and res.count == 1
    assert res.save_path == os.path.join(e.save_base, "My Show")


def test_upload_torrent_name_rules(make):
    e = make()
    data, _ = _torrent()
    e.ani.upload_torrent(data, file_name="../../etc/Evil.torrent")
    e.ani.upload_torrent(data, file_name="a.torrent", name="Chosen", hidden=True)
    e.ani.upload_torrent(data, file_name="")
    paths = [f["savepath"] for _, f in e.qbt.multiparts]
    names = [f["file_name"] for _, f in e.qbt.multiparts]
    assert paths == [os.path.join(e.save_base, "Evil"),
                     os.path.join(e.hidden_base, "Chosen"),
                     os.path.join(e.save_base, "file")]
    assert names == ["Evil.torrent", "a.torrent", "file.torrent"]


def test_upload_torrent_empty_bytes_rejected(make):
    e = make()
    with pytest.raises(ValidationError, match="empty"):
        e.ani.upload_torrent(b"", file_name="x.torrent")
    assert e.qbt.multiparts == []


def test_upload_torrent_unparseable_bytes_have_no_hashes(make):
    e = make()
    res = e.ani.upload_torrent(b"not bencode at all", file_name="x.torrent")
    assert res.hashes == [] and res.count == 1


def test_upload_trackerless_torrent_still_reports_hash(make):
    e = make()
    data, want = _torrent(announce=None)
    assert e.ani.upload_torrent(data, file_name="t.torrent").hashes == [want]


def test_upload_failure_reply_raises(make):
    e = make(FakeQbt(add_reply="Fails."))
    with pytest.raises(QbtError):
        e.ani.upload_torrent(_torrent()[0], file_name="x.torrent")


# ------------------------------------------------------------ magnet_hash


def test_magnet_hash_hex_is_lowercased():
    assert actions.magnet_hash(f"magnet:?xt=urn:btih:{HEX40.upper()}&dn=x") == HEX40
    assert actions.magnet_hash(f"magnet:?dn=x&xt=urn:btih:{HEX40}&tr=y") == HEX40


def test_magnet_hash_base32():
    raw = bytes(range(20))
    b32 = base64.b32encode(raw).decode()
    assert len(b32) == 32
    assert actions.magnet_hash(f"magnet:?xt=urn:btih:{b32}") == raw.hex()
    assert actions.magnet_hash(f"magnet:?xt=urn:btih:{b32.lower()}") == raw.hex()


@pytest.mark.parametrize("link", [
    "https://x.example/a.torrent", "", "magnet:?dn=nothing",
    "magnet:?xt=urn:btih:abc123",                   # wrong length
    "magnet:?xt=urn:btih:" + "1" * 32,             # '1' is not base32
    "magnet:?xt=urn:btmh:" + HEX40,                # v2 multihash form
    f"MAGNET:?xt=urn:btih:{HEX40}",                # scheme is case-sensitive
])
def test_magnet_hash_none(link):
    assert actions.magnet_hash(link) is None


def test_magnet_hash_rejects_non_hex_40_chars():
    assert actions.magnet_hash("magnet:?xt=urn:btih:" + "z" * 40) is None


# ------------------------------------------------------ session handling


def test_session_is_created_once_and_reused(make):
    e = make()
    e.ani.subscriptions()
    e.ani.subscriptions()
    assert len(e.logins.calls) == 1


def test_login_receives_password_precedence(make, monkeypatch):
    e = make(qbt_password="explicit")
    e.ani.subscriptions()
    assert e.logins.calls[0]["password"] == "explicit"
    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "from-env")
    e = make()
    e.ani.subscriptions()
    assert e.logins.calls[0]["password"] == "from-env"
    e = make(qbt_password="explicit")
    e.ani.subscriptions()
    assert e.logins.calls[0]["password"] == "explicit"
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD")
    e = make()
    e.ani.subscriptions()
    assert e.logins.calls[0]["password"] == ""


def test_login_flags_follow_constructor(make):
    e = make(autostart_qbt=True, persist_session=True)
    e.ani.subscriptions()
    assert e.logins.calls[0]["autostart"] is True
    assert e.logins.calls[0]["persist"] is True


def test_expired_session_relogs_in_once_and_retries(make):
    stale, fresh = FakeQbt(auth_failures=1), FakeQbt(rules={})
    e = make(stale, outcomes=[stale, fresh])
    assert e.ani.subscriptions() == []
    assert len(e.logins.calls) == 2
    assert any(ep == "/api/v2/rss/rules" for ep, _ in fresh.gets)
    # the fresh session is now the cached one
    e.ani.subscriptions()
    assert len(e.logins.calls) == 2


def test_expired_session_is_retried_only_once(make):
    s1, s2 = FakeQbt(auth_failures=5), FakeQbt(auth_failures=5)
    e = make(s1, outcomes=[s1, s2])
    with pytest.raises(QbtAuthError):
        e.ani.subscriptions()
    assert len(e.logins.calls) == 2
    assert len(s1.gets) == 1 and len(s2.gets) == 1


def test_relogin_failure_after_expiry_surfaces_and_sets_login_failed(make):
    stale = FakeQbt(auth_failures=1)
    e = make(stale, outcomes=[stale, QbtAuthError("rejected", rejected=True)])
    with pytest.raises(QbtAuthError, match="rejected"):
        e.ani.subscriptions()
    assert e.ani._qbt_state["default"].session is None and e.ani._qbt_state["default"].failed_at is not None


def test_post_expiry_during_write_is_retried(make):
    stale = FakeQbt(auth_failures=1)
    fresh = FakeQbt()
    e = make(stale, outcomes=[stale, fresh])
    e.ani.download(["https://x.example/a.torrent"], name="X")
    assert stale.posts == [] and len(fresh.posts_to("/api/v2/torrents/add")) == 1


def test_non_auth_qbt_errors_do_not_trigger_relogin(make):
    class Broken(FakeQbt):
        def get_json(self, endpoint, **params):
            raise QbtError("HTTP 500")
    e = make(Broken())
    with pytest.raises(QbtError):
        e.ani.subscriptions()
    assert len(e.logins.calls) == 1
    assert e.ani._qbt_state["default"].session is not None and e.ani._qbt_state["default"].failed_at is None


def test_login_failure_blocks_further_logins_until_password_swap(make, monkeypatch):
    monkeypatch.setattr(session_mod, "_try_qbt_sid", lambda url, **k: None)
    good = FakeQbt()
    e = make(outcomes=[QbtAuthError("bad password", rejected=True), good])
    with pytest.raises(QbtAuthError, match="bad password"):
        e.ani.subscriptions()
    assert e.ani._qbt_state["default"].failed_at is not None and len(e.logins.calls) == 1
    for _ in range(3):                                # never retried on its own
        with pytest.raises(QbtAuthError, match="set_qbt_password"):
            e.ani.subscriptions()
    assert len(e.logins.calls) == 1
    e.ani.set_qbt_password("new")
    assert e.ani._qbt_state["default"].failed_at is None
    assert e.ani.subscriptions() == []
    assert len(e.logins.calls) == 2
    assert e.logins.calls[1]["password"] == "new"


def test_set_qbt_password_drops_the_cached_session(make):
    s1, s2 = FakeQbt(), FakeQbt()
    e = make(s1, outcomes=[s1, s2])
    e.ani.subscriptions()
    e.ani.set_qbt_password("other")
    e.ani.subscriptions()
    assert len(e.logins.calls) == 2 and s2.gets


def test_login_failed_recovers_via_still_valid_cached_sid(make, monkeypatch):
    cached = FakeQbt()
    monkeypatch.setattr(session_mod, "_try_qbt_sid", lambda url, **k: cached)
    e = make(outcomes=[QbtAuthError("bad password", rejected=True)])
    with pytest.raises(QbtAuthError):
        e.ani.subscriptions()
    assert e.ani.subscriptions() == []                # picked up the CLI's session
    assert e.ani._qbt_state["default"].failed_at is None
    assert len(e.logins.calls) == 1


def test_qbt_unreachable_error_does_not_set_login_failed(make):
    e = make(outcomes=[QbtError("can't reach"), FakeQbt()])
    with pytest.raises(QbtError):
        e.ani.subscriptions()
    assert e.ani._qbt_state["default"].failed_at is None
    assert e.ani.subscriptions() == []                # next call tries again
    assert len(e.logins.calls) == 2


def test_qbt_status_reports_flags_without_logging_in(make, monkeypatch):
    seen = []
    monkeypatch.setattr(api_mod, "_qbt_reachable",
                        lambda url, timeout: seen.append((url, timeout)) or True)
    monkeypatch.setattr(session_mod, "_try_qbt_sid", lambda url, **k: None)
    e = make(outcomes=[QbtAuthError("no", rejected=True)])
    assert e.ani.qbt_status() == {"client": "default", "url": "http://localhost:8080",
                                  "reachable": True, "authenticated": False,
                                  "login_failed": False}
    with pytest.raises(QbtAuthError):
        e.ani.subscriptions()
    st = e.ani.qbt_status()
    assert st["login_failed"] is True and st["authenticated"] is False
    assert seen == [("http://localhost:8080", 2.0)] * 2
    assert len(e.logins.calls) == 1


# -------------------------------------------------- subscriptions/torrents


def test_subscriptions_lists_rules_with_counts(make, tmp_path):
    a = tmp_path / "anime" / "Alpha"
    a.mkdir(parents=True)
    (a / "1.mkv").write_text("x")
    (a / "2.mkv").write_text("x")
    qbt = FakeQbt(
        rules={"beta": {"affectedFeeds": ["http://fb"], "savePath": str(tmp_path / "b"),
                        "enabled": False},
               "Alpha": {"affectedFeeds": ["http://fa"], "savePath": str(a)}},
        torrents=[{"save_path": str(a) + "/"}, {"save_path": str(a)},
                  {"save_path": "/elsewhere"}])
    e = make(qbt)
    infos = e.ani.subscriptions()
    assert [i.name for i in infos] == ["Alpha", "beta"]       # case-insensitive
    alpha, beta = infos
    assert (alpha.feed_url, alpha.rule_enabled) == ("http://fa", True)
    assert (alpha.torrent_count, alpha.file_count) == (2, 2)
    assert (beta.rule_enabled, beta.torrent_count, beta.file_count) == (False, 0, 0)


def test_subscriptions_empty(make):
    assert make().ani.subscriptions() == []


def test_torrents_filters_and_trims(make):
    full = {k: f"v-{k}" for k in actions.TORRENT_FIELDS}
    qbt = FakeQbt(torrents=[
        {**full, "save_path": "/dl/Show", "extra": 1},
        {**full, "save_path": "/dl/Other"}])
    e = make(qbt)
    e.ani._save_base = lambda hidden, client=None: "/dl"
    got = e.ani.torrents(name="Show")
    assert len(got) == 1 and set(got[0]) == set(actions.TORRENT_FIELDS)
    assert len(e.ani.torrents()) == 2
    e.ani.torrents(hashes=["aa", "bb"])
    assert qbt.gets[-1] == ("/api/v2/torrents/info", {"hashes": "aa|bb"})


# ----------------------------------------------------------------- remove


def _rule_env(make, tmp_path, *, save_path, feeds=("http://feed",), torrents=()):
    qbt = FakeQbt(
        rules={"Show": {"affectedFeeds": list(feeds), "savePath": str(save_path)}},
        feed_tree={"Show": {"uid": "u", "url": feeds[0]}},
        torrents=list(torrents))
    return make(qbt)


def _populated(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "ep1.mkv").write_text("12345")
    return path


def test_remove_requires_names(make):
    e = make()
    with pytest.raises(ValidationError):
        e.ani.remove([])
    assert e.logins.calls == []


def test_remove_unknown_name_is_not_found_and_posts_nothing(make):
    e = make()
    with pytest.raises(NotFoundError, match="ghost"):
        e.ani.remove(["ghost"])
    assert e.qbt.posts == []


def test_remove_unknown_name_among_known_aborts_everything(make, tmp_path):
    e = _rule_env(make, tmp_path, save_path=_populated(tmp_path / "anime" / "Show"))
    with pytest.raises(NotFoundError):
        e.ani.remove(["Show", "ghost"])
    assert e.qbt.posts == []


def test_remove_dry_run_posts_nothing_and_keeps_files(make, tmp_path):
    d = _populated(tmp_path / "anime" / "Show")
    e = _rule_env(make, tmp_path, save_path=d,
                  torrents=[{"hash": "h1", "save_path": str(d)}])
    plans = e.ani.remove(["Show"], drop_torrents=True, drop_files=True, dry_run=True)
    assert e.qbt.posts == [] and (d / "ep1.mkv").exists()
    [plan] = plans
    assert plan.name == "Show" and plan.feed_url == "http://feed"
    assert plan.feed_path == "Show" and plan.save_path == str(d)
    assert plan.size_bytes == 5 and plan.torrent_count_in_qb == 1
    assert plan.torrents[0]["hash"] == "h1"


def test_remove_rule_and_feed_only(make, tmp_path):
    d = _populated(tmp_path / "anime" / "Show")
    e = _rule_env(make, tmp_path, save_path=d)
    e.ani.remove(["Show"])
    assert e.qbt.posts == [("/api/v2/rss/removeItem", {"path": "Show"}),
                           ("/api/v2/rss/removeRule", {"ruleName": "Show"})]
    assert (d / "ep1.mkv").exists()


def test_remove_drop_files_inside_save_base_is_allowed(make, tmp_path):
    d = _populated(tmp_path / "anime" / "Show")
    e = _rule_env(make, tmp_path, save_path=d)
    e.ani.remove(["Show"], drop_files=True)
    assert not d.exists()
    assert [p[0] for p in e.qbt.posts] == ["/api/v2/rss/removeItem",
                                           "/api/v2/rss/removeRule"]


def test_remove_drop_files_inside_hidden_base_is_allowed(make, tmp_path):
    d = _populated(tmp_path / "hidden" / "Show")
    e = _rule_env(make, tmp_path, save_path=d)
    e.ani.remove(["Show"], drop_files=True)
    assert not d.exists()


def test_remove_drop_torrents_and_files_lets_qbt_delete(make, tmp_path):
    d = _populated(tmp_path / "anime" / "Show")
    e = _rule_env(make, tmp_path, save_path=d, torrents=[
        {"hash": "h1", "save_path": str(d)}, {"hash": "h2", "save_path": str(d)},
        {"hash": "zz", "save_path": "/other"}])
    e.ani.remove(["Show"], drop_torrents=True, drop_files=True)
    [fields] = e.qbt.posts_to("/api/v2/torrents/delete")
    assert fields == {"hashes": "h1|h2", "deleteFiles": "true"}
    assert d.exists()                  # qB deletes the files, not anirss


def test_remove_drop_torrents_only_keeps_files_on_disk(make, tmp_path):
    d = _populated(tmp_path / "anime" / "Show")
    e = _rule_env(make, tmp_path, save_path=d,
                  torrents=[{"hash": "h1", "save_path": str(d)}])
    e.ani.remove(["Show"], drop_torrents=True)
    [fields] = e.qbt.posts_to("/api/v2/torrents/delete")
    assert fields["deleteFiles"] == "false" and d.exists()


@pytest.fixture
def outside_dir(tmp_path):
    return _populated(tmp_path / "elsewhere" / "Show")


def test_remove_drop_files_outside_roots_refused_before_any_post(
        make, tmp_path, outside_dir):
    e = _rule_env(make, tmp_path, save_path=outside_dir,
                  torrents=[{"hash": "h1", "save_path": str(outside_dir)}])
    for kw in ({"drop_files": True},
               {"drop_files": True, "drop_torrents": True}):
        with pytest.raises(ValidationError, match="outside the download roots"):
            e.ani.remove(["Show"], **kw)
    assert e.qbt.posts == []
    assert (outside_dir / "ep1.mkv").exists()


def test_remove_outside_path_is_fine_when_not_deleting_files(
        make, tmp_path, outside_dir):
    e = _rule_env(make, tmp_path, save_path=outside_dir)
    e.ani.remove(["Show"], drop_torrents=True)
    assert e.qbt.posts_to("/api/v2/rss/removeRule")
    assert (outside_dir / "ep1.mkv").exists()


def test_remove_refuses_the_root_itself(make, tmp_path):
    root = _populated(tmp_path / "anime")
    e = _rule_env(make, tmp_path, save_path=root)
    with pytest.raises(ValidationError):
        e.ani.remove(["Show"], drop_files=True)
    assert e.qbt.posts == [] and root.exists()


def test_remove_refuses_dotdot_escape(make, tmp_path, outside_dir):
    sneaky = tmp_path / "anime" / ".." / "elsewhere" / "Show"
    (tmp_path / "anime").mkdir(exist_ok=True)
    e = _rule_env(make, tmp_path, save_path=sneaky)
    with pytest.raises(ValidationError):
        e.ani.remove(["Show"], drop_files=True)
    assert e.qbt.posts == [] and outside_dir.exists()


def test_remove_refuses_symlink_escape(make, tmp_path, outside_dir):
    base = tmp_path / "anime"
    base.mkdir()
    link = base / "Show"
    link.symlink_to(outside_dir, target_is_directory=True)
    e = _rule_env(make, tmp_path, save_path=link)
    with pytest.raises(ValidationError):
        e.ani.remove(["Show"], drop_files=True)
    assert e.qbt.posts == [] and (outside_dir / "ep1.mkv").exists()


def test_remove_refuses_sibling_with_shared_prefix(make, tmp_path):
    d = _populated(tmp_path / "anime-extra" / "Show")      # startswith("anime")
    e = _rule_env(make, tmp_path, save_path=d)
    with pytest.raises(ValidationError):
        e.ani.remove(["Show"], drop_files=True)
    assert e.qbt.posts == []


def test_remove_rule_without_save_path_falls_back_to_base(make, tmp_path):
    qbt = FakeQbt(rules={"Show": {"affectedFeeds": []}})
    e = make(qbt)
    [plan] = e.ani.remove(["Show"], dry_run=True, hidden=True)
    assert plan.save_path == os.path.join(e.hidden_base, "Show")
    assert plan.feed_path is None and plan.feed_url == ""


def test_execute_remove_notifies_and_confine_to_is_optional(tmp_path):
    d = _populated(tmp_path / "anywhere" / "Show")
    qbt = FakeQbt(rules={"Show": {"affectedFeeds": ["http://feed"], "savePath": str(d)}},
                  feed_tree={"Show": {"url": "http://feed"}})
    plans = actions.plan_remove(qbt, ["Show"], drop_torrents=False, drop_files=True,
                                save_base=str(tmp_path))
    notes: list[str] = []
    actions.execute_remove(qbt, plans, drop_torrents=False, drop_files=True,
                           notify=notes.append)         # confine_to=None: allowed
    assert not d.exists()
    assert notes == ["removed feed Show", "removed rule Show",
                     f"deleted files at {d}"]


# -------------------------------------------------------- serialization


def test_to_jsonable_search_result():
    items = [Item("t", "l", seeders=3, size="1 GiB")]
    res = SearchResult("q", "nyaa", items, [{"endpoint": "nyaa", "status": "ok",
                                              "count": 1, "error": None}])
    out = to_jsonable(res)
    assert out == {"query": "q", "endpoint": "nyaa",
                   "items": [{**Item("t", "l", seeders=3, size="1 GiB")._asdict()}],
                   "tried": [{"endpoint": "nyaa", "status": "ok", "count": 1,
                              "error": None}]}
    assert type(out) is dict and type(out["items"][0]) is dict
    assert json.loads(json.dumps(out)) == out


def test_to_jsonable_dataclasses(tmp_path):
    rule = actions.RuleInfo("n", "http://f", "/p", True, 2, 3)
    assert to_jsonable(rule) == {"name": "n", "feed_url": "http://f",
                                 "save_path": "/p", "rule_enabled": True,
                                 "torrent_count": 2, "file_count": 3}
    plan = actions.RemovePlan("n", "http://f", None, "/p", [{"hash": "h"}], 5, 1)
    out = to_jsonable([plan])
    assert out == [{"name": "n", "feed_url": "http://f", "feed_path": None,
                    "save_path": "/p", "torrents": [{"hash": "h"}],
                    "size_bytes": 5, "torrent_count_in_qb": 1}]
    sub = actions.SubscribeResult("n", "u", "/p", "", "x|y", ["a"])
    assert to_jsonable(sub)["tags"] == ["a"]
    add = actions.AddResult(1, "/p", ["a"], ["h"])
    assert to_jsonable(add) == {"count": 1, "save_path": "/p", "tags": ["a"],
                                "hashes": ["h"]}
    json.dumps(to_jsonable([rule, plan, sub, add]))


def test_to_jsonable_refine_outcome_drops_derived_properties():
    out = _apply(GENERIC_RSS, "Show", TUI_ITEMS, "term", term="avc")
    j = to_jsonable(out)
    assert set(j) == {"applied", "query", "items", "mode", "previous_count", "reason"}
    assert j["applied"] is True and j["reason"] is None
    assert j["items"] == [TUI_ITEMS[2]._asdict()]
    json.dumps(j)


def test_to_jsonable_containers_and_scalars():
    nested = {1: (Item("a", "b"),), "k": [{"x": None}], "s": "str", "n": 2.5}
    out = to_jsonable(nested)
    assert set(out) == {"1", "k", "s", "n"}
    assert out["1"] == [Item("a", "b")._asdict()]
    assert to_jsonable(None) is None and to_jsonable(3) == 3
    assert to_jsonable(actions.RuleInfo) is actions.RuleInfo      # a class, not an instance


def test_item_from_dict_round_trip_all_fields():
    item = Item("T", "L", seeders=1, leechers=2, downloads=3, size="4 MiB",
                category="c", page="p", source="s", resolution="1080p",
                infomkv="NF WEB-DL", linktype="Torrent")
    assert item_from_dict(to_jsonable(item)) == item
    assert item_from_dict(json.loads(json.dumps(to_jsonable(item)))) == item
    minimal = Item("T", "L")
    assert item_from_dict(to_jsonable(minimal)) == minimal


def test_item_from_dict_defaults_unknown_keys_and_nulls():
    got = item_from_dict({"title": "T", "link": "L", "seeders": None,
                          "size": None, "bogus": object(), "extra": [1]})
    assert got == Item("T", "L")
    assert item_from_dict({"title": "", "link": ""}) == Item("", "")
    assert item_from_dict({"title": "T", "link": "L", "seeders": 0}).seeders == 0


@pytest.mark.parametrize("bad", [
    None, [], "x", 5,
    {}, {"title": "T"}, {"link": "L"},
    {"title": 1, "link": "L"}, {"title": "T", "link": None},
    {"title": "T", "link": ["L"]},
    {"title": "T", "link": "L", "seeders": "5"},
    {"title": "T", "link": "L", "seeders": 5.0},
    {"title": "T", "link": "L", "seeders": True},
    {"title": "T", "link": "L", "leechers": [1]},
    {"title": "T", "link": "L", "downloads": {}},
    {"title": "T", "link": "L", "size": 7},
    {"title": "T", "link": "L", "category": ["x"]},
    {"title": "T", "link": "L", "source": 1},
    {"title": "T", "link": "L", "linktype": False},
])
def test_item_from_dict_validation(bad):
    with pytest.raises(ValidationError):
        item_from_dict(bad)


def test_item_from_dict_covers_every_item_field():
    # guards against a new Item field being added without serializer support
    full = {f: (1 if f in ("seeders", "leechers", "downloads") else f)
            for f in Item._fields}
    item = item_from_dict(full)
    assert item._asdict() == full
