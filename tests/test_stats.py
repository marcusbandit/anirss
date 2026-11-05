"""Tests for tracker-stats enrichment (stats.py), fully offline."""

import hashlib
import json

import pytest

from anirss_lib import stats
from anirss_lib.types import Item


def _benc(obj) -> bytes:
    """Tiny bencode encoder for fixtures."""
    if isinstance(obj, int):
        return f"i{obj}e".encode()
    if isinstance(obj, str):
        return f"{len(obj.encode())}:".encode() + obj.encode()
    if isinstance(obj, bytes):
        return f"{len(obj)}:".encode() + obj
    if isinstance(obj, list):
        return b"l" + b"".join(_benc(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(_benc(k) + _benc(v) for k, v in obj.items()) + b"e"
    raise TypeError(obj)


def _make_torrent(name: bytes = b"Show - 01 [1080p].mkv",
                  announces: list | None = None) -> bytes:
    if announces is None:
        announces = [[b"http://t1.example/announce"],
                     [b"udp://t2.example:1337/announce"]]
    return _benc({
        b"announce": announces[0][0],
        b"announce-list": announces,
        b"comment": b"test",
        b"info": {b"name": name, b"length": 12345,
                  b"piece length": 16384, b"pieces": b"\x00" * 20},
    })


# ---- torrent_facts ----

def test_torrent_facts_infohash_matches_sha1_of_info_dict():
    data = _make_torrent()
    infohash, _ = stats.torrent_facts(data)
    start = data.index(b"4:info") + 6
    end = data.rindex(b"ee") + 1  # info is the last top-level key here
    assert infohash == hashlib.sha1(data[start:end]).digest()


def test_torrent_facts_announce_list_in_priority_order():
    infohash, announces = stats.torrent_facts(_make_torrent())
    assert announces == ["http://t1.example/announce",
                         "udp://t2.example:1337/announce"]


def test_torrent_facts_falls_back_to_single_announce():
    data = _benc({b"announce": b"http://only.example/announce",
                  b"info": {b"name": b"x", b"length": 1,
                            b"piece length": 16384, b"pieces": b"\x00" * 20}})
    _, announces = stats.torrent_facts(data)
    assert announces == ["http://only.example/announce"]


def test_torrent_facts_rejects_no_announce():
    data = _benc({b"comment": b"nothing here"})
    with pytest.raises(ValueError):
        stats.torrent_facts(data)


# ---- HTTP scrape parsing ----

def test_parse_http_scrape_reads_all_files():
    h1, h2 = (b"\x01" * 20), (b"\x02" * 20)
    body = (b"d5:filesd" + b"20:" + h1
            + b"d8:completei310e10:incompletei2e10:downloadedi4576ee"
            + b"20:" + h2
            + b"d8:completei0e10:incompletei0e10:downloadedi0eee")
    assert stats._parse_http_scrape(body) == {
        h1: (310, 2, 4576),
        h2: (0, 0, 0),
    }


def test_http_scrape_replaces_announce_with_scrape(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        class R:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b"d5:filesd" + b"20:" + b"\x01" * 20 \
                    + b"d8:completei5e10:incompletei1e10:downloadedi9eee"
        return R()

    monkeypatch.setattr(stats.urllib.request, "urlopen", fake_urlopen)
    out = stats._http_scrape("http://nyaa.tracker.wf:7777/announce",
                             [b"\x01" * 20])
    assert captured["url"].startswith("http://nyaa.tracker.wf:7777/scrape?")
    assert "info_hash=" in captured["url"]
    assert out[b"\x01" * 20] == (5, 1, 9)


def _capture_urlopen(monkeypatch):
    captured = []

    def fake_urlopen(req, timeout=0):
        captured.append(req.full_url)
        class R:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b"d5:filesd" + b"20:" + b"\x01" * 20 \
                    + b"d8:completei5e10:incompletei1e10:downloadedi9eee"
        return R()

    monkeypatch.setattr(stats.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_http_scrape_turns_announce_php_into_scrape_php_and_keeps_the_query(monkeypatch):
    # BEP 48 only asks that the last path segment start with "announce" (#40).
    captured = _capture_urlopen(monkeypatch)
    out = stats._http_scrape("https://t.example/x/announce.php?passkey=abc",
                             [b"\x01" * 20])
    assert len(captured) == 1
    assert captured[0].startswith(
        "https://t.example/x/scrape.php?passkey=abc&info_hash=")
    assert out[b"\x01" * 20] == (5, 1, 9)


def test_http_scrape_keeps_the_query_of_a_plain_announce(monkeypatch):
    captured = _capture_urlopen(monkeypatch)
    stats._http_scrape("http://t.example/pk/announce?passkey=abc", [b"\x01" * 20])
    assert captured[0].startswith("http://t.example/pk/scrape?passkey=abc&info_hash=")


def test_scrape_path_swaps_announce_at_the_start_of_the_last_segment():
    for announce, scrape in [
        ("/announce", "/scrape"),
        ("/x/announce.php", "/x/scrape.php"),
        ("/pk/announce", "/pk/scrape"),
        ("/announce/x", None),
        ("/my-announce", None),
        ("/x/announce_v2", "/x/scrape_v2"),
    ]:
        assert stats._scrape_path(announce) == scrape, announce


def test_http_scrape_skips_a_tracker_with_no_scrape_convention(monkeypatch):
    captured = _capture_urlopen(monkeypatch)
    assert stats._http_scrape("http://t.example/announce/x", [b"\x01" * 20]) == {}
    assert captured == []


def test_http_scrape_skips_non_http_trackers():
    assert stats._http_scrape("udp://tracker.example:1337/announce",
                              [b"\x01" * 20]) == {}


# ---- UDP scrape parsing ----

def test_udp_scrape_parses_seeders_completed_leechers(monkeypatch):
    h = b"\x03" * 20
    sent = []

    class FakeSock:
        def settimeout(self, t):
            pass
        def sendto(self, payload, addr):
            sent.append(payload)
        def recvfrom(self, n):
            if len(sent) == 1:  # connect response
                return (b"\x00" * 4 + b"\x00" * 4 + b"\x11" * 8), ("x", 1)
            # scrape response: action=2, tid, then (seeders, completed, leechers)
            return ((2).to_bytes(4, "big") + b"\x00" * 4
                    + (99).to_bytes(4, "big")     # seeders
                    + (777).to_bytes(4, "big")    # completed
                    + (11).to_bytes(4, "big")), ("x", 1)  # leechers
        def close(self):
            pass

    import socket as socket_mod
    monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: FakeSock())

    out = stats._udp_scrape("udp://tracker.example:1337/announce", [h])
    # UDP order is (seeders, completed, leechers) -> mapped to (seeders,
    # leechers, downloads) like the HTTP answer.
    assert out[h] == (99, 11, 777)


# ---- enrich ----

@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "CACHE_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(stats, "_MEM_CACHE", {})


def test_enrich_passes_through_known_stats():
    items = [Item("Show - 01", "l", seeders=5, leechers=1, downloads=9)]
    assert stats.enrich(items) == items


def test_enrich_fills_stats_from_scrape(monkeypatch):
    data = _make_torrent()
    infohash, _ = stats.torrent_facts(data)
    monkeypatch.setattr(stats, "_fetch_torrent", lambda url, timeout=10: data)
    monkeypatch.setattr(stats, "_scrape_tracker",
                        lambda ann, hashes: {infohash: (310, 2, 4576)})
    items = [Item("Show - 01", "http://erai/t.torrent")]
    out = stats.enrich(items)
    assert (out[0].seeders, out[0].leechers, out[0].downloads) == (310, 2, 4576)


def test_enrich_failure_leaves_unknown_and_negative_caches(monkeypatch):
    monkeypatch.setattr(stats, "_fetch_torrent", lambda url, timeout=10: None)
    items = [Item("Show - 01", "http://erai/broken.torrent")]
    assert stats.enrich(items)[0].seeders is None
    # Second run must not even try to download (negative cache).
    calls = []
    monkeypatch.setattr(stats, "_fetch_torrent",
                        lambda url, timeout=10: calls.append(url) or None)
    assert stats.enrich(items)[0].seeders is None
    assert calls == []


def test_enrich_prefers_nonzero_tracker_results(monkeypatch):
    """A tracker that doesn't know the hash answers zeros; a later tracker
    that knows better wins."""
    data = _make_torrent()
    infohash, _ = stats.torrent_facts(data)

    def fake_scrape(announce, hashes):
        if "t1" in announce:
            return {infohash: (0, 0, 0)}
        return {infohash: (7, 1, 42)}

    monkeypatch.setattr(stats, "_fetch_torrent", lambda url, timeout=10: data)
    monkeypatch.setattr(stats, "_scrape_tracker", fake_scrape)
    out = stats.enrich([Item("Show - 01", "http://erai/t.torrent")])
    assert (out[0].seeders, out[0].leechers, out[0].downloads) == (7, 1, 42)


def test_enrich_uses_cache_within_ttl(monkeypatch):
    data = _make_torrent()
    infohash, _ = stats.torrent_facts(data)
    monkeypatch.setattr(stats, "_scrape_tracker",
                        lambda ann, hashes: {infohash: (1, 2, 3)})
    downloads = []
    monkeypatch.setattr(stats, "_fetch_torrent",
                        lambda url, timeout=10: downloads.append(url) or data)
    items = [Item("Show - 01", "http://erai/t.torrent")]
    stats.enrich(items)
    out = stats.enrich(items)
    assert downloads == ["http://erai/t.torrent"]
    assert (out[0].seeders, out[0].leechers, out[0].downloads) == (1, 2, 3)


def test_enrich_persists_cache_to_disk(monkeypatch):
    data = _make_torrent()
    infohash, _ = stats.torrent_facts(data)
    monkeypatch.setattr(stats, "_scrape_tracker",
                        lambda ann, hashes: {infohash: (1, 2, 3)})
    monkeypatch.setattr(stats, "_fetch_torrent", lambda url, timeout=10: data)
    stats.enrich([Item("Show - 01", "http://erai/t.torrent")])
    raw = json.loads(stats.CACHE_PATH.read_text())
    assert list(raw["urls"].values())[0][1:4] == [1, 2, 3]


def test_enrich_respects_deadline(monkeypatch):
    # A stalled server: the fetch sleeps exactly as long as its timeout
    # allows (like urlopen would), so the deadline must bound the total.
    import time

    def slow_fetch(url, timeout=10):
        time.sleep(timeout)
        return None

    monkeypatch.setattr(stats, "_fetch_torrent", slow_fetch)
    items = [Item(f"Show - {i:02d}", f"http://erai/{i}.torrent") for i in range(9)]
    t0 = time.time()
    stats.enrich(items, deadline_s=1.0)
    assert time.time() - t0 < 3.5


def test_fetch_torrents_keeps_only_real_torrents(monkeypatch):
    good = _make_torrent()
    replies = {"http://h/a": good, "http://h/b": None,
               "http://h/c": b"<html>checking your browser</html>"}
    calls = []
    monkeypatch.setattr(stats, "_fetch_torrent",
                        lambda url, timeout=10: calls.append(url) or replies[url])
    got = stats.fetch_torrents(["http://h/a", "http://h/b", "http://h/c", "http://h/a"],
                               deadline_s=5)
    assert got == {"http://h/a": good}
    assert sorted(calls) == ["http://h/a", "http://h/b", "http://h/c"]


def test_fetch_torrent_encodes_the_path_but_keeps_the_query(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"d4:infod1:xi1eee"

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        return Resp()

    monkeypatch.setattr(stats.urllib.request, "urlopen", fake_urlopen)
    assert stats._fetch_torrent("https://h.example/[G] a b.torrent?token=1&x=y")
    assert seen == ["https://h.example/%5BG%5D%20a%20b.torrent?token=1&x=y"]
