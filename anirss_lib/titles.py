"""Best-effort title parsing for nyaa torrent names."""

import re


POSTER_RE   = re.compile(r"^\[([^\]]+)\]")
TOKEN_RE    = re.compile(r"[\s\[\]\(\)]+")
NUMERIC_RE  = re.compile(r"^\d+$")
HEX_RE      = re.compile(r"^[0-9A-Fa-f]{6,}$")
EXT_RE      = re.compile(r"\.\w{2,4}$")
RES_RE      = re.compile(r"\b(\d{3,4})p\b", re.IGNORECASE)
# " - 04" or " - 04v2" followed by space, end, or bracket; not " - 1080p".
EPISODE_RE  = re.compile(r"\s+-\s+\d{1,4}(?:v\d+)?(?=\s|$|\[)")
META_RE     = re.compile(r"\s*\[")


def poster_of(title: str) -> str | None:
    match = POSTER_RE.match(title)
    return f"[{match.group(1)}]" if match else None


def show_name(title: str) -> str:
    """Best-effort show name from a torrent title: strip poster, episode, metadata, extension."""
    name = EXT_RE.sub("", title)
    name = POSTER_RE.sub("", name, count=1).strip()
    cuts: list[int] = []
    episode = EPISODE_RE.search(name)
    if episode:
        cuts.append(episode.start())
    meta = META_RE.search(name)
    if meta and meta.start() > 0:
        cuts.append(meta.start())
    if cuts:
        name = name[:min(cuts)]
    return name.strip(" -")


def title_tokens(title: str) -> list[str]:
    base = EXT_RE.sub("", title)
    base = POSTER_RE.sub("", base, count=1)
    tokens: list[str] = []
    for token in TOKEN_RE.split(base):
        if not token or token == "-":
            continue
        if NUMERIC_RE.match(token):
            continue
        if HEX_RE.match(token) and not re.search(r"[g-zG-Z]", token):
            continue
        if len(token) < 2:
            continue
        tokens.append(token)
    return tokens
