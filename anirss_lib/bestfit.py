"""Rank releases for the "[★ Try Best Fit]" button.

Given the current results, find the single best release from a trusted group at
the best available quality, then expose the (group + resolution + source) tokens
that define its profile so the refine loop can refetch every matching release.

Everything here is pure: parsing helpers plus a `score()` that turns a title +
config into a comparable tuple. The tuple's field *order* is the policy:

    (is_trusted, size_rank, source_rank, resolution, has_subs, audio_rank,
     group_rank, seeders, downloads)

Trusted groups win first. Then comes size adequacy: a release starved of
bitrate is the one defect no other virtue makes up for, so it outranks even the
source type (a properly encoded WEB-DL beats a BDRip squeezed to half the bits
it needed). Then the source type (BluRay over WEB-DL over WebRip), resolution,
subtitle presence, the audio variant (Dual-Audio beats Multi-Audio: JP+EN is
all we play, the extra dubs just cost disk), which trusted group it is, and
finally raw popularity. Group/source priority and the size target come from
config, so the ranking is data-driven rather than a wall of per-case branches.
"""

import re

from anirss_lib.config import BestfitConfig
from anirss_lib.titles import (
    EPISODE_RE, RES_RE, SEASON_EP_RE, poster_of, season_of, show_name,
)
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

# --- size adequacy -------------------------------------------------------
#
# "12 episodes in 6 GiB" is the complaint this section answers. Raw bytes can't
# be compared across releases directly: HEVC needs ~a third fewer bits than
# x264 for the same result, and 720p needs a fraction of what 1080p does. So a
# release's per-episode size is normalised to a "1080p x264-equivalent" figure
# and compared against one config target.

_SIZE_RE = re.compile(r"(\d[\d.,]*)\s*([KMGT])?i?B\b", re.IGNORECASE)
_UNIT_MIB = {
    "": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1.0,
    "g": 1024.0, "t": 1024.0 * 1024,
}

# Codec efficiency relative to AVC/x264 (1.0): how many times smaller a file in
# this codec can be while still looking the same. List order is detection
# precedence. An untagged title falls back to 1.0, i.e. we assume the least
# efficient codec, which reads its size as generously as possible.
_CODEC_EFFICIENCY: list[tuple[str, float, re.Pattern]] = [
    ("AV1",  1.8, re.compile(r"\bAV1\b", re.IGNORECASE)),
    ("HEVC", 1.5, re.compile(r"\bHEVC\b|\b[xh][\s._-]?265\b", re.IGNORECASE)),
    ("AVC",  1.0, re.compile(r"\bAVC\b|\b[xh][\s._-]?264\b", re.IGNORECASE)),
]
_DEFAULT_CODEC_EFFICIENCY = 1.0

# Bitrate scales with pixel count to roughly the 0.75 power, and pixel count
# scales with the square of the vertical resolution, so the per-episode target
# scales as (res / 1080) ** 1.5. That puts 720p at 0.54x the 1080p target and
# 2160p at 2.83x, instead of pretending every resolution needs the same bits.
_RES_TARGET_EXPONENT = 1.5
_TARGET_BASE_RES = 1080

# Adequacy bands for the normalised per-episode size, as (min_ratio, rank),
# checked top-down. Unknown size sits at 0 so a release whose size or episode
# count can't be parsed neither gains nor loses ground against one that can.
_SIZE_BANDS: list[tuple[float, int]] = [
    (0.85, 1),   # adequate: enough bits for its resolution and codec
    (0.60, -1),  # thin: under-provisioned, banding on gradients and dark scenes
    (0.00, -2),  # starved
]
_UNKNOWN_SIZE_RANK = 0

# Episode spans: "01-12", "(1-12)", "07~12", "S01E01-E12". The lookbehind
# rejects date- and version-like neighbours ("2021-07-12", "x264-2"), which
# would otherwise read as an episode span.
_EP_RANGE_RE = re.compile(
    r"(?<![\d\-./~])[Ee]?(\d{1,3})\s*[-~]\s*[Ee]?(\d{1,3})(?![\d.])")

# Marks a release as covering a whole season rather than one episode or a movie.
# Only these may borrow the result set's consensus episode count; without it a
# countless title (typically a film) would be divided by 12 and look starved.
_BATCH_RE = re.compile(
    r"\bBatch\b|\bSeason\b|\bS\d{1,2}\b|\bComplete\b|\bFin\b|合集|全集",
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


def _source_rank(title: str, source_order: list[str], assumed: str = "") -> int:
    """Rank of the title's source type in `source_order` (first = highest), 0
    when the type is absent and no `assumed` fallback places it.

    Plenty of groups (SubsPlease, Erai-raws) never write a source tag at all.
    Scoring those as 0 buried them below every tagged release no matter how
    trusted they were, so `assumed` lets config name what an untagged release
    most likely is (in practice a simulcast "WEB") instead of assuming the worst.
    """
    src = source_of(title)
    name = (src[0] if src else assumed).lower()
    if not name:
        return 0
    lowered = [s.lower() for s in source_order]
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


def _to_float(raw: str) -> float | None:
    """Parse a size number that may use '.' or ',' as its decimal mark, and ','
    as a thousands separator ('1,024' -> 1024.0, '6,2' -> 6.2)."""
    text = raw.strip()
    if "." in text and "," in text:
        text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        joiner = "" if len(tail) == 3 else "."
        text = f"{head.replace(',', '')}{joiner}{tail}"
    try:
        return float(text)
    except ValueError:
        return None


def size_mib(size: str) -> float | None:
    """A feed's size string ('6.2 GiB', '301.4 MiB') in MiB, or None if unreadable."""
    match = _SIZE_RE.search(size or "")
    if not match:
        return None
    value = _to_float(match.group(1))
    if value is None:
        return None
    return value * _UNIT_MIB[(match.group(2) or "").lower()]


def codec_efficiency(title: str) -> float:
    """How many times smaller this title's codec can be than x264 for equal
    quality. 1.0 when no codec is tagged (see _CODEC_EFFICIENCY)."""
    for _, factor, pattern in _CODEC_EFFICIENCY:
        if pattern.search(title):
            return factor
    return _DEFAULT_CODEC_EFFICIENCY


def episode_span(title: str) -> int | None:
    """How many episodes the title says it contains: the widest declared range
    ('01-12' -> 12, '07~12' -> 6), 1 for a single-episode marker, else None.

    None means "the title doesn't say", which is the common case for season
    batches ('S01', '(Batch)'); `consensus_episode_count` fills those in.
    """
    spans = [
        high - low + 1
        for low, high in (
            (int(m.group(1)), int(m.group(2))) for m in _EP_RANGE_RE.finditer(title)
        )
        if 0 <= low < high
    ]
    if spans:
        return max(spans)
    if SEASON_EP_RE.search(title) or EPISODE_RE.search(title):
        return 1
    return None


def consensus_episode_count(items: list[Item]) -> int | None:
    """The episode count the result set agrees on, or None if none declares one.

    Episode count is a property of the *show*, not of any one release, so the
    batches that spell out "01-12" tell us what the ones that only say "S01"
    contain. Most-declared wins; a tie goes to the wider span, since a full
    season is likelier to be the shared truth than a half-batch.
    """
    spans = [s for s in (episode_span(i.title) for i in items) if s and s > 1]
    if not spans:
        return None
    return max(sorted(set(spans)), key=lambda s: (spans.count(s), s))


def per_episode_mib(item: Item, fallback_episodes: int | None = None) -> float | None:
    """The release's MiB per episode, or None when size or episode count is unknown.

    The title's own declared span wins; `fallback_episodes` (the result set's
    consensus) is used only for titles that look like season batches.
    """
    total = size_mib(item.size)
    if total is None:
        return None
    episodes = episode_span(item.title)
    if episodes is None and _BATCH_RE.search(item.title):
        episodes = fallback_episodes
    if not episodes:
        return None
    return total / episodes


def _size_target_mib(res: int, cfg: BestfitConfig) -> float:
    """The per-episode MiB a release at this resolution should carry, scaled off
    the config's 1080p baseline (see _RES_TARGET_EXPONENT)."""
    base = float(cfg["target_mib_per_episode"])
    if res <= 0:
        return base
    return base * (res / _TARGET_BASE_RES) ** _RES_TARGET_EXPONENT


def size_ratio(item: Item, cfg: BestfitConfig,
               fallback_episodes: int | None = None) -> float | None:
    """How the release's per-episode size compares to the target for its
    resolution, normalised for codec efficiency. 1.0 is on target, 0.5 is half
    the bits it wanted. None when it can't be determined.
    """
    per_ep = per_episode_mib(item, fallback_episodes)
    if per_ep is None:
        return None
    target = _size_target_mib(resolution_of(item.title), cfg)
    if target <= 0:
        return None
    return (per_ep * codec_efficiency(item.title)) / target


def _size_rank(item: Item, cfg: BestfitConfig,
               fallback_episodes: int | None = None) -> int:
    """Adequacy band of the release's bitrate budget (see _SIZE_BANDS).

    Past `max_size_ratio` the extra bits stop buying visible quality, so remuxes
    and other giants drop back to the unknown-size rank rather than winning on
    bulk alone: this picks the best *fit*, not the biggest file.
    """
    ratio = size_ratio(item, cfg, fallback_episodes)
    if ratio is None or ratio > cfg["max_size_ratio"]:
        return _UNKNOWN_SIZE_RANK
    for minimum, rank in _SIZE_BANDS:
        if ratio >= minimum:
            return rank
    return _SIZE_BANDS[-1][1]


def score(item: Item, cfg: BestfitConfig,
          fallback_episodes: int | None = None) -> tuple:
    """Comparable ranking tuple; higher sorts as better. See module docstring.

    `fallback_episodes` is the result set's consensus episode count, which
    `best_item` derives; without it, season batches that don't spell out their
    episode range simply score as unknown-size.
    """
    title = item.title
    grank = _group_rank(title, cfg["preferred_groups"])
    return (
        1 if grank > 0 else 0,                               # trusted-group gate
        _size_rank(item, cfg, fallback_episodes),            # enough bits?
        _source_rank(title, cfg["source_order"],
                     cfg.get("assumed_source", "")),         # BluRay > WEB-DL
        _resolution_score(resolution_of(title), cfg["preferred_resolution"]),
        1 if has_subs(title) else 0,
        _audio_rank(title),                                  # Dual > Multi
        grank,                                               # which trusted group
        item.seeders,
        item.downloads,
    )


def best_item(items: list[Item], cfg: BestfitConfig) -> Item | None:
    """The single highest-ranked release, or None for an empty list."""
    episodes = consensus_episode_count(items)
    return max(items, key=lambda it: score(it, cfg, episodes), default=None)


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
