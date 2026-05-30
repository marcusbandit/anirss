"""qBittorrent RSS feed cache mirror."""

import datetime

from anirss_lib import config as _cfg
from anirss_lib.logging import log
from anirss_lib.qbt.session import QbtSession


def write_feed_cache(names: list[str]) -> None:
    """Atomically replace ~/.local/state/anirss/feeds.txt with the given names."""
    try:
        _cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cfg.FEEDS_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text("".join(name + "\n" for name in sorted(names, key=str.lower)))
        tmp.replace(_cfg.FEEDS_CACHE_PATH)
        log("INFO", f"wrote feed cache ({len(names)} names) to {_cfg.FEEDS_CACHE_PATH}")
    except OSError as e:
        log("WARN", f"couldn't write feed cache to {_cfg.FEEDS_CACHE_PATH}: {e}")


def read_feed_cache() -> list[str]:
    try:
        return [line for line in _cfg.FEEDS_CACHE_PATH.read_text().splitlines() if line]
    except OSError:
        return []


def feed_cache_age_seconds() -> float | None:
    try:
        return max(0.0,
                   datetime.datetime.now().timestamp()
                   - _cfg.FEEDS_CACHE_PATH.stat().st_mtime)
    except OSError:
        return None


def is_feed_cache_stale(threshold_seconds: float = _cfg.FEED_CACHE_TTL_SECONDS) -> bool:
    age = feed_cache_age_seconds()
    return age is None or age > threshold_seconds


def list_qbt_rule_names(qbt: QbtSession) -> list[str]:
    """Return all qBittorrent RSS rule names. These are the same strings anirss subscribes under."""
    rules = qbt.get_json("/api/v2/rss/rules")
    if not isinstance(rules, dict):
        return []
    return list(rules.keys())


def refresh_feed_cache(qbt: QbtSession) -> list[str]:
    names = list_qbt_rule_names(qbt)
    write_feed_cache(names)
    return names


def maybe_refresh_feed_cache(qbt: QbtSession) -> None:
    """Refresh the feed cache if it's missing or older than 24h. Best-effort, logs only on failure."""
    if not is_feed_cache_stale():
        return
    try:
        refresh_feed_cache(qbt)
    except Exception as e:  # noqa: BLE001 — silent best-effort on the way out
        log("WARN", f"feed-cache refresh failed: {e}")
