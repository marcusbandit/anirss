"""RSS fetching and parsing: nyaa stat extensions plus generic-feed fallbacks."""

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from anirss_lib.logging import log
from anirss_lib.types import Item


NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}


def _int_text(elem: ET.Element | None) -> int:
    if elem is None or not elem.text:
        return 0
    try:
        return int(elem.text.strip())
    except ValueError:
        return 0


def _str_text(elem: ET.Element | None) -> str:
    if elem is None or not elem.text:
        return ""
    return elem.text.strip()


class FetchError(Exception):
    """A search endpoint couldn't be fetched or parsed."""


_DESC_SIZE_RE = re.compile(r"Size:\s*([\d.,]+\s*[KMGT]?i?B)", re.IGNORECASE)
_DESC_CAT_RE = re.compile(r"Category:\s*([^|]+)")


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
        size = _str_text(entry.find("nyaa:size", NYAA_NS))
        category = _str_text(entry.find("nyaa:category", NYAA_NS))
        if not size or not category:
            desc = _str_text(entry.find("description"))
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
            seeders=_int_text(entry.find("nyaa:seeders", NYAA_NS)),
            leechers=_int_text(entry.find("nyaa:leechers", NYAA_NS)),
            downloads=_int_text(entry.find("nyaa:downloads", NYAA_NS)),
            size=size,
            category=category,
        ))
    return out


def fetch_rss(url: str, endpoint_name: str = "feed") -> list[Item]:
    """GET `url` and parse it as an RSS feed. Raises FetchError on failure."""
    log("INFO", f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "anirss/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"can't reach {endpoint_name}: {e}") from e
    items = parse_rss(data, endpoint_name)
    log("INFO", f"  -> {len(items)} items")
    return items
