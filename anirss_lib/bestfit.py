"""Rank releases for the "[★ Try Best Fit]" button.

Given the current results, find the single best release from a trusted group at
the best available quality, then expose the (group + resolution + source) tokens
that define its profile so the refine loop can refetch every matching release.

Everything here is pure: parsing helpers plus a `score()` that turns a title +
config into a comparable tuple. The tuple's field *order* is the policy:

    (is_trusted, source_rank, resolution, has_subs, group_rank, seeders, downloads)

Trusted groups win first; among them the source type decides (so WEB-DL always
beats WebRip), then resolution, then subtitle presence, then which trusted group
it is, then raw popularity. Group/source priority come from config lists, so the
ranking is data-driven rather than a wall of per-source branches.
"""

import re

from anirss_lib.config import BestfitConfig
from anirss_lib.titles import RES_RE, poster_of
from anirss_lib.types import Item


# Source-type detection. List order is *detection precedence*: WEB-DL / WEBRip
# are tried before bare WEB so "WEB-DL" never reads as a plain "WEB". Which
# source is preferred is a separate question, decided by config source_order.
_SOURCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("WEB-DL", re.compile(r"WEB[\s._-]?DL", re.IGNORECASE)),
    ("WEBRip", re.compile(r"WEB[\s._-]?Rip", re.IGNORECASE)),
    ("BluRay", re.compile(r"Blu[\s._-]?Ray|BD[\s._-]?Rip|\bBD\b", re.IGNORECASE)),
    ("WEB",    re.compile(r"\bWEB\b", re.IGNORECASE)),
    ("HDTV",   re.compile(r"\bHDTV\b", re.IGNORECASE)),
]
_SUBS_RE = re.compile(r"Multi[\s._-]?Subs?|Multiple Subtitle|Dual[\s._-]?Audio",
                      re.IGNORECASE)


def source_of(title: str) -> tuple[str, str] | None:
    """(canonical_name, literal_match) of the first source tag found, else None.

    The literal match is what actually appears in the title (e.g. "WEB-DL" vs
    "WEBDL"), so it can be pinned back into a nyaa query and still match.
    """
    for name, pattern in _SOURCE_PATTERNS:
        m = pattern.search(title)
        if m:
            return name, m.group(0)
    return None


def resolution_of(title: str) -> int:
    """Highest <n>p resolution in the title (e.g. 1080), or 0 if none."""
    found = [int(m.group(1)) for m in RES_RE.finditer(title)]
    return max(found) if found else 0


def has_subs(title: str) -> bool:
    return bool(_SUBS_RE.search(title))


def _group_rank(title: str, preferred: list[str]) -> int:
    """Rank of the title's release group in `preferred` (first listed = highest),
    or 0 when the group is absent / not in the trusted list."""
    poster = poster_of(title)
    if not poster:
        return 0
    core = poster.strip("[]").lower()
    lowered = [g.lower() for g in preferred]
    return (len(lowered) - lowered.index(core)) if core in lowered else 0


def _source_rank(title: str, source_order: list[str]) -> int:
    """Rank of the title's source type in `source_order` (first = highest), 0 if none."""
    src = source_of(title)
    if not src:
        return 0
    lowered = [s.lower() for s in source_order]
    name = src[0].lower()
    return (len(lowered) - lowered.index(name)) if name in lowered else 0


def _resolution_score(res: int, preferred_resolution: str) -> int:
    """Score a resolution. "highest" rewards bigger numbers; a target like "1080"
    rewards closeness to that target (so 4K loses to the sweet spot)."""
    if preferred_resolution != "highest":
        try:
            target = int(str(preferred_resolution).rstrip("pP"))
            return -abs(res - target)
        except ValueError:
            pass
    return res


def score(item: Item, cfg: BestfitConfig) -> tuple:
    """Comparable ranking tuple; higher sorts as better. See module docstring."""
    title = item.title
    grank = _group_rank(title, cfg["preferred_groups"])
    return (
        1 if grank > 0 else 0,                               # trusted-group gate
        _source_rank(title, cfg["source_order"]),            # WEB-DL > WebRip
        _resolution_score(resolution_of(title), cfg["preferred_resolution"]),
        1 if has_subs(title) else 0,
        grank,                                               # which trusted group
        item.seeders,
        item.downloads,
    )


def best_item(items: list[Item], cfg: BestfitConfig) -> Item | None:
    """The single highest-ranked release, or None for an empty list."""
    return max(items, key=lambda it: score(it, cfg), default=None)


def profile_tokens(item: Item) -> list[str]:
    """The group + resolution + source tags that define `item`'s quality profile,
    in title order. These get pinned into the query for the best-fit refetch.
    Subtitles are deliberately excluded so the refetch isn't over-narrowed."""
    tokens: list[str] = []
    poster = poster_of(item.title)
    if poster:
        tokens.append(poster)
    res = resolution_of(item.title)
    if res:
        tokens.append(f"{res}p")
    src = source_of(item.title)
    if src:
        tokens.append(src[1])
    return tokens
