import pytest
from anirss_lib.cli.urls import UrlKind, classify_url, extract_nyaa_query


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
