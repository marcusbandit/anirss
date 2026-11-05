"""RSS fetching and parsing: nyaa stat extensions plus generic-feed fallbacks."""

import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from anirss_lib.errors import FetchError
from anirss_lib.logging import log
from anirss_lib.types import Item


NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
ERAI_NS = {"erai": "https://www.erai-raws.info/rss-page/"}
TORZNAB_NS = {"torznab": "http://torznab.com/schemas/2015/feed"}

# Newznab top-level category ids (Jackett/Prowlarr torznab feeds).
_TORZNAB_CATEGORIES = {
    "1": "Console", "2": "Movies", "3": "Audio", "4": "PC", "5": "TV",
    "6": "XXX", "7": "Books", "8": "Other",
}


def _opt_int_text(elem: ET.Element | None) -> int | None:
    """Stat from a namespaced element, or None when the feed doesn't carry
    it. None is "unknown" and must never render as 0 (a real zero seed
    count is dead; an absent count is just absent)."""
    if elem is None or not elem.text:
        return None
    try:
        return int(elem.text.strip())
    except ValueError:
        return None


def _str_text(elem: ET.Element | None) -> str:
    if elem is None or not elem.text:
        return ""
    return elem.text.strip()


def _human_size(n: int) -> str:
    """Bytes in nyaa's own notation ("284.7 MiB")."""
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024.0 or unit == "TiB":
            return f"{int(f)} B" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{n} B"


def _torznab(entry: ET.Element) -> tuple[int | None, int | None, int | None,
                                         str, str]:
    """(seeders, leechers, grabs, size, category) from a torznab item, all
    empty/None when the item has no torznab:attr elements. Torznab reports
    peers as seeders + leechers."""
    attrs: dict[str, str] = {}
    for attr in entry.findall("torznab:attr", TORZNAB_NS):
        name, value = attr.get("name"), attr.get("value")
        if name and value is not None:
            attrs.setdefault(name, value)
    if not attrs:
        return None, None, None, "", ""

    def num(key: str) -> int | None:
        try:
            return int(attrs[key])
        except (KeyError, ValueError):
            return None

    seeders, peers = num("seeders"), num("peers")
    leechers = max(0, peers - seeders) \
        if peers is not None and seeders is not None else None
    size = ""
    raw = _str_text(entry.find("size")) or attrs.get("size", "")
    if raw.isdigit():
        size = _human_size(int(raw))
    category = _TORZNAB_CATEGORIES.get(attrs.get("category", "")[:1], "") \
        if len(attrs.get("category", "")) == 4 else ""
    return seeders, leechers, num("grabs"), size, category


_DESC_SIZE_RE = re.compile(r"Size:\s*([\d.,]+\s*[KMGT]?i?B)", re.IGNORECASE)
_DESC_CAT_RE = re.compile(r"Category:\s*([^|]+)")
_HREF_RE = re.compile(r'href="([^"]+)"')


def _item_link(entry: ET.Element, link_text: str) -> str:
    """Prefer a torrent-file enclosure over <link>: on generic trackers the
    <link> is a web page, while the enclosure is the actual .torrent."""
    enclosure = entry.find("enclosure")
    if enclosure is not None:
        enc_url = (enclosure.get("url") or "").strip()
        enc_type = (enclosure.get("type") or "").lower()
        if enc_url and ("bittorrent" in enc_type
                        or enc_url.lower().endswith(".torrent")):
            return enc_url
    return link_text


def _erai_polish(entry: ET.Element, title: str
                 ) -> tuple[str, str, str, str, str, str, str]:
    """(title, size, category, page, resolution, infomkv, linktype) for an
    Erai-raws feed item, or the title untouched with empty extras when the
    item has no erai: extensions.

    Erai titles read "[Torrent] Show - 02 [tags][us][br]...[Airing]": the
    linktype prefix and the subtitle-language run are noise (the languages
    live in erai:subtitles), while the trailing [Airing]/[Encoded]/[Batch]
    tag is kept as a real refine facet. The post URL from <description>
    becomes Item.page so the per-show feed slug can be derived from it.
    resolution/infomkv/linktype are the structured quality facets — they
    exist because the s= search can't match them (see erai.split_noise)."""
    linktype = _str_text(entry.find("erai:linktype", ERAI_NS))
    if not linktype:
        return title, "", "", "", "", "", ""
    if title.startswith(f"[{linktype}] "):
        title = title[len(linktype) + 3:]
    subs_tags = _str_text(entry.find("erai:subtitles", ERAI_NS))
    if subs_tags:
        title = title.replace(subs_tags, "")
    size = _str_text(entry.find("erai:size", ERAI_NS))
    category = _str_text(entry.find("erai:category", ERAI_NS)).strip("[]")
    resolution = _str_text(entry.find("erai:resolution", ERAI_NS))
    infomkv = _str_text(entry.find("erai:infomkv", ERAI_NS))
    href = _HREF_RE.search(_str_text(entry.find("description")))
    return title, size, category, href.group(1) if href else "", \
        resolution, infomkv, linktype


def parse_rss(data: bytes | str, endpoint_name: str = "feed") -> list[Item]:
    """Parse an RSS feed into Items. Reads nyaa's stat extensions when
    present; otherwise falls back to Size:/Category: hints in <description>.
    Raises FetchError on malformed XML."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FetchError(f"bad feed from {endpoint_name}: {e}") from e
    out: list[Item] = []
    for entry in root.iter("item"):
        title = entry.find("title")
        link = entry.find("link")
        if title is None or not title.text or link is None or not link.text:
            continue
        title_text = title.text.strip()
        title_text, erai_size, erai_cat, page, resolution, infomkv, linktype = \
            _erai_polish(entry, title_text)
        tz_seeders, tz_leechers, tz_grabs, tz_size, tz_cat = _torznab(entry)
        size = (_str_text(entry.find("nyaa:size", NYAA_NS)) or erai_size
                or tz_size)
        category = (_str_text(entry.find("nyaa:category", NYAA_NS))
                    or erai_cat or tz_cat)
        if not size or not category:
            # CDATA descriptions arrive HTML-escaped (AniRena writes
            # "Anime &gt; Subs"), so unescape what the regexes pull out.
            desc = html.unescape(_str_text(entry.find("description")))
            if desc:
                if not size and (m := _DESC_SIZE_RE.search(desc)):
                    size = m.group(1).strip()
                if not category and (m := _DESC_CAT_RE.search(desc)):
                    category = m.group(1).strip()
        # Generic trackers prefix titles with "[<category>] ": strip it so
        # release-group detection doesn't mistake the category for a poster.
        if category and title_text.startswith(f"[{category}] "):
            title_text = title_text[len(category) + 3:]
        out.append(Item(
            title=title_text,
            link=_item_link(entry, link.text.strip()),
            seeders=_opt_int_text(entry.find("nyaa:seeders", NYAA_NS))
            if tz_seeders is None else tz_seeders,
            leechers=_opt_int_text(entry.find("nyaa:leechers", NYAA_NS))
            if tz_leechers is None else tz_leechers,
            downloads=_opt_int_text(entry.find("nyaa:downloads", NYAA_NS))
            if tz_grabs is None else tz_grabs,
            size=size,
            category=category,
            page=page,
            resolution=resolution,
            infomkv=infomkv,
            linktype=linktype,
        ))
    return out


def fetch_rss(url: str, endpoint_name: str = "feed",
              timeout: float = 10.0) -> list[Item]:
    """GET `url` and parse it as an RSS feed. Raises FetchError on failure."""
    items = parse_rss(fetch_bytes(url, endpoint_name, timeout), endpoint_name)
    log("INFO", f"  -> {len(items)} items")
    return items


def fetch_bytes(url: str, endpoint_name: str = "feed",
                timeout: float = 10.0) -> bytes:
    """GET `url` the way a feed is fetched. Raises FetchError on failure."""
    log("INFO", f"GET {url}")
    # erai-raws (Cloudflare) 502s non-browser User-Agents like the old
    # "anirss/1.0", so present as a stock Firefox.
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) "
                      "Gecko/20100101 Firefox/130.0",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    try:
        # 10s by default, not urllib's usual generous one: a blackholed
        # endpoint (nyaa is silently dropped by some networks) must fail fast
        # so the fallback probe can move on instead of feeling like a hang.
        # Endpoints that are slow by nature raise it via `timeout`.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"can't reach {endpoint_name}: {e}") from e
