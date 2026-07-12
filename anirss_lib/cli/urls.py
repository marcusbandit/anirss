"""URL classification + nyaa query extraction."""

import enum
import re
import urllib.parse


class UrlKind(enum.Enum):
    NOT_URL = enum.auto()
    ONE_SHOT = enum.auto()       # magnet: or remote *.torrent URL
    LOCAL_TORRENT = enum.auto()  # local *.torrent file path
    NYAA_RSS = enum.auto()
    ENDPOINT_RSS = enum.auto()
    OTHER_HTTP = enum.auto()


_TORRENT_RE = re.compile(r"\.torrent($|[?#])", re.IGNORECASE)


def classify_url(s: str, *, nyaa_hosts: frozenset[str] = frozenset(),
                 rss_hosts: frozenset[str] = frozenset()) -> UrlKind:
    if not s:
        return UrlKind.NOT_URL
    if s.startswith("magnet:"):
        return UrlKind.ONE_SHOT
    if s.startswith(("http://", "https://")):
        if _TORRENT_RE.search(s):
            return UrlKind.ONE_SHOT
        try:
            host = (urllib.parse.urlparse(s).hostname or "").lower()
        except ValueError:
            return UrlKind.OTHER_HTTP
        if host == "nyaa.si" or host.endswith(".nyaa.si") or host in nyaa_hosts:
            return UrlKind.NYAA_RSS
        if host in rss_hosts:
            return UrlKind.ENDPOINT_RSS
        return UrlKind.OTHER_HTTP
    # Not a URL - but a *.torrent suffix means a local file. The caller
    # checks existence and errors clearly if it's missing.
    if s.lower().endswith(".torrent"):
        return UrlKind.LOCAL_TORRENT
    return UrlKind.NOT_URL


def extract_nyaa_query(url: str) -> str | None:
    """Pull the `q=` parameter out of a nyaa URL, decoded. Returns None if absent/empty."""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        return None
    values = qs.get("q") or []
    if not values:
        return None
    q = values[0].strip()
    return q or None


def endpoint_hosts(endpoints) -> tuple[frozenset[str], frozenset[str]]:
    """(nyaa_hosts, rss_hosts) for the configured endpoints, lowercased.
    Accepts anything with .kind and .url so it doesn't import Endpoint."""
    nyaa_hosts: set[str] = set()
    rss_hosts: set[str] = set()
    for ep in endpoints:
        host = (urllib.parse.urlparse(ep.url).hostname or "").lower()
        if not host:
            continue
        (nyaa_hosts if ep.kind == "nyaa" else rss_hosts).add(host)
    return frozenset(nyaa_hosts), frozenset(rss_hosts)
