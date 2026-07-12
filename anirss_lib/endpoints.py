"""Search endpoints: config validation, active-endpoint state, URL building,
fetch dispatch, and the auto-fallback probe."""

import urllib.parse
from typing import NamedTuple

from anirss_lib.logging import die


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
