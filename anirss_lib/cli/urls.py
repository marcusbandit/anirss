"""URL classification + nyaa query extraction."""

import enum
import re
import urllib.parse


class UrlKind(enum.Enum):
    NOT_URL = enum.auto()
    ONE_SHOT = enum.auto()       # magnet: or remote *.torrent URL
    LOCAL_TORRENT = enum.auto()  # local *.torrent file path
    NYAA_RSS = enum.auto()
    OTHER_HTTP = enum.auto()


_TORRENT_RE = re.compile(r"\.torrent($|[?#])", re.IGNORECASE)


def classify_url(s: str) -> UrlKind:
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
        if host == "nyaa.si" or host.endswith(".nyaa.si"):
            return UrlKind.NYAA_RSS
        return UrlKind.OTHER_HTTP
    # Not a URL — but a *.torrent suffix means a local file. The caller
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
