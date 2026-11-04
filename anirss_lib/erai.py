"""Erai-raws specifics: search-term hygiene and per-show feed resolution.

Erai's search endpoint (WordPress `s=` on the tokenized feed) is a plain
word-AND over post text — and the posts are titled "Show - 02 (NF)", with no
quality tags at all. A query like "Show 1080p WEB-DL" therefore zeroes the
search out: the quality facets live in the feed's erai: metadata instead
(erai:resolution, erai:infomkv, erai:linktype). split_noise() strips those
tokens from a query before it goes on the wire and matches_token() applies
them to the fetched results client-side, so the endpoint is searched with
the normal thing and the facets still narrow the results.

Beyond that, what Erai adds over a generic feed is a per-show feed at
/anime-list/<slug>/feed/ that carries only that show's current offerings.
Subscribing to it (instead of the search feed) keeps the qBittorrent feed
small and immune to search-term drift. The show slug is derived from the
release post URL each feed item links to, then validated with a single
fetch: a wrong slug returns HTTP 200 with an empty feed, so an empty probe
means "fall back to the search feed", never a hard failure.
"""

import re
import urllib.parse

from anirss_lib import nyaa
from anirss_lib.ansi import C_DIM, C_OFF
from anirss_lib.endpoints import FIELD_RE, Endpoint
from anirss_lib.logging import log
from anirss_lib.types import Item


_HOST = "erai-raws.info"

# Release post URL inside a feed item, e.g.
#   https://www.erai-raws.info/episodes/<show-slug>-02/
#   https://www.erai-raws.info/encodes/<show-slug>-01-hevc/
#   https://www.erai-raws.info/batches/<show-slug>-01-11/
_POST_RE = re.compile(
    r"https://www\.erai-raws\.info/"
    r"(episodes|encodes|batches|specials|raws)/([a-z0-9-]+)/")

# Suffix stripped from a post slug to recover the show slug. Only the
# section's own suffix is removed so shows whose slug ends in a number
# (mob-psycho-100) survive: episodes/specials/raws end in one episode
# number, encodes add -hevc after it, batches end in a range.
_POST_SUFFIX = {
    "episodes": re.compile(r"-\d+(?:v\d+)?$"),
    "specials": re.compile(r"-\d+(?:v\d+)?$"),
    "raws":     re.compile(r"-\d+(?:v\d+)?$"),
    "encodes":  re.compile(r"-\d+(?:v\d+)?-hevc$"),
    "batches":  re.compile(r"-\d+-\d+$"),
}


def is_erai(ep: Endpoint) -> bool:
    return ep.kind == "rss" and _HOST in ep.url


# Quality facets Erai keeps in erai:resolution / erai:infomkv / erai:linktype
# / erai:category metadata instead of the post text its s= search matches —
# the posts are titled "Show - 02 (NF)", so s="Show 1080p" finds nothing.
# Whole-token only: "1080p" is stripped from a query, "NF" (a real post-text
# platform tag) is not.
_QUALITY_RE = re.compile(
    r"^(?:"
    r"\d{3,4}p"                                # 1080p
    r"|web[\s._-]?(?:dl|rip)?"                 # WEB-DL / WEBRip / WEB
    r"|blu[\s._-]?ray|bd[\s._-]?(?:rip|mv)?"   # BluRay / BDRip / BD
    r"|hdtv|dvd"
    r"|avc|hevc|x[\s._-]?26[45]|h[\s._-]?26[45]|av1"
    r"|aac|flac|mp3|e?ac3|dts"
    r"|airing|encoded|batch"                   # erai:category values
    r")$", re.IGNORECASE)


def _field_core(field: str) -> str:
    """A query field without its leading `-` and surrounding quotes."""
    core = field.removeprefix("-")
    if len(core) >= 2 and core[0] == '"' and core[-1] == '"':
        core = core[1:-1]
    return core


def is_quality_token(field: str) -> bool:
    """Is this query field a quality facet (see _QUALITY_RE)? A leading `-`
    and quotes are stripped before the whole-token check."""
    core = _field_core(field)
    return bool(core) and bool(_QUALITY_RE.fullmatch(core))


# Erai posts every episode twice into the same feed: the AVC WEB-DL as it
# airs ([Airing]) and an HEVC re-encode hours later ([Encoded]). No feed
# parameter separates them, so a qB rule that names neither variant matches
# both and downloads each episode twice.
_VARIANT_RE = re.compile(
    r"^(?:avc|hevc|x[\s._-]?26[45]|h[\s._-]?26[45]"
    r"|web[\s._-]?(?:dl|rip)|airing|encoded)$", re.IGNORECASE)


def variant_exclusions(query: str) -> list[str]:
    """Rule exclusions that keep a subscription to one release per episode:
    ["HEVC"] (drop the re-encode, keep the airing WEB-DL) unless a field of
    `query`, positive or `-`excluded, already picks a variant."""
    if any(_VARIANT_RE.fullmatch(_field_core(f))
           for f in FIELD_RE.findall(query)):
        return []
    return ["HEVC"]


def split_noise(query: str) -> tuple[str, list[str]]:
    """(searchable terms, quality tokens) from a positive-terms query.

    The quality tokens are the facets Erai exposes as erai: metadata; the
    s= search only matches post text, so sending them zeroes it out. They
    come back for client-side filtering (matches_all) instead. Quoted
    phrases survive — they never match the whole-token pattern.
    """
    terms: list[str] = []
    quality: list[str] = []
    for field in FIELD_RE.findall(query):
        if is_quality_token(field):
            quality.append(field.strip('"'))
            continue
        terms.append(field)
    return " ".join(terms), quality


def render_query(query: str, ep: Endpoint, *, hint: bool = True) -> str:
    """The Query line as displayed on erai endpoints: quality facets dimmed
    — they never reach the s= search (that would zero it out), they match
    the erai: metadata client-side — with a hint saying so whenever any
    field is dimmed. Other endpoints get the query verbatim."""
    if not is_erai(ep):
        return query
    fields = FIELD_RE.findall(query)
    out = [f"{C_DIM}{f}{C_OFF}" if is_quality_token(f) else f
           for f in fields]
    if out != fields and hint:
        out.append(f"{C_DIM}(dimmed = matched in feed metadata, "
                   f"not searched){C_OFF}")
    return " ".join(out)


def metadata_hit(item: Item, token: str) -> bool:
    """Does `token` match the item's structured erai fields? resolution /
    category / linktype by equality, infomkv by substring. False for items
    without erai metadata (all fields empty), so nyaa and generic feeds are
    unaffected."""
    if not (item.resolution or item.infomkv or item.linktype):
        return False
    lc = token.lower()
    return (lc == item.resolution.lower()
            or lc in item.infomkv.lower()
            or lc == item.linktype.lower()
            or lc == item.category.lower())


def matches_token(item: Item, token: str) -> bool:
    """Client-side facet match for one item: title text first, then the
    structured erai fields — the polished title drops the linktype prefix,
    so e.g. 'Torrent' only exists there."""
    lc = token.lower()
    return lc in item.title.lower() or metadata_hit(item, lc)


def matches_all(item: Item, tokens: list[str]) -> bool:
    """Every token matches (see matches_token)."""
    return all(matches_token(item, t) for t in tokens)


def _show_slug(section: str, post_slug: str) -> str | None:
    """Show slug from a release post slug, None when nothing was stripped
    (an unrecognized shape is not trusted as a show slug)."""
    suffix = _POST_SUFFIX.get(section)
    if suffix is None:
        return None
    slug = suffix.sub("", post_slug)
    return slug if slug and slug != post_slug else None


def per_show_feed_url(ep: Endpoint, slug: str) -> str:
    """The anime-list feed for `slug`, carrying over the endpoint URL's
    res/type/subs/token params and dropping the s= search placeholder."""
    base, _, qs = ep.url.partition("?")
    params = [(k, v) for k, v in urllib.parse.parse_qsl(qs) if k != "s"]
    origin = base.split(_HOST)[0] + _HOST
    return f"{origin}/anime-list/{slug}/feed/?{urllib.parse.urlencode(params)}"


def probe_show_feed(ep: Endpoint, items: list[Item], fetch=None
                    ) -> tuple[str, list[Item]] | None:
    """(per-show feed URL, the items the probe found) for the show `items`
    belong to, or None to make the caller fall back to the search feed.
    Costs at most one request: the first item with a recognizable post URL
    decides. `fetch` is injectable for tests."""
    fetch = fetch or nyaa.fetch_rss
    for item in items:
        match = _POST_RE.search(item.page)
        if not match:
            continue
        slug = _show_slug(match.group(1), match.group(2))
        if not slug:
            continue
        url = per_show_feed_url(ep, slug)
        try:
            found = fetch(url, "erai per-show probe")
        except nyaa.FetchError as e:
            log("WARN", f"erai per-show probe failed: {e}")
            return None
        if found:
            log("INFO", f"erai per-show feed for {slug!r}: {len(found)} item(s)")
            return url, found
        log("WARN", f"erai slug {slug!r}: empty probe, using the search feed")
        return None
    return None


def resolve_show_feed(ep: Endpoint, items: list[Item], fetch=None) -> str | None:
    """The per-show feed URL alone (see probe_show_feed)."""
    probe = probe_show_feed(ep, items, fetch=fetch)
    return probe[0] if probe else None


def rule_words(positive: str, shown: list[Item]) -> str:
    """mustContain for a per-show subscription: the query's positive words,
    quotes unwrapped, poster fields dropped, each word once. qB ANDs every
    word against the feed titles, so a word no title in the per-show feed
    carries ("erai": the feed is the group already, its titles never name
    it) would stop the rule from ever matching. Such a word is dropped,
    judged against `shown`, the items the probe found. An empty `shown`
    drops nothing."""
    words: list[str] = []
    for field in FIELD_RE.findall(positive):
        if field.startswith("["):
            continue
        word = field.strip('"')
        if not word or any(w.lower() == word.lower() for w in words):
            continue
        if shown and not any(matches_token(it, word) for it in shown):
            log("WARN", f"erai rule drops {word!r}: no title in the "
                        f"show feed carries it")
            continue
        words.append(word)
    return " ".join(words)
