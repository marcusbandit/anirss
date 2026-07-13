"""Rank releases for the "[★ Try Best Fit]" button.

Given the current results, find the single best release from a trusted group at
the best available quality, then expose the (group + resolution + source) tokens
that define its profile so the refine loop can refetch every matching release.

Everything here is pure: parsing helpers plus a `score()` that turns a title +
config into a comparable tuple. The tuple's field *order* is the policy:

    (is_trusted, source_rank, resolution, has_subs, audio_rank,
     group_rank, seeders, downloads)

Trusted groups win first; among them the source type decides (so WEB-DL always
beats WebRip), then resolution, then subtitle presence, then the audio variant
(Dual-Audio beats Multi-Audio: JP+EN is all we play, the extra dubs just cost
disk), then which trusted group it is, then raw popularity. Group/source
priority come from config lists, so the ranking is data-driven rather than a
wall of per-source branches.
"""

import re

from anirss_lib.config import BestfitConfig
from anirss_lib.titles import RES_RE, poster_of, season_of, show_name
from anirss_lib.types import Item


# Punctuation that's noise in a search query (and that nyaa tokenises away
# anyway). Stripped from the extracted show name so the rebuilt query reads
# clean: "Heroine? Seijo? Iie, ..." -> "Heroine Seijo Iie ...". Colons, dots,
# parentheses, and the like are kept since they can carry meaning (Re:Zero).
_TITLE_NOISE_RE = re.compile(r"[?!,]+")


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
_SUBS_RE = re.compile(r"Multi[\s._-]?Subs?|Multiple Subtitle", re.IGNORECASE)

# Audio-variant detection, same shape as _SOURCE_PATTERNS. List order is both
# detection precedence and preference: Dual-Audio first. The bare "Multi" form
# must not swallow "Multi Subs", hence the lookahead.
_AUDIO_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Dual",  re.compile(r"Dual[\s._-]?Audio|\bDual\b", re.IGNORECASE)),
    ("Multi", re.compile(r"Multi[\s._-]?Audio|\bMulti\b(?![\s._-]?Sub)",
                         re.IGNORECASE)),
]


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


def audio_of(title: str) -> tuple[str, str] | None:
    """(canonical_name, literal_match) of the first audio-variant tag found,
    else None. Same contract as source_of: the literal is what the title
    actually says (e.g. "DUAL" vs "Dual-Audio") so it can be pinned back into
    a query and still match."""
    for name, pattern in _AUDIO_PATTERNS:
        m = pattern.search(title)
        if m:
            return name, m.group(0)
    return None


def _audio_rank(title: str) -> int:
    """Rank of the title's audio variant in _AUDIO_PATTERNS order (first =
    highest), 0 when the title carries no audio tag."""
    audio = audio_of(title)
    if not audio:
        return 0
    names = [name for name, _ in _AUDIO_PATTERNS]
    return len(names) - names.index(audio[0])


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
        _audio_rank(title),                                  # Dual > Multi
        grank,                                               # which trusted group
        item.seeders,
        item.downloads,
    )


def best_item(items: list[Item], cfg: BestfitConfig) -> Item | None:
    """The single highest-ranked release, or None for an empty list."""
    return max(items, key=lambda it: score(it, cfg), default=None)


def clean_show_name(title: str) -> str:
    """The real show name from a release title, with search-noise punctuation
    removed and whitespace collapsed (e.g. 'Heroine? Seijo? Iie, ... desu
    (Hokori)!' -> 'Heroine Seijo Iie ... desu (Hokori)')."""
    name = _TITLE_NOISE_RE.sub(" ", show_name(title))
    return " ".join(name.split())


def best_fit_query(item: Item) -> str:
    """Rebuild a clean nyaa query from the best-matched release: the group, the
    *real* show name pulled from its title, then season, resolution and source.

    This replaces whatever (often hand-truncated) terms the user searched with
    the canonical title the result actually has, so e.g. a search narrowed down
    to '... Maid des' becomes '... Maid desu (Hokori)'. A SxxEyy marker keeps
    only its season half (pinning the episode would drop every other episode
    from the refetch). The audio variant IS pinned: groups that post a DUAL
    and a MULTi copy of every episode would otherwise come back as duplicate
    pairs. Subtitles are left out so the refetch isn't over-narrowed."""
    parts: list[str] = []
    poster = poster_of(item.title)
    if poster:
        parts.append(poster)
    name = clean_show_name(item.title)
    if name:
        parts.append(name)
    season = season_of(item.title)
    if season:
        parts.append(season)
    res = resolution_of(item.title)
    if res:
        parts.append(f"{res}p")
    src = source_of(item.title)
    if src:
        parts.append(src[1])
    audio = audio_of(item.title)
    if audio:
        parts.append(audio[1])
    return " ".join(parts)
