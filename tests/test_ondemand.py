import http.server
import os
import sys
import threading

import pytest

from anirss_lib import endpoints, ondemand
from anirss_lib import main as main_mod
from anirss_lib.cli.args import ParsedArgs
from anirss_lib.endpoints import Endpoint, EndpointState, load_endpoints
from anirss_lib.errors import ConfigError
from anirss_lib.nyaa import FetchError, parse_rss

TORZNAB_ITEM = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
<channel><title>1337x</title>
<item>
  <title>Avatar.Seven.Havens.S01E03.The.Order.1080p.PMTP.WEB-DL.DDP5.1.ENG.ITA.H264-TBK</title>
  <guid>https://1337x.to/torrent/6736161/x/</guid>
  <pubDate>Sat, 10 Oct 2026 17:00:00 +0200</pubDate>
  <size>298570796</size>
  <description>Uploader: LittleMouse</description>
  <link>http://127.0.0.1:9117/dl/1337x/?jackett_apikey=k&amp;path=p&amp;file=f</link>
  <category>5040</category>
  <enclosure url="http://127.0.0.1:9117/dl/1337x/?jackett_apikey=k&amp;path=p&amp;file=f"
             length="298570796" type="application/x-bittorrent" />
  <torznab:attr name="category" value="5040" />
  <torznab:attr name="category" value="100041" />
  <torznab:attr name="seeders" value="393" />
  <torznab:attr name="peers" value="476" />
</item>
</channel></rss>"""


def test_torznab_item_parses_stats_size_category():
    (item,) = parse_rss(TORZNAB_ITEM, "1337x")
    assert item.title.endswith("ENG.ITA.H264-TBK")
    assert item.seeders == 393
    assert item.leechers == 83
    assert item.downloads is None
    assert item.size == "284.7 MiB"
    assert item.category == "TV"
    assert item.link.startswith("http://127.0.0.1:9117/dl/1337x/")


def _cfg(**extra):
    return {"endpoint": [
        {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.si/"},
        {"name": "1337x", "kind": "rss",
         "url": "http://127.0.0.1:9117/api?t=search&q={query}", **extra},
    ]}


def test_endpoint_overrides_and_on_demand_fields():
    eps = load_endpoints(_cfg(start="up", stop="down", ready="http://r/",
                              save_base="~/TV", tags=["TV"]))
    nyaa, x = eps
    assert not nyaa.on_demand and nyaa.timeout == 10.0 and nyaa.tags is None
    assert x.on_demand and x.timeout == 60.0
    assert x.save_base == os.path.expanduser("~/TV")
    assert x.tags == ("TV",)


def test_start_without_ready_is_a_config_error():
    with pytest.raises(ConfigError):
        load_endpoints(_cfg(start="up"))


def test_owner_of_matches_feed_urls():
    eps = load_endpoints(_cfg())
    url = endpoints.search_url(eps[1], "Avatar Seven Havens")
    assert endpoints.owner_of(eps, url) is eps[1]
    assert endpoints.owner_of(eps, "https://elsewhere/?q=x") is None


def test_save_base_and_tags_follow_the_endpoint():
    cfg = {"downloads": {"save_base": "/anime", "movie_path": "/movies",
                         "hidden_base": "/h"},
           "qbittorrent": {"tags": ["Anime"]}}
    tv = Endpoint("1337x", "rss", "u{query}", save_base="/tv",
                  movie_path="/films", tags=("TV",))
    plain = Endpoint("nyaa", "nyaa", "https://nyaa.si/")
    parsed = ParsedArgs()
    assert main_mod._save_base_for(parsed, cfg, tv) == "/tv"
    assert main_mod._save_base_for(parsed, cfg, plain) == "/anime"
    assert main_mod._tags_for(cfg, tv) == ["TV"]
    assert main_mod._tags_for(cfg, plain) == ["Anime"]
    assert main_mod._movie_path_for(cfg, tv) == "/films"
    parsed.hidden = True
    assert main_mod._save_base_for(parsed, cfg, tv) == "/h"


def test_fallback_skips_on_demand_endpoint_that_is_off(monkeypatch):
    off = Endpoint("1337x", "rss", "http://x/{query}", start="up", ready="http://r/")
    nyaa = Endpoint("nyaa", "nyaa", "https://nyaa.si/")
    state = EndpointState([nyaa, off])
    monkeypatch.setattr(ondemand, "is_ready", lambda ep, timeout=2.0: False)
    called = []
    _items, notes = endpoints.probe_fallback(
        state, "q", fetch=lambda ep, q: called.append(ep) or [])
    assert called == [] and notes == ["1337x: off"]


def test_fetch_without_boot_never_starts(monkeypatch):
    off = Endpoint("1337x", "rss", "http://x/{query}", start="up", ready="http://r/")
    monkeypatch.setattr(ondemand, "is_ready", lambda ep, timeout=2.0: False)
    monkeypatch.setattr(ondemand, "ensure",
                        lambda ep, say=print: pytest.fail("must not start"))
    with pytest.raises(FetchError):
        endpoints.fetch_items(off, "q", boot=False)


@pytest.fixture
def lease_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ondemand, "LEASE_DIR", tmp_path)
    monkeypatch.setattr(ondemand, "_held", {})
    monkeypatch.setattr(ondemand, "_hooks_installed", True)
    return tmp_path


def _py(code: str) -> str:
    """A shell command running `code` with this Python, on sh and cmd.exe alike."""
    return f'"{sys.executable}" -c "{code}"'


def _service(tmp_path):
    flag = tmp_path / "running"
    ep = Endpoint("svc", "rss", "http://x/{query}",
                  start=_py(f"import pathlib; pathlib.Path(r'{flag}').touch()"),
                  stop=_py(f"import pathlib; pathlib.Path(r'{flag}').unlink(missing_ok=True)"),
                  ready="http://r/", timeout=5)
    return ep, flag


def test_ensure_starts_and_release_stops(lease_dir, monkeypatch):
    ep, flag = _service(lease_dir)
    monkeypatch.setattr(ondemand, "is_ready", lambda e, timeout=2.0: flag.exists())
    lines = []
    ondemand.ensure(ep, say=lines.append)
    assert flag.exists()
    assert "starting connection" in lines[0] and "connected" in lines[1]
    ondemand.release(ep)
    assert not flag.exists()


def test_release_keeps_service_while_another_lease_lives(lease_dir, monkeypatch):
    ep, flag = _service(lease_dir)
    monkeypatch.setattr(ondemand, "is_ready", lambda e, timeout=2.0: flag.exists())
    ondemand.ensure(ep, say=lambda _l: None)
    other = ondemand._lease(ep.name, os.getppid())
    other.touch()
    ondemand.release(ep)
    assert flag.exists()
    other.unlink()
    ondemand.ensure(ep, say=lambda _l: None)
    ondemand.release(ep)
    assert not flag.exists()


def test_dead_leases_are_pruned(lease_dir, monkeypatch):
    ep, flag = _service(lease_dir)
    monkeypatch.setattr(ondemand, "is_ready", lambda e, timeout=2.0: flag.exists())
    ondemand.ensure(ep, say=lambda _l: None)
    ondemand._lease(ep.name, 2**22 + 12345).touch()
    ondemand.release(ep)
    assert not flag.exists()


def test_service_started_by_someone_else_is_left_running(lease_dir, monkeypatch):
    ep, flag = _service(lease_dir)
    flag.touch()
    monkeypatch.setattr(ondemand, "is_ready", lambda e, timeout=2.0: flag.exists())
    ondemand.ensure(ep, say=lambda _l: pytest.fail("already up, no status"))
    ondemand.release(ep)
    assert flag.exists()


def test_failed_start_raises_and_cleans_up(lease_dir, monkeypatch):
    ep = Endpoint("svc", "rss", "http://x/{query}",
                  start=_py("import sys; sys.stderr.write('nope'); sys.exit(3)"),
                  stop=_py("pass"), ready="http://r/", timeout=5)
    monkeypatch.setattr(ondemand, "is_ready", lambda e, timeout=2.0: False)
    with pytest.raises(ondemand.StartError, match="nope"):
        ondemand.ensure(ep, say=lambda _l: None)
    assert not list(lease_dir.glob("*.lease"))


class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "magnet:?xt=urn:btih:ABC")
        self.end_headers()

    def log_message(self, *_a):
        pass


def test_resolve_links_swaps_proxied_links_for_magnets():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        ep = Endpoint("svc", "rss", base + "/api?q={query}", start="x",
                      ready=base, timeout=5)
        links = [base + "/dl/1", "https://nyaa.si/download/2.torrent"]
        assert ondemand.resolve_links(links, [ep]) == [
            "magnet:?xt=urn:btih:ABC", "https://nyaa.si/download/2.torrent"]
    finally:
        srv.shutdown()
