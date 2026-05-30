"""nyaa.si RSS fetching."""

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from anirss_lib.config import SearchConfig
from anirss_lib.logging import die, log
from anirss_lib.types import Item


NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}


def search_url(query: str, search: SearchConfig) -> str:
    qs = urllib.parse.urlencode({
        "page": "rss",
        "q": query,
        "c": search["category"],
        "f": search["filter"],
    })
    return f"{search['nyaa_url']}?{qs}"


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


def _fetch_items_from_url(url: str) -> list[Item]:
    log("INFO", f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "anirss/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        die(f"can't reach nyaa: {e}")
    root = ET.fromstring(data)
    out: list[Item] = []
    for entry in root.iter("item"):
        title = entry.find("title")
        link = entry.find("link")
        if title is not None and title.text and link is not None and link.text:
            out.append(Item(
                title=title.text.strip(),
                link=link.text.strip(),
                seeders=_int_text(entry.find("nyaa:seeders", NYAA_NS)),
                leechers=_int_text(entry.find("nyaa:leechers", NYAA_NS)),
                downloads=_int_text(entry.find("nyaa:downloads", NYAA_NS)),
                size=_str_text(entry.find("nyaa:size", NYAA_NS)),
                category=_str_text(entry.find("nyaa:category", NYAA_NS)),
            ))
    log("INFO", f"  -> {len(out)} items")
    return out


def fetch_items(query: str, search: SearchConfig) -> list[Item]:
    """Return list of Items from the nyaa RSS for `query`."""
    return _fetch_items_from_url(search_url(query, search))
