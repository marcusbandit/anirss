import pytest
from anirss_lib.cli.urls import UrlKind, classify_url, extract_nyaa_query, endpoint_hosts


@pytest.mark.parametrize("url,kind", [
    ("magnet:?xt=urn:btih:abc", UrlKind.ONE_SHOT),
    ("https://example.com/x.torrent", UrlKind.ONE_SHOT),
    ("http://example.com/x.torrent?token=1", UrlKind.ONE_SHOT),
    ("https://nyaa.si/?page=rss&q=Frieren", UrlKind.NYAA_RSS),
    ("https://sukebei.nyaa.si/", UrlKind.NYAA_RSS),
    ("https://example.com/", UrlKind.OTHER_HTTP),
    ("not a url", UrlKind.NOT_URL),
    ("", UrlKind.NOT_URL),
    # Local .torrent paths route through the upload path.
    ("/tmp/foo.torrent", UrlKind.LOCAL_TORRENT),
    ("./local.torrent", UrlKind.LOCAL_TORRENT),
    ("~/Downloads/x.TORRENT", UrlKind.LOCAL_TORRENT),
    ("relative.torrent", UrlKind.LOCAL_TORRENT),
])
def test_classify_url(url, kind):
    assert classify_url(url) == kind


def test_extract_nyaa_query_basic():
    url = "https://nyaa.si/?page=rss&q=Frieren&c=1_0&f=0"
    assert extract_nyaa_query(url) == "Frieren"


def test_extract_nyaa_query_url_encoded_spaces():
    url = "https://nyaa.si/?page=rss&q=Sousou+no+Frieren"
    assert extract_nyaa_query(url) == "Sousou no Frieren"
    url2 = "https://nyaa.si/?page=rss&q=Sousou%20no%20Frieren"
    assert extract_nyaa_query(url2) == "Sousou no Frieren"


def test_extract_nyaa_query_missing():
    assert extract_nyaa_query("https://nyaa.si/") is None
    assert extract_nyaa_query("https://nyaa.si/?page=rss") is None
    assert extract_nyaa_query("https://nyaa.si/?page=rss&q=") is None


class _Ep:
    def __init__(self, kind, url):
        self.kind, self.url = kind, url


def test_endpoint_hosts_partition_by_kind():
    nyaa_hosts, rss_hosts = endpoint_hosts([
        _Ep("nyaa", "https://mirror.example/"),
        _Ep("rss", "https://www.anirena.com/rss?q={query}&adult=1"),
    ])
    assert nyaa_hosts == frozenset({"mirror.example"})
    assert rss_hosts == frozenset({"www.anirena.com"})


def test_classify_url_nyaa_kind_host_is_nyaa_rss():
    kind = classify_url("https://mirror.example/?page=rss&q=x",
                        nyaa_hosts=frozenset({"mirror.example"}))
    assert kind == UrlKind.NYAA_RSS


def test_classify_url_rss_kind_host_is_endpoint_rss():
    kind = classify_url("https://www.anirena.com/rss?q=x&adult=1",
                        rss_hosts=frozenset({"www.anirena.com"}))
    assert kind == UrlKind.ENDPOINT_RSS


def test_classify_url_unknown_host_still_other_http():
    assert classify_url("https://elsewhere.example/feed") == UrlKind.OTHER_HTTP
