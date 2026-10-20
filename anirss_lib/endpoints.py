"""Search endpoints: config validation, active-endpoint state, URL building,
fetch dispatch, and the auto-fallback probe."""

import os
import re
import urllib.parse
from typing import NamedTuple

from anirss_lib import nyaa
from anirss_lib.errors import ConfigError, NoEndpointsError, NotFoundError
from anirss_lib.types import Item


VALID_KINDS = ("nyaa", "rss")

NO_ENDPOINTS = ("no search endpoints configured: add one under [[endpoint]] in "
                "config.toml, or from an app that uses anirss")


class Endpoint(NamedTuple):
    name: str
    kind: str          # "nyaa" | "rss"
    url: str
    category: str = "1_0"   # nyaa kind only
    filter: str = "0"       # nyaa kind only
    # On-demand: shell commands that bring the endpoint's backing service up
    # and down, plus a URL that answers 2xx once it is ready (see ondemand.py).
    start: str = ""
    stop: str = ""
    ready: str = ""
    timeout: float = 10.0
    # Per-endpoint overrides of [downloads] / [qbittorrent].tags ("" / None
    # = use the global value).
    save_base: str = ""
    movie_path: str = ""
    tags: tuple[str, ...] | None = None

    @property
    def on_demand(self) -> bool:
        return bool(self.start)

    def owns_url(self, url: str) -> bool:
        """True when `url` points at this endpoint's feed (everything in the
        template before {query} matches)."""
        base = self.url.split("{query}", 1)[0]
        return bool(base) and url.startswith(base)


def load_endpoints(cfg) -> list[Endpoint]:
    """Validate cfg["endpoint"] into Endpoint objects. Raises ConfigError.
    An empty list is valid: anirss ships with no endpoints."""
    out: list[Endpoint] = []
    seen: set[str] = set()
    for i, raw in enumerate(cfg.get("endpoint") or [], start=1):
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not name:
            raise ConfigError(f"[[endpoint]] #{i}: missing `name`")
        if name in seen:
            raise ConfigError(f"[[endpoint]] #{i}: duplicate name {name!r}")
        seen.add(name)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ConfigError(f"[[endpoint]] {name!r}: invalid name "
                              f"(use only letters, digits, '.', '_', '-')")
        if kind not in VALID_KINDS:
            raise ConfigError(f"[[endpoint]] {name!r}: unknown kind {kind!r} "
                              f"(valid: {', '.join(VALID_KINDS)})")
        if not url:
            raise ConfigError(f"[[endpoint]] {name!r}: missing `url`")
        if kind == "rss" and "{query}" not in url:
            raise ConfigError(f"[[endpoint]] {name!r}: rss url must contain a {{query}} placeholder")
        start = str(raw.get("start") or "").strip()
        ready = str(raw.get("ready") or "").strip()
        if start and not ready:
            raise ConfigError(f"[[endpoint]] {name!r}: `start` needs a `ready` URL to wait on")
        tags = raw.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise ConfigError(f"[[endpoint]] {name!r}: `tags` must be a list")
        default_timeout = 60.0 if start else 10.0
        try:
            timeout = float(raw.get("timeout") or default_timeout)
        except (TypeError, ValueError):
            raise ConfigError(f"[[endpoint]] {name!r}: `timeout` must be a number of seconds")
        out.append(Endpoint(name=name, kind=kind, url=url,
                            category=str(raw.get("category") or "1_0"),
                            filter=str(raw.get("filter") or "0"),
                            start=start,
                            stop=str(raw.get("stop") or "").strip(),
                            ready=ready,
                            timeout=timeout,
                            save_base=os.path.expanduser(
                                str(raw.get("save_base") or "")),
                            movie_path=os.path.expanduser(
                                str(raw.get("movie_path") or "")),
                            tags=None if tags is None
                            else tuple(str(t) for t in tags)))
    return out


class EndpointState:
    """The configured endpoint list plus which one is currently active."""

    def __init__(self, endpoints: list[Endpoint],
                 active_name: str | None = None) -> None:
        if not endpoints:
            raise NoEndpointsError(NO_ENDPOINTS)
        self.endpoints = endpoints
        self.active = endpoints[0]
        if active_name is not None:
            found = self.by_name(active_name)
            if found is None:
                names = ", ".join(e.name for e in endpoints)
                raise NotFoundError(
                    f"unknown endpoint {active_name!r} (configured: {names})")
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


def _erai_split(ep: Endpoint, positive: str) -> tuple[str, list[str]]:
    """Erai's s= search matches post text only; the quality facets (1080p,
    WEB-DL, …) live in the erai: metadata and would zero the search out, so
    they're stripped from the terms here and returned for client-side
    filtering (erai.split_noise). Other endpoints pass through untouched."""
    if ep.kind == "rss":
        from anirss_lib import erai
        if erai.is_erai(ep):
            return erai.split_noise(positive)
    return positive, []


def feed_url(ep: Endpoint, query: str) -> tuple[str, list[str]]:
    """URL to persist as a qBittorrent RSS feed for `query`, plus the
    exclusion terms that could not be encoded into it. nyaa understands
    -tag exclusions server-side, so they stay in the URL; generic rss
    endpoints get only the positive terms and the exclusions are returned
    for the caller to enforce via the qB rule instead. erai additionally
    loses its quality tokens (they'd zero the s= search out — the caller
    puts them back via mustContain if it needs them)."""
    if ep.kind == "nyaa":
        return search_url(ep, query), []
    positive, excluded = split_exclusions(query)
    positive, _ = _erai_split(ep, positive)
    return search_url(ep, positive), excluded


def subscription_feed(ep: Endpoint, query: str, items: list[Item]
                      ) -> tuple[str, str, list[str]]:
    """(feed_url, mustContain, excluded) to persist as a qB subscription.

    Erai endpoints subscribe to the per-show anime-list feed when the slug
    can be resolved from `items` (see erai.resolve_show_feed); the positive
    query terms then move into the rule's mustContain since the per-show URL
    no longer encodes the search. Quoted phrases are unwrapped and poster
    fields dropped: qB matches whitespace-separated tokens against the raw
    feed titles, which carry no [group] poster. When the slug can't be
    resolved, the search feed is used with only the searchable terms on the
    wire and the quality tokens (which would zero the s= search out) moved
    into mustContain as well. Either way, a query that picks no AVC/HEVC
    variant gets the HEVC encode excluded (erai.variant_exclusions) so each
    episode downloads once. Every other endpoint keeps the feed_url()
    behavior with an empty mustContain."""
    from anirss_lib import erai
    if erai.is_erai(ep):
        per_show = erai.resolve_show_feed(ep, items)
        positive, excluded = split_exclusions(query)
        excluded += erai.variant_exclusions(query)
        if per_show:
            must = " ".join(
                field.strip('"') for field in FIELD_RE.findall(positive)
                if not field.startswith("["))
            return per_show, must, excluded
        positive, quality = _erai_split(ep, positive)
        return search_url(ep, positive), " ".join(quality), excluded
    url, excluded = feed_url(ep, query)
    return url, "", excluded


class SubscriptionSpec(NamedTuple):
    """Everything a qB subscription for (endpoint, query) needs."""
    feed_url: str
    must_contain: str
    must_not_contain: str   # qB's "|"-joined exclusion list
    endpoint_name: str


def build_subscription(ep: Endpoint, query: str, items: list[Item]
                       ) -> SubscriptionSpec:
    """subscription_feed() packaged for actions.subscribe_rule. May cost one
    probe request (erai per-show feed resolution)."""
    url, must, excluded = subscription_feed(ep, query, items)
    return SubscriptionSpec(url, must, "|".join(excluded), ep.name)


def filter_excluded(items: list[Item], terms: list[str]) -> list[Item]:
    """Drop items whose title contains any excluded term (case-insensitive)."""
    if not terms:
        return items
    lc = [t.lower() for t in terms]
    return [it for it in items
            if not any(t in it.title.lower() for t in lc)]


def fetch_items(ep: Endpoint, query: str, *, boot: bool = True) -> list[Item]:
    """Fetch `query` from `ep`. Raises nyaa.FetchError on network/parse errors.
    An on-demand endpoint is started first when `boot`; without it, one
    that isn't running already fails fast instead of starting."""
    from anirss_lib import ondemand
    if ep.on_demand:
        if boot:
            ondemand.ensure(ep)
        elif not ondemand.usable(ep):
            raise nyaa.FetchError(f"{ep.name}: not running")
    if ep.kind == "nyaa":
        return nyaa.fetch_rss(search_url(ep, query), ep.name, ep.timeout)
    from anirss_lib import erai
    positive, excluded = split_exclusions(query)
    positive, quality = _erai_split(ep, positive)
    items = nyaa.fetch_rss(search_url(ep, positive), ep.name, ep.timeout)
    items = filter_excluded(items, excluded)
    if quality:
        # The quality facets can't be searched server-side (they'd zero the
        # s= search out); they narrow the results here instead.
        items = [it for it in items if erai.matches_all(it, quality)]
    return items


def probe_fallback(state: EndpointState, query: str, fetch=None
                   ) -> tuple[list[Item], list[str]]:
    """After the active endpoint came up empty or unreachable, try the other
    endpoints in priority order. First hit wins: state.active switches to it
    and its items are returned. Returns (items, per-endpoint notes); empty
    items means nothing anywhere. On-demand endpoints that aren't running
    are skipped rather than started. `fetch` is injectable for tests."""
    from anirss_lib import ondemand
    fetch = fetch or fetch_items
    notes: list[str] = []
    for ep in state.endpoints:
        if ep is state.active:
            continue
        if not ondemand.usable(ep):
            notes.append(f"{ep.name}: off")
            continue
        try:
            items = fetch(ep, query)
        except nyaa.FetchError:
            notes.append(f"{ep.name}: unreachable")
            continue
        if items:
            state.active = ep
            notes.append(f"{ep.name}: {len(items)}")
            # Tag where these came from so the results list can render a
            # source-header row above them (the search didn't ask for this
            # endpoint, it fell back to it).
            return [it._replace(source=ep.name) for it in items], notes
        notes.append(f"{ep.name}: 0")
    return [], notes


def owner_of(endpoints: list[Endpoint], url: str) -> Endpoint | None:
    """The configured endpoint whose feed `url` is, if any."""
    return next((e for e in endpoints if e.owns_url(url)), None)
