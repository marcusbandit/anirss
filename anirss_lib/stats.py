"""Tracker stats for feeds that don't carry them (Erai-raws et al.).

A feed item without nyaa-style stat extensions renders as unknown (`–`), but
the numbers do exist: they live at the torrent's tracker, which is exactly
where qBittorrent reads them from after adding. This module does the same
without adding the torrent:

1. download the small .torrent file (cached by URL on disk),
2. pull the info-hash + announce list out of its bencode,
3. scrape the trackers (HTTP batch first, UDP fallback), which answers
   seeders / leechers / completed per info-hash in one tiny request.

`enrich()` is the only entry point the flows call. It never raises: any
failure leaves the stats unknown and logs. Results are cached on disk with a
TTL, so keystroke-driven live previews and refine refetches mostly cost
nothing.
"""

import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from anirss_lib.config import STATE_DIR
from anirss_lib.logging import log
from anirss_lib.types import Item


CACHE_PATH = STATE_DIR / "stats.json"
CACHE_TTL_SECONDS = 2 * 60 * 60          # tracker counts go stale quickly
HTTP_SCRAPE_TIMEOUT = 8                  # seconds per HTTP scrape request
UDP_SCRAPE_TIMEOUT = 5                   # seconds for the UDP connect+scrape
TORRENT_TIMEOUT = 10                     # seconds per .torrent download
MAX_HTTP_BATCH = 50                      # info-hashes per HTTP scrape
MAX_UDP_BATCH = 60                       # keeps the datagram well under MTU
MAX_TRACKERS_TRIED = 4
DOWNLOAD_WORKERS = 8

_USER_AGENT = "anirss/1.0"
_UDP_MAGIC = 0x41727101980               # BEP 15 protocol id

# Stats triple as stored/cached: (seeders, leechers, downloads)
Stats = tuple[int, int, int]


# ---- minimal bencode (read-only) ----

def _decode(data: bytes, pos: int) -> tuple[object, int]:
    """Decode one bencode value at `pos`; return (value, offset_past_it)."""
    c = data[pos:pos + 1]
    if c == b"d":
        pos += 1
        d: dict[bytes, object] = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _decode(data, pos)
            val, pos = _decode(data, pos)
            d[key] = val
        return d, pos + 1
    if c == b"l":
        pos += 1
        out: list[object] = []
        while data[pos:pos + 1] != b"e":
            val, pos = _decode(data, pos)
            out.append(val)
        return out, pos + 1
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    colon = data.index(b":", pos)
    n = int(data[pos:colon])
    return data[colon + 1:colon + 1 + n], colon + 1 + n


def info_hash(data: bytes) -> bytes:
    """SHA-1 info-hash of .torrent bytes (trackerless torrents too). Raises
    ValueError on anything that doesn't parse."""
    start = data.index(b"4:info") + len(b"4:info")
    _info, end = _decode(data, start)
    return hashlib.sha1(data[start:end]).digest()


def torrent_facts(data: bytes) -> tuple[bytes, list[str]]:
    """(info-hash bytes, announce URLs in priority order) from .torrent bytes.

    Raises ValueError on anything that doesn't parse; callers treat that as
    "can't scrape this one".
    """
    start = data.index(b"4:info") + len(b"4:info")
    _info, end = _decode(data, start)
    infohash = hashlib.sha1(data[start:end]).digest()

    single: str | None = None
    from_list: list[str] = []
    pos = 1  # skip the leading dict marker
    while pos < len(data) - 1 and data[pos:pos + 1] != b"e":
        key, pos = _decode(data, pos)
        val_start = pos
        val, pos = _decode(data, pos)
        if key == b"announce-list" and not from_list:
            flat = val if isinstance(val, list) else [val]
            for entry in flat:
                for url in (entry if isinstance(entry, list) else [entry]):
                    text = url.decode("utf-8", "replace") \
                        if isinstance(url, bytes) else ""
                    if text.startswith(("http://", "https://", "udp://")):
                        from_list.append(text)
        elif key == b"announce" and single is None:
            single = val.decode("utf-8", "replace") \
                if isinstance(val, bytes) else ""
        if val_start >= start:
            break  # inside/past the info dict: top-level walk is done
    announces = from_list or ([single] if single else [])
    if not announces:
        raise ValueError("no announce URL")
    return infohash, announces


# ---- scraping ----

def _bencode_nested_end(data: bytes, pos: int) -> int:
    """Byte offset just past the nested value starting at `pos`."""
    depth = 0
    while True:
        c = data[pos:pos + 1]
        if c in (b"d", b"l"):
            depth += 1
            pos += 1
        elif c == b"i":
            pos = data.index(b"e", pos) + 1
        elif c == b"e":
            depth -= 1
            pos += 1
            if depth == 0:
                return pos
        else:
            colon = data.index(b":", pos)
            pos = colon + 1 + int(data[pos:colon])


def _http_scrape(announce: str, hashes: list[bytes]) -> dict[bytes, Stats]:
    """Scrape an http(s) tracker (announce -> scrape, BEP 48 batching)."""
    parts = urllib.parse.urlsplit(announce)
    path = parts.path or "/announce"
    if parts.scheme not in ("http", "https") or not path.endswith("/announce"):
        return {}
    scrape_url = parts._replace(path=path[:-len("announce")] + "scrape").geturl()
    out: dict[bytes, Stats] = {}
    for i in range(0, len(hashes), MAX_HTTP_BATCH):
        batch = hashes[i:i + MAX_HTTP_BATCH]
        qs = "&".join(f"info_hash={urllib.parse.quote(h)}" for h in batch)
        req = urllib.request.Request(f"{scrape_url}?{qs}",
                                     headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_SCRAPE_TIMEOUT) as resp:
                body = resp.read()
            out.update(_parse_http_scrape(body))
        except Exception as e:
            log("WARN", f"scrape {scrape_url.split('?')[0]}: {e}")
    return out


def _parse_http_scrape(body: bytes) -> dict[bytes, Stats]:
    """Parse `d5:filesd20:<hash>d8:completei N e10:incompletei N e10:downloadedi N e …ee`."""
    files_at = body.index(b"d5:filesd") + len(b"d5:filesd")
    out: dict[bytes, Stats] = {}
    pos = files_at
    while body[pos:pos + 3] == b"20:":
        h = body[pos + 3:pos + 23]
        seg_end = _bencode_nested_end(body, pos + 23)
        seg = body[pos + 23:seg_end]
        out[h] = (_bkey_int(seg, b"8:complete"),
                  _bkey_int(seg, b"10:incomplete"),
                  _bkey_int(seg, b"10:downloaded"))
        pos = seg_end
    return out


def _bkey_int(seg: bytes, key: bytes) -> int:
    m = re.search(re.escape(key) + rb"i(-?\d+)e", seg)
    return int(m.group(1)) if m else 0


def _udp_scrape(announce: str, hashes: list[bytes]) -> dict[bytes, Stats]:
    """UDP tracker scrape (BEP 15): connect for a lease, then action 2.

    Note the UDP scrape response's per-hash order is (seeders, completed,
    leechers) — completed and leechers swap places vs the HTTP answer.
    """
    parts = urllib.parse.urlsplit(announce)
    if parts.scheme != "udp" or not parts.hostname:
        return {}
    host, port = parts.hostname, parts.port or 80
    hashes = hashes[:MAX_UDP_BATCH]
    out: dict[bytes, Stats] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(UDP_SCRAPE_TIMEOUT)
    try:
        tid = int.from_bytes(os.urandom(4), "big")
        sock.sendto(struct.pack(">QII", _UDP_MAGIC, 0, tid), (host, port))
        resp, _ = sock.recvfrom(2048)
        if len(resp) < 16:
            return {}
        action, _rtid, conn_id = struct.unpack(">IIQ", resp[:16])
        if action != 0:
            return {}
        payload = struct.pack(">QII", conn_id, 2, tid + 1) + b"".join(hashes)
        sock.sendto(payload, (host, port))
        resp, _ = sock.recvfrom(8 + 12 * len(hashes))
        if len(resp) < 8:
            return {}
        action, _rtid = struct.unpack(">II", resp[:8])
        if action != 2:
            return {}
        for i, h in enumerate(hashes):
            off = 8 + 12 * i
            if off + 12 > len(resp):
                break
            seeders, completed, leechers = struct.unpack(">III", resp[off:off + 12])
            out[h] = (seeders, leechers, completed)
    except (OSError, struct.error) as e:
        log("WARN", f"udp scrape {host}:{port}: {e}")
    finally:
        sock.close()
    return out


def _scrape_tracker(announce: str, hashes: list[bytes]) -> dict[bytes, Stats]:
    return _http_scrape(announce, hashes) or _udp_scrape(announce, hashes)


# ---- .torrent downloads + cache ----

# What a URL path may hold raw: RFC 3986 pchar, plus '/' and '%' so an
# already-encoded URL passes through unchanged. A query may also hold '?'.
_PATH_SAFE = "/%:@!$&'()*+,;="
_QUERY_SAFE = _PATH_SAFE + "?"


def encode_http_link(link: str) -> str:
    """Percent-encode what an http(s) URL can't hold raw.

    Feed <link>s carry raw spaces and brackets (t.erai-raws.info does). The
    host is left alone so an IPv6 literal keeps its brackets. Raises
    ValueError on a malformed host, e.g. an unclosed '['."""
    parts = urllib.parse.urlsplit(link)
    return urllib.parse.urlunsplit(parts._replace(
        path=urllib.parse.quote(parts.path, safe=_PATH_SAFE),
        query=urllib.parse.quote(parts.query, safe=_QUERY_SAFE),
        fragment=urllib.parse.quote(parts.fragment, safe=_QUERY_SAFE)))


def _fetch_torrent(url: str, timeout: float = TORRENT_TIMEOUT) -> bytes | None:
    try:
        req = urllib.request.Request(encode_http_link(url),
                                     headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log("WARN", f"torrent download failed ({url.split('?')[0]}): {e}")
        return None


def fetch_torrents(urls: list[str], *, deadline_s: float) -> dict[str, bytes]:
    """The .torrent bytes behind each URL, DOWNLOAD_WORKERS at a time.

    A URL that fails, misses the deadline or isn't a torrent is left out.
    Each attempt's own timeout shrinks with the time that's left, so even a
    fully stalled server can only cost about deadline_s in total."""
    deadline = time.monotonic() + deadline_s
    out: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_torrent, url,
                        max(0.5, deadline - time.monotonic())): url
            for url in dict.fromkeys(urls)
        }
        try:
            for fut in as_completed(futures,
                                    timeout=max(1.0, deadline - time.monotonic())):
                try:
                    data = fut.result()
                    if data:
                        info_hash(data)
                        out[futures[fut]] = data
                except Exception:
                    continue
        except TimeoutError:
            for fut in futures:
                fut.cancel()
    return out


_MEM_CACHE: dict[str, tuple[float, Stats]] = {}
# The API server runs enrich() from several request threads at once; this
# guards _MEM_CACHE mutation and the single stats.json writer.
_CACHE_LOCK = threading.Lock()


def _load_disk_cache() -> None:
    with _CACHE_LOCK:
        _load_disk_cache_locked()


def _load_disk_cache_locked() -> None:
    if _MEM_CACHE:
        return
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        for url, entry in raw.get("urls", {}).items():
            stats_ = tuple(entry[1:4])
            _MEM_CACHE[url] = (entry[0], stats_ if stats_[0] is not None else None)
    except (OSError, ValueError, IndexError, TypeError):
        pass


def _save_disk_cache() -> None:
    with _CACHE_LOCK:
        _save_disk_cache_locked()


def _save_disk_cache_locked() -> None:
    now = time.time()
    fresh = {url: [ts, *(stats if stats else (None, None, None))]
             for url, (ts, stats) in _MEM_CACHE.items()
             if now - ts < CACHE_TTL_SECONDS}
    try:
        os.makedirs(CACHE_PATH.parent, exist_ok=True)
        tmp = f"{CACHE_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "urls": fresh}, f)
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        log("WARN", f"stats cache write failed: {e}")


def _cached_stats(url: str) -> tuple[str, Stats | None] | None:
    """("ok", stats) / ("fail", None) for a fresh entry, None for a miss."""
    hit = _MEM_CACHE.get(url)
    if not hit or time.time() - hit[0] >= CACHE_TTL_SECONDS:
        return None
    return ("ok", hit[1]) if hit[1] else ("fail", None)


# ---- public entry point ----

def enrich(items: list[Item], *, deadline_s: float = 20.0) -> list[Item]:
    """Fill in unknown seed/peer/download counts from the items' trackers.

    Items whose feed already carries stats (nyaa) pass through untouched.
    For the rest, stats come from scraping the .torrent's trackers — the
    same numbers qBittorrent shows after adding. Never raises; on any
    failure the item keeps its unknown stats. Cheap when everything is
    cached (one mtime read).
    """
    if not items:
        return items
    deadline = time.monotonic() + deadline_s
    _load_disk_cache()

    by_url: dict[str, list[int]] = {}      # torrent URL -> item indexes
    resolved: dict[int, Stats] = {}
    for i, item in enumerate(items):
        # Torznab feeds carry live seeders/leechers but often no download
        # count; scraping would only re-fetch numbers already in hand.
        if item.seeders is not None and item.leechers is not None:
            continue
        if item.link.startswith(("http://", "https://")):
            cached = _cached_stats(item.link)
            if cached is None:
                by_url.setdefault(item.link, []).append(i)
            elif cached[0] == "ok":
                resolved[i] = cached[1]
            # "fail": known-bad URL — leave unknown without re-fetching.
    if not by_url:
        return [it if i not in resolved else _with_stats(it, resolved[i])
                for i, it in enumerate(items)]

    # Download the missing .torrent files in parallel, honoring the deadline.
    facts: dict[str, tuple[bytes, list[str]]] = {}
    fetched = fetch_torrents(list(by_url), deadline_s=deadline - time.monotonic())
    for url, data in fetched.items():
        try:
            facts[url] = torrent_facts(data)
        except ValueError:
            continue

    # Scrape per tracker in announce-priority order until everything lands.
    pending: dict[bytes, list[str]] = {}   # infohash -> announce URLs to try
    for infohash, announces in facts.values():
        pending.setdefault(infohash, announces)
    tracker_order: list[str] = []
    for announces in pending.values():
        for a in announces:
            if a not in tracker_order:
                tracker_order.append(a)

    found: dict[bytes, Stats] = {}
    for announce in tracker_order[:MAX_TRACKERS_TRIED]:
        if time.monotonic() > deadline:
            break
        todo = [h for h, urls in pending.items()
                if announce in urls
                and (h not in found or not any(found[h]))]
        if not todo:
            continue
        for h, stats_ in _scrape_tracker(announce, todo).items():
            if h not in found or not any(found[h]):
                # A tracker that doesn't know the hash answers all zeros;
                # prefer any later tracker that knows better.
                found[h] = stats_

    now = time.time()
    with _CACHE_LOCK:
        for url, idxs in by_url.items():
            fact = facts.get(url)
            stats_ = found.get(fact[0]) if fact else None
            if stats_ is None:
                # Negative-cache: a dead link or silent tracker stays unknown
                # until the TTL, instead of being re-fetched on every render.
                _MEM_CACHE[url] = (now, None)
                continue
            _MEM_CACHE[url] = (now, stats_)
            for i in idxs:
                resolved[i] = stats_
        _save_disk_cache_locked()

    return [_with_stats(it, resolved[i]) if i in resolved else it
            for i, it in enumerate(items)]


def top_by_downloads(items: list[Item], n: int) -> list[Item]:
    """Top-N by downloads, clamped to len(items). Stable for ties. Unknown
    downloads (feeds without stats) sort last, like -1, with seeders
    breaking the tie."""
    return sorted(items, key=lambda i: (
        i.downloads if i.downloads is not None else -1,
        i.seeders if i.seeders is not None else -1,
    ), reverse=True)[:n]


def _with_stats(item: Item, stats: Stats) -> Item:
    return item._replace(seeders=stats[0], leechers=stats[1], downloads=stats[2])
