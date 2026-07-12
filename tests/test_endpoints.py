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


def test_search_url_nyaa_kind():
    url = endpoints.search_url(NYAA, "one piece")
    assert url.startswith("https://nyaa.si/?")
    assert "page=rss" in url and "q=one+piece" in url
    assert "c=1_2" in url and "f=1" in url


def test_search_url_rss_kind_fills_template():
    url = endpoints.search_url(ANIRENA, "shin chan")
    assert url == "https://www.anirena.com/rss?q=shin+chan&adult=1"


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
