import pytest

from anirss_lib import endpoints
from anirss_lib.endpoints import Endpoint, EndpointState


NYAA = Endpoint(name="nyaa", kind="nyaa", url="https://nyaa.si/",
                category="1_2", filter="1")
ANIRENA = Endpoint(name="anirena", kind="rss",
                   url="https://www.anirena.com/rss?q={query}&adult=1")


def test_load_endpoints_valid():
    cfg = {"endpoint": [
        {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.si/",
         "category": "1_2", "filter": "1"},
        {"name": "anirena", "kind": "rss",
         "url": "https://www.anirena.com/rss?q={query}&adult=1"},
    ]}
    eps = endpoints.load_endpoints(cfg)
    assert eps == [NYAA, ANIRENA]


@pytest.mark.parametrize("bad", [
    {"kind": "nyaa", "url": "https://x/"},                       # no name
    {"name": "a", "kind": "html", "url": "https://x/"},          # bad kind
    {"name": "a", "kind": "rss", "url": "https://x/rss"},        # no {query}
    {"name": "a", "kind": "nyaa"},                               # no url
])
def test_load_endpoints_invalid_dies(bad):
    with pytest.raises(SystemExit):
        endpoints.load_endpoints({"endpoint": [bad]})


def test_load_endpoints_duplicate_name_dies():
    ep = {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.si/"}
    with pytest.raises(SystemExit):
        endpoints.load_endpoints({"endpoint": [ep, dict(ep)]})


def test_load_endpoints_invalid_name_chars_dies():
    ep = {"name": "bad name)", "kind": "nyaa", "url": "https://x/"}
    with pytest.raises(SystemExit):
        endpoints.load_endpoints({"endpoint": [ep]})


def test_search_url_nyaa_kind():
    url = endpoints.search_url(NYAA, "one piece")
    assert url.startswith("https://nyaa.si/?")
    assert "page=rss" in url and "q=one+piece" in url
    assert "c=1_2" in url and "f=1" in url


def test_search_url_rss_kind_fills_template():
    url = endpoints.search_url(ANIRENA, "shin chan")
    assert url == "https://www.anirena.com/rss?q=shin+chan&adult=1"


def test_feed_url_nyaa_kind_keeps_exclusions_in_url():
    url, excluded = endpoints.feed_url(NYAA, "one piece -HEVC")
    assert url == endpoints.search_url(NYAA, "one piece -HEVC")
    assert excluded == []


def test_feed_url_rss_kind_strips_exclusions():
    url, excluded = endpoints.feed_url(ANIRENA, 'show 1080p -HEVC -"dual audio"')
    assert "HEVC" not in url and "dual" not in url
    assert url == endpoints.search_url(ANIRENA, "show 1080p")
    assert excluded == ["HEVC", "dual audio"]


def test_feed_url_rss_kind_no_exclusions_roundtrips():
    url, excluded = endpoints.feed_url(ANIRENA, "shin chan")
    assert url == endpoints.search_url(ANIRENA, "shin chan")
    assert excluded == []


def test_state_default_active_is_first():
    st = EndpointState([NYAA, ANIRENA])
    assert st.active is NYAA


def test_state_active_by_name():
    st = EndpointState([NYAA, ANIRENA], "anirena")
    assert st.active is ANIRENA


def test_state_unknown_name_dies():
    with pytest.raises(SystemExit):
        EndpointState([NYAA, ANIRENA], "tosho")


def test_state_cycle_wraps():
    st = EndpointState([NYAA, ANIRENA])
    assert st.cycle() is ANIRENA
    assert st.cycle() is NYAA


from anirss_lib.nyaa import FetchError
from anirss_lib.types import Item


def test_split_exclusions():
    q, excl = endpoints.split_exclusions('show 1080p -HEVC -"dual audio"')
    assert q == "show 1080p"
    assert excl == ["HEVC", "dual audio"]


def test_split_exclusions_no_exclusions_roundtrip():
    q, excl = endpoints.split_exclusions("[Erai-raws] show 1080p")
    assert q == "[Erai-raws] show 1080p"
    assert excl == []


def test_filter_excluded_case_insensitive():
    items = [Item("Show 05 HEVC x265", "l1"), Item("Show 05 AVC", "l2")]
    kept = endpoints.filter_excluded(items, ["hevc"])
    assert [i.link for i in kept] == ["l2"]


def test_fetch_items_rss_kind_applies_exclusions(monkeypatch):
    fetched_urls = []

    def fake_fetch_rss(url, endpoint_name="feed"):
        fetched_urls.append(url)
        return [Item("Show 05 HEVC", "l1"), Item("Show 05 AVC", "l2")]

    monkeypatch.setattr(endpoints.nyaa, "fetch_rss", fake_fetch_rss)
    items = endpoints.fetch_items(ANIRENA, "show -HEVC")
    assert [i.link for i in items] == ["l2"]
    # The exclusion never reaches the wire; only positive terms are sent.
    assert "HEVC" not in fetched_urls[0]


def test_fetch_items_nyaa_kind_sends_exclusions(monkeypatch):
    import urllib.parse

    def fake_fetch_rss(url, endpoint_name="feed"):
        assert "-HEVC" in urllib.parse.unquote_plus(url)
        return [Item("t", "l")]

    monkeypatch.setattr(endpoints.nyaa, "fetch_rss", fake_fetch_rss)
    assert endpoints.fetch_items(NYAA, "show -HEVC")


def test_probe_fallback_switches_to_first_hit():
    st = EndpointState([NYAA, ANIRENA])

    def fake_fetch(ep, query):
        return [Item("t", "l")] if ep.name == "anirena" else []

    items, notes = endpoints.probe_fallback(st, "q", fetch=fake_fetch)
    assert items and st.active is ANIRENA
    assert notes == ["anirena: 1"]


def test_probe_fallback_all_fail_keeps_active():
    third = Endpoint(name="tosho", kind="rss", url="https://x/?q={query}")
    st = EndpointState([NYAA, ANIRENA, third])

    def fake_fetch(ep, query):
        if ep.name == "anirena":
            raise FetchError("can't reach anirena: boom")
        return []

    items, notes = endpoints.probe_fallback(st, "q", fetch=fake_fetch)
    assert items == [] and st.active is NYAA
    assert notes == ["anirena: unreachable", "tosho: 0"]
