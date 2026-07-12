"""Search endpoints: config validation, active-endpoint state, URL building,
fetch dispatch, and the auto-fallback probe."""

import re
import urllib.parse
from typing import NamedTuple

from anirss_lib import nyaa
from anirss_lib.logging import die
from anirss_lib.types import Item


VALID_KINDS = ("nyaa", "rss")


class Endpoint(NamedTuple):
    name: str
    kind: str          # "nyaa" | "rss"
    url: str
    category: str = "1_0"   # nyaa kind only
    filter: str = "0"       # nyaa kind only


def load_endpoints(cfg) -> list[Endpoint]:
    """Validate cfg["endpoint"] into Endpoint objects. Dies on config errors."""
    out: list[Endpoint] = []
    seen: set[str] = set()
    for i, raw in enumerate(cfg.get("endpoint") or [], start=1):
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not name:
            die(f"[[endpoint]] #{i}: missing `name`")
        if name in seen:
            die(f"[[endpoint]] #{i}: duplicate name {name!r}")
        seen.add(name)
        if kind not in VALID_KINDS:
            die(f"[[endpoint]] {name!r}: unknown kind {kind!r} "
                f"(valid: {', '.join(VALID_KINDS)})")
        if not url:
            die(f"[[endpoint]] {name!r}: missing `url`")
        if kind == "rss" and "{query}" not in url:
            die(f"[[endpoint]] {name!r}: rss url must contain a {{query}} placeholder")
        out.append(Endpoint(name=name, kind=kind, url=url,
                            category=str(raw.get("category") or "1_0"),
                            filter=str(raw.get("filter") or "0")))
    if not out:
        die("no [[endpoint]] configured")  # load_config synthesizes, so: unreachable
    return out


class EndpointState:
    """The configured endpoint list plus which one is currently active."""

    def __init__(self, endpoints: list[Endpoint],
                 active_name: str | None = None) -> None:
        self.endpoints = endpoints
        self.active = endpoints[0]
        if active_name is not None:
            found = self.by_name(active_name)
            if found is None:
                names = ", ".join(e.name for e in endpoints)
                die(f"unknown endpoint {active_name!r} (configured: {names})")
            else:
                self.active = found

    def by_name(self, name: str) -> Endpoint | None:
        return next((e for e in self.endpoints if e.name == name), None)

    def cycle(self) -> Endpoint:
        """Advance to the next endpoint in priority order and return it."""
        i = self.endpoints.index(self.active)
        self.active = self.endpoints[(i + 1) % len(self.endpoints)]
        return self.active


def search_url(ep: Endpoint, query: str) -> str:
    if ep.kind == "nyaa":
        qs = urllib.parse.urlencode({
            "page": "rss", "q": query, "c": ep.category, "f": ep.filter,
        })
        return f"{ep.url}?{qs}"
    return ep.url.replace("{query}", urllib.parse.quote_plus(query))


# Splits a query into fields, keeping `-"quoted phrase"` / `"quoted"` intact.
# Shared with refine.py's positional-insert logic.
FIELD_RE = re.compile(r'-?"[^"]*"|\S+')


def split_exclusions(query: str) -> tuple[str, list[str]]:
    """Split `query` into (positive-terms query, exclusion terms). An
    exclusion is a `-tag` or `-"quoted phrase"` field (nyaa syntax). Generic
    RSS endpoints don't understand `-tag`, so exclusions are applied
    client-side after the fetch instead."""
    positive: list[str] = []
    excluded: list[str] = []
    for field in FIELD_RE.findall(query):
        if field.startswith("-") and len(field) > 1:
            core = field[1:]
            if len(core) >= 2 and core[0] == '"' and core[-1] == '"':
                core = core[1:-1]
            if core:
                excluded.append(core)
                continue
        positive.append(field)
    return " ".join(positive), excluded


def filter_excluded(items: list[Item], terms: list[str]) -> list[Item]:
    """Drop items whose title contains any excluded term (case-insensitive)."""
    if not terms:
        return items
    lc = [t.lower() for t in terms]
    return [it for it in items
            if not any(t in it.title.lower() for t in lc)]


def fetch_items(ep: Endpoint, query: str) -> list[Item]:
    """Fetch `query` from `ep`. Raises nyaa.FetchError on network/parse errors."""
    if ep.kind == "nyaa":
        return nyaa.fetch_rss(search_url(ep, query), ep.name)
    positive, excluded = split_exclusions(query)
    items = nyaa.fetch_rss(search_url(ep, positive), ep.name)
    return filter_excluded(items, excluded)


def probe_fallback(state: EndpointState, query: str, fetch=None
                   ) -> tuple[list[Item], list[str]]:
    """After the active endpoint came up empty or unreachable, try the other
    endpoints in priority order. First hit wins: state.active switches to it
    and its items are returned. Returns (items, per-endpoint notes); empty
    items means nothing anywhere. `fetch` is injectable for tests."""
    fetch = fetch or fetch_items
    notes: list[str] = []
    for ep in state.endpoints:
        if ep is state.active:
            continue
        try:
            items = fetch(ep, query)
        except nyaa.FetchError:
            notes.append(f"{ep.name}: unreachable")
            continue
        if items:
            state.active = ep
            notes.append(f"{ep.name}: {len(items)}")
            return items, notes
        notes.append(f"{ep.name}: 0")
    return [], notes
