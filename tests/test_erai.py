import urllib.parse

from anirss_lib import endpoints, erai, nyaa
from anirss_lib.ansi import C_DIM, C_OFF, ansi_strip
from anirss_lib.endpoints import Endpoint
from anirss_lib.types import Item


ERAI_EP = Endpoint(
    name="erai", kind="rss",
    url=("https://www.erai-raws.info/episodes/feed/"
         "?res=1080p&type=torrent&subs%5B0%5D=us&s={query}&token=TOK"))
NYAA_EP = Endpoint(name="nyaa", kind="nyaa", url="https://nyaa.si/")

GAIKOTSU_PAGE = ("https://www.erai-raws.info/episodes/"
                 "gaikotsu-kishi-sama-tadaima-isekai-e-odekake-chuu-ii-02/")


def _erai_items():
    return [Item("Gaikotsu Kishi-sama Tadaima Isekai e Odekake-chuu II - 02 "
                 "[1080p CR WEB-DL AVC AAC][Airing]", "l", page=GAIKOTSU_PAGE)]


def test_is_erai():
    assert erai.is_erai(ERAI_EP) is True
    assert erai.is_erai(NYAA_EP) is False


def test_split_noise_strips_quality_tokens():
    terms, quality = erai.split_noise(
        "Ranma 3rd Season 1080p WEB-DL AVC AAC")
    assert terms == "Ranma 3rd Season"
    assert quality == ["1080p", "WEB-DL", "AVC", "AAC"]


def test_split_noise_keeps_searchable_text():
    # Platform tags ("NF") are real post text; a year is not a resolution;
    # quoted phrases never match the whole-token pattern.
    terms, quality = erai.split_noise('Ranma NF 2024 "Multi Subs"')
    assert terms == 'Ranma NF 2024 "Multi Subs"'
    assert quality == []


def test_split_noise_strips_category_tokens():
    terms, quality = erai.split_noise("Ranma Airing")
    assert terms == "Ranma"
    assert quality == ["Airing"]


def test_matches_token_uses_metadata():
    item = Item("Ranma - 01 [Airing]", "l", resolution="1080p",
                infomkv="NF WEB-DL AVC AAC", linktype="Torrent")
    assert erai.matches_token(item, "torrent")   # linktype (stripped from titles)
    assert erai.matches_token(item, "web-dl")    # infomkv
    assert erai.matches_token(item, "1080p")     # resolution
    assert erai.matches_token(item, "airing")    # title text
    assert not erai.matches_token(item, "hevc")


def test_metadata_hit_ignores_items_without_erai_fields():
    assert not erai.metadata_hit(Item("Show 1080p WEB-DL", "l"), "1080p")


HINT = "(dimmed = matched in feed metadata, not searched)"


def test_render_query_dims_quality_tokens_on_erai():
    out = erai.render_query("Ranma 1080p WEB-DL", ERAI_EP)
    assert ansi_strip(out) == f"Ranma 1080p WEB-DL {HINT}"
    assert f"{C_DIM}1080p{C_OFF}" in out
    assert f"{C_DIM}WEB-DL{C_OFF}" in out


def test_render_query_plain_when_nothing_to_dim():
    q = "Ranma 3rd Season"
    assert erai.render_query(q, ERAI_EP) == q


def test_render_query_hint_suppressible():
    out = erai.render_query("Ranma 1080p", ERAI_EP, hint=False)
    assert HINT not in out
    assert f"{C_DIM}1080p{C_OFF}" in out


def test_render_query_verbatim_for_non_erai():
    q = "show 1080p"
    assert erai.render_query(q, NYAA_EP) == q


def _s_param(url: str) -> str:
    """The value of the s= search parameter (the only part a query can
    pollute; the endpoint template's own res=/type= params are fixed)."""
    for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query):
        if k == "s":
            return v
    return ""


def test_feed_url_erai_strips_quality_tokens():
    url, excluded = endpoints.feed_url(ERAI_EP, "Ranma 1080p WEB-DL -hevc")
    assert _s_param(url) == "Ranma"
    assert excluded == ["hevc"]


def test_fetch_items_erai_searches_clean_and_filters_quality(monkeypatch):
    urls = []

    def fake_fetch_rss(url, endpoint_name="feed", timeout=10.0):
        urls.append(url)
        return [
            Item("Ranma - 01 [1080p NF WEB-DL AVC AAC][Airing]", "dl",
                 resolution="1080p", infomkv="NF WEB-DL AVC AAC",
                 linktype="Torrent"),
            Item("Ranma - 01 (HEVC) [1080p NF WEBRip HEVC AAC][Encoded]",
                 "rip", resolution="1080p", infomkv="NF WEBRip HEVC AAC",
                 linktype="Torrent"),
        ]

    monkeypatch.setattr(nyaa, "fetch_rss", fake_fetch_rss)
    items = endpoints.fetch_items(ERAI_EP, "Ranma 1080p WEB-DL")
    # The search went out with the show name only — the quality tokens would
    # zero it out — and the facet was applied to the results client-side.
    assert _s_param(urls[0]) == "Ranma"
    assert [it.link for it in items] == ["dl"]


def test_subscription_feed_erai_fallback_puts_quality_in_must(monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: None)
    url, must, excluded = endpoints.subscription_feed(
        ERAI_EP, "Ranma 1080p WEB-DL -hevc", [])
    assert _s_param(url) == "Ranma"
    # qB enforces the quality facets against the raw feed titles instead.
    assert must == "1080p WEB-DL"
    assert excluded == ["hevc"]


def test_show_slug_per_section():
    assert erai._show_slug("episodes", "gaikotsu-kishi-sama-02") == \
        "gaikotsu-kishi-sama"
    # A show whose own slug ends in a number must survive the strip.
    assert erai._show_slug("episodes", "mob-psycho-100-05") == "mob-psycho-100"
    assert erai._show_slug("encodes", "gaikotsu-kishi-sama-01-hevc") == \
        "gaikotsu-kishi-sama"
    assert erai._show_slug("batches", "86-eighty-six-01-11") == "86-eighty-six"


def test_per_show_feed_url_carries_params_drops_search():
    url = erai.per_show_feed_url(ERAI_EP, "86-eighty-six")
    assert url.startswith(
        "https://www.erai-raws.info/anime-list/86-eighty-six/feed/?")
    assert "res=1080p" in url and "type=torrent" in url
    assert "subs%5B0%5D=us" in url and "token=TOK" in url
    assert "{query}" not in url and "&s=" not in url and "?s=" not in url


def test_resolve_show_feed_success():
    calls = []

    def fetch(url, name="feed"):
        calls.append(url)
        return [Item("x", "l")]

    url = erai.resolve_show_feed(ERAI_EP, _erai_items(), fetch=fetch)
    assert url is not None
    assert ("/anime-list/gaikotsu-kishi-sama-tadaima-isekai-e-odekake-chuu-ii"
            "/feed/") in url
    assert calls == [url]


def test_resolve_show_feed_empty_probe_falls_back():
    assert erai.resolve_show_feed(
        ERAI_EP, _erai_items(), fetch=lambda url, name="feed": []) is None


def test_resolve_show_feed_fetch_error_falls_back():
    def fetch(url, name="feed"):
        raise nyaa.FetchError("boom")

    assert erai.resolve_show_feed(ERAI_EP, _erai_items(), fetch=fetch) is None


def test_resolve_show_feed_without_page_makes_no_request():
    def fetch(url, name="feed"):
        raise AssertionError("must not fetch")

    assert erai.resolve_show_feed(ERAI_EP, [Item("t", "l")], fetch=fetch) is None


def test_subscription_feed_erai_uses_per_show_and_must_contain(monkeypatch):
    monkeypatch.setattr(
        erai, "probe_show_feed",
        lambda ep, items, fetch=None: (
            "https://www.erai-raws.info/anime-list/x/feed/?res=1080p", []))
    url, must, excluded = endpoints.subscription_feed(
        ERAI_EP, "Gaikotsu 1080p CR WEB-DL -hevc", _erai_items())
    assert url == "https://www.erai-raws.info/anime-list/x/feed/?res=1080p"
    assert must == "Gaikotsu 1080p CR WEB-DL"
    assert excluded == ["hevc"]


def test_subscription_feed_must_contain_drops_poster_and_quotes(monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: ("https://e/f", []))
    _, must, _ = endpoints.subscription_feed(
        ERAI_EP, '[Erai-raws] Show "Multi Subs" 1080p', [])
    assert must == "Show Multi Subs 1080p"


# The per-show feed for Kyouran Reijou Nia Liston: its titles never name the
# group, so the `erai` its rule carried (#22) matched nothing for weeks.
NIA_SHOWN = [Item(f"Kyouran Reijou Nia Liston - 0{n} [1080p HIDIVE WEB-DL AVC "
                  "AAC][us][Airing]", "l", resolution="1080p", linktype="Torrent")
             for n in (1, 2)]


def test_subscription_feed_drops_words_the_show_feed_never_carries(monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: ("https://e/f", NIA_SHOWN))
    _, must, _ = endpoints.subscription_feed(
        ERAI_EP, "Kyouran Reijou Nia Liston 1080p erai 1080p", [])
    assert must == "Kyouran Reijou Nia Liston 1080p"


def test_rule_words_keeps_metadata_only_words_and_each_word_once():
    # "Torrent" is the raw title's linktype prefix, which qB matches on.
    assert erai.rule_words("Nia nia Torrent HIDIVE", NIA_SHOWN) == \
        "Nia Torrent HIDIVE"


def test_probe_show_feed_returns_the_items_it_found():
    found = [Item("x", "l")]
    url, shown = erai.probe_show_feed(
        ERAI_EP, _erai_items(), fetch=lambda url, name="feed": found)
    assert "/anime-list/" in url
    assert shown == found


def test_subscription_feed_erai_falls_back_to_search_feed(monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: None)
    url, must, excluded = endpoints.subscription_feed(
        ERAI_EP, "gaikotsu -hevc", [])
    assert "s=gaikotsu" in url
    assert must == ""
    assert excluded == ["hevc"]


def test_subscription_feed_non_erai_unchanged():
    url, must, excluded = endpoints.subscription_feed(NYAA_EP, "show -bad", [])
    assert url == endpoints.feed_url(NYAA_EP, "show -bad")[0]
    assert must == ""
    assert excluded == []


# The two releases Erai's feed carries for every episode (Kyoufu Collector,
# whose `kyoufu 1080p` rule downloaded both).
KYOUFU_AVC = ("[Torrent] Kyoufu Collector - 01 [1080p CR WEB-DL AVC AAC]"
              "[us][br][mx][es][sa][fr][de][it][ru][pl][Airing]")
KYOUFU_HEVC = ("[Torrent] Kyoufu Collector - 01 (HEVC) [1080p CR WEBRip HEVC "
               "AAC][us][br][mx][es][sa][fr][de][it][ru][pl][Encoded]")


def _qb_accepts(title, must, excluded):
    """qB's non-regex rule match: every mustContain token and none of the
    mustNotContain expressions, as case-insensitive substrings."""
    lc = title.lower()
    return (all(t.lower() in lc for t in must.split())
            and not any(x.lower() in lc for x in excluded))


def test_variant_exclusions_neutral_query_drops_the_encode():
    assert erai.variant_exclusions("Kyoufu 1080p") == ["HEVC"]
    # WEB alone matches both WEB-DL and WEBRip, so it picks nothing.
    assert erai.variant_exclusions("Show WEB AAC") == ["HEVC"]


def test_variant_exclusions_respects_a_picked_variant():
    for query in ("Show 1080p WEB-DL", "Show HEVC", "Show webrip",
                  "Show -hevc", 'Show -"WEB-DL"', "Show x265", "Show AVC"):
        assert erai.variant_exclusions(query) == [], query


def test_subscription_feed_per_show_downloads_one_release_per_episode(
        monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: ("https://e/f", []))
    _, must, excluded = endpoints.subscription_feed(
        ERAI_EP, "kyoufu 1080p", [])
    assert excluded == ["HEVC"]
    kept = [t for t in (KYOUFU_AVC, KYOUFU_HEVC)
            if _qb_accepts(t, must, excluded)]
    assert kept == [KYOUFU_AVC]


def test_subscription_feed_hevc_query_keeps_only_the_encode(monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: ("https://e/f", []))
    _, must, excluded = endpoints.subscription_feed(
        ERAI_EP, "kyoufu 1080p HEVC", [])
    assert excluded == []
    kept = [t for t in (KYOUFU_AVC, KYOUFU_HEVC)
            if _qb_accepts(t, must, excluded)]
    assert kept == [KYOUFU_HEVC]


def test_subscription_feed_search_fallback_also_drops_the_encode(monkeypatch):
    monkeypatch.setattr(erai, "probe_show_feed",
                        lambda ep, items, fetch=None: None)
    _, _, excluded = endpoints.subscription_feed(ERAI_EP, "Ranma 1080p", [])
    assert excluded == ["HEVC"]
