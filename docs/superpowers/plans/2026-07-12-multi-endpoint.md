# Multi-Endpoint Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let anirss search any number of configured endpoints (nyaa-style or generic RSS), switchable on the fly with Ctrl-E / `-e <name>`, with automatic fallback to the next endpoint when the initial search comes up empty.

**Architecture:** A new `anirss_lib/endpoints.py` owns the `Endpoint` model, config validation, per-kind URL building, fetch dispatch (with client-side exclusions for generic RSS), and the fallback probe. `nyaa.py` becomes the shared RSS fetch/parse engine (nyaa stat extensions optional, enclosure and description fallbacks for generic feeds). A mutable `EndpointState` (list + active) is threaded through `main.py` and `refine.py` where `cfg["search"]` travels today.

**Tech Stack:** Python 3.11+ stdlib only (`urllib`, `xml.etree`, `tomllib`), external `fzf` binary, pytest for tests.

**Spec:** `docs/superpowers/specs/2026-07-12-multi-endpoint-design.md`

## Global Constraints

- Pure stdlib: no third-party runtime dependencies may be added.
- Python 3.11+ (`tomllib`, `X | None` unions, NamedTuple defaults).
- The config key is `endpoint` (array of tables `[[endpoint]]`), never `source` (that word means video source in `[bestfit].source_order`).
- Existing configs with only `[search]` must keep working with zero edits.
- NEW user-facing strings must not contain em dashes (U+2014) or en dashes (U+2013); use commas, colons, or hyphens. Existing strings quoted for context keep their original characters.
- Never add `Co-Authored-By` lines to commits.
- Test command: `python -m pytest -q` from the repo root. The full suite must pass at every commit.
- Work happens on the existing `multi-endpoint` branch.

---

### Task 1: Config schema: `[[endpoint]]` + legacy `[search]` synthesis

**Files:**
- Modify: `anirss_lib/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `cfg["endpoint"]` is ALWAYS a non-empty `list[dict]` after `load_config()`, each dict having keys `name`, `kind`, `url` and optionally `category`, `filter`. `EndpointConfig` TypedDict exported from `anirss_lib.config`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
def test_default_config_has_endpoint_list():
    eps = config.DEFAULT_CONFIG["endpoint"]
    assert isinstance(eps, list) and len(eps) == 1
    assert eps[0]["name"] == "nyaa"
    assert eps[0]["kind"] == "nyaa"
    assert eps[0]["url"] == "https://nyaa.si/"


def test_load_config_synthesizes_endpoint_from_legacy_search(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[search]\nnyaa_url = "https://mirror.example/"\n'
        'category = "1_2"\nfilter = "2"\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    cfg = config.load_config()
    assert cfg["endpoint"] == [{
        "name": "nyaa", "kind": "nyaa", "url": "https://mirror.example/",
        "category": "1_2", "filter": "2",
    }]


def test_load_config_user_endpoints_win(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[endpoint]]\nname = "anirena"\nkind = "rss"\n'
        'url = "https://www.anirena.com/rss?q={query}&adult=1"\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    cfg = config.load_config()
    assert [e["name"] for e in cfg["endpoint"]] == ["anirena"]


def test_load_config_missing_file_still_has_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "nope" / "config.toml")
    cfg = config.load_config()
    assert cfg["endpoint"][0]["name"] == "nyaa"
    assert cfg["endpoint"][0]["url"] == "https://nyaa.si/"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL with `KeyError: 'endpoint'`.

- [ ] **Step 3: Implement**

In `anirss_lib/config.py`:

(a) Add after `SearchConfig` (line 36-39):

```python
class EndpointConfig(TypedDict, total=False):
    name: str
    kind: str        # "nyaa" | "rss"
    url: str
    category: str    # nyaa kind only
    filter: str      # nyaa kind only
```

(b) Add `endpoint: list[EndpointConfig]` to `AnirssConfig`.

(c) In `DEFAULT_CONFIG_TOML`, immediately after the `[search]` block (after the `filter = "0"` line), insert:

```toml
# Search endpoints in priority order; the first is the default at startup.
# Ctrl-E switches on the fly; `anirss -e <name>` starts on a specific one.
# A search with zero hits automatically probes the rest in this order.
# ([search] above is the legacy fallback, used only when no [[endpoint]]
# is defined.)
[[endpoint]]
name = "nyaa"
kind = "nyaa"   # nyaa-style software: q/c/f params + seeders/size stats
url = "https://nyaa.si/"
category = "1_0"
filter = "0"

# `kind = "rss"` fits any site with an RSS search URL. Put {query} where the
# search terms go; extra fixed params are fine. Stats columns show only when
# the feed carries them. Uncomment to enable AniRena as a fallback:
#[[endpoint]]
#name = "anirena"
#kind = "rss"
#url = "https://www.anirena.com/rss?q={query}&adult=1"
```

Note: `_split_toml_sections`'s regex does not match `[[endpoint]]` lines, so this text rides inside the `[search]` block for `migrate_config()` purposes. That is intended; legacy users are covered by runtime synthesis, not file rewriting.

(d) Rework `load_config()` so the parsed user TOML is inspectable and synthesis happens when the user has no `[[endpoint]]`:

```python
def load_config() -> AnirssConfig:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    user_cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("rb") as f:
                user_cfg = tomllib.load(f)
            _deep_merge(cfg, user_cfg)
        except (OSError, tomllib.TOMLDecodeError) as e:
            print(f"{C_YEL}warning: bad config at {CONFIG_PATH}: {e}{C_OFF}", file=sys.stderr)
    else:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
            print(f"{C_DIM}wrote default config: {CONFIG_PATH} (edit it!){C_OFF}", file=sys.stderr)
        except OSError as e:
            print(f"{C_YEL}warning: couldn't create {CONFIG_PATH}: {e}{C_OFF}", file=sys.stderr)
    if "endpoint" not in user_cfg:
        # Legacy config (or fresh defaults): honor [search] by synthesizing
        # the endpoint list from it, so an edited nyaa_url keeps working.
        s = cfg["search"]
        cfg["endpoint"] = [{
            "name": "nyaa", "kind": "nyaa", "url": s["nyaa_url"],
            "category": s["category"], "filter": s["filter"],
        }]
    cfg["downloads"]["save_base"] = os.path.expanduser(cfg["downloads"]["save_base"])
    cfg["downloads"]["movie_path"] = os.path.expanduser(cfg["downloads"]["movie_path"])
    cfg["logging"]["log_path"] = os.path.expanduser(cfg["logging"]["log_path"])
    return cfg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/config.py tests/test_config.py
git commit -m "Add [[endpoint]] config schema with legacy [search] synthesis"
```

---

### Task 2: `endpoints.py` core: Endpoint, load_endpoints, EndpointState, search_url

**Files:**
- Create: `anirss_lib/endpoints.py`
- Test: `tests/test_endpoints.py` (new)

**Interfaces:**
- Consumes: `cfg["endpoint"]` list from Task 1; `die` from `anirss_lib.logging`.
- Produces:
  - `Endpoint(NamedTuple)`: fields `name: str, kind: str, url: str, category: str = "1_0", filter: str = "0"`.
  - `load_endpoints(cfg) -> list[Endpoint]` (dies on invalid config).
  - `EndpointState(endpoints: list[Endpoint], active_name: str | None = None)` with attributes `.endpoints`, `.active`, methods `.by_name(name) -> Endpoint | None`, `.cycle() -> Endpoint`.
  - `search_url(ep: Endpoint, query: str) -> str`.
  - `VALID_KINDS = ("nyaa", "rss")`.

- [ ] **Step 1: Write the failing tests** (create `tests/test_endpoints.py`)

```python
import pytest

from anirss_lib import endpoints
from anirss_lib.endpoints import Endpoint, EndpointState


NYAA = Endpoint(name="nyaa", kind="nyaa", url="https://nyaa.si/",
                category="1_2", filter="1")
ANIRENA = Endpoint(name="anirena", kind="rss",
                   url="https://www.anirena.com/rss?q={query}&adult=1")


def test_load_endpoints_valid():
    cfg = {"endpoint": [
        {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.si/",
         "category": "1_2", "filter": "1"},
        {"name": "anirena", "kind": "rss",
         "url": "https://www.anirena.com/rss?q={query}&adult=1"},
    ]}
    eps = endpoints.load_endpoints(cfg)
    assert eps == [NYAA, ANIRENA]


@pytest.mark.parametrize("bad", [
    {"kind": "nyaa", "url": "https://x/"},                       # no name
    {"name": "a", "kind": "html", "url": "https://x/"},          # bad kind
    {"name": "a", "kind": "rss", "url": "https://x/rss"},        # no {query}
    {"name": "a", "kind": "nyaa"},                               # no url
])
def test_load_endpoints_invalid_dies(bad):
    with pytest.raises(SystemExit):
        endpoints.load_endpoints({"endpoint": [bad]})


def test_load_endpoints_duplicate_name_dies():
    ep = {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.si/"}
    with pytest.raises(SystemExit):
        endpoints.load_endpoints({"endpoint": [ep, dict(ep)]})


def test_search_url_nyaa_kind():
    url = endpoints.search_url(NYAA, "one piece")
    assert url.startswith("https://nyaa.si/?")
    assert "page=rss" in url and "q=one+piece" in url
    assert "c=1_2" in url and "f=1" in url


def test_search_url_rss_kind_fills_template():
    url = endpoints.search_url(ANIRENA, "shin chan")
    assert url == "https://www.anirena.com/rss?q=shin+chan&adult=1"


def test_state_default_active_is_first():
    st = EndpointState([NYAA, ANIRENA])
    assert st.active is NYAA


def test_state_active_by_name():
    st = EndpointState([NYAA, ANIRENA], "anirena")
    assert st.active is ANIRENA


def test_state_unknown_name_dies():
    with pytest.raises(SystemExit):
        EndpointState([NYAA, ANIRENA], "tosho")


def test_state_cycle_wraps():
    st = EndpointState([NYAA, ANIRENA])
    assert st.cycle() is ANIRENA
    assert st.cycle() is NYAA
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_endpoints.py -q`
Expected: FAIL with `ModuleNotFoundError: anirss_lib.endpoints`.

- [ ] **Step 3: Implement** (create `anirss_lib/endpoints.py`)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/endpoints.py tests/test_endpoints.py
git commit -m "Add endpoints module: Endpoint model, validation, state, URL building"
```

---

### Task 3: Generalize the RSS parser (nyaa.py)

**Files:**
- Modify: `anirss_lib/nyaa.py`
- Test: `tests/test_rss_parsing.py` (new)

**Interfaces:**
- Consumes: `Item` from `anirss_lib.types`.
- Produces (all in `anirss_lib.nyaa`):
  - `class FetchError(Exception)`.
  - `parse_rss(data: bytes | str) -> list[Item]` (pure parser, no network).
  - `fetch_rss(url: str, endpoint_name: str = "feed") -> list[Item]` (network; raises `FetchError` instead of dying).
  - Back-compat alias `_fetch_items_from_url = fetch_rss` kept until Task 8 migrates `main.py`.
  - Existing `search_url(query, search)` / `fetch_items(query, search)` stay for now (removed in Task 8).

Parser behavior additions (all backward-compatible for nyaa feeds):
1. Link preference: if an `<item>` has an `<enclosure>` whose `type` contains `bittorrent` or whose `url` ends with `.torrent`, use the enclosure URL as `Item.link`; otherwise use `<link>`. (AniRena's `<link>` is a torrent web page; the `.torrent` file is the enclosure. nyaa has no enclosure, so nothing changes there.)
2. Size/category fallback: when the nyaa namespace elements are absent, best-effort parse `Size: <n> <unit>` and `Category: <text>` out of `<description>` (AniRena format: `Size: 485.1 MB | Uploader: X | Category: Anime > ...`).
3. Category-prefix strip: AniRena prefixes titles with `[<category>] `. If the resolved category is non-empty and the title starts with `[<category>] `, strip that prefix, so release-group detection (`poster_of`) and Best Fit don't mistake the category for a release group.
4. Errors: network and XML parse failures raise `FetchError(f"can't reach {endpoint_name}: {e}")` / `FetchError(f"bad feed from {endpoint_name}: {e}")` instead of `die("can't reach nyaa: ...")`.

- [ ] **Step 1: Write the failing tests** (create `tests/test_rss_parsing.py`)

```python
from anirss_lib import nyaa


NYAA_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
<channel>
  <item>
    <title>[Erai-raws] Show - 05 [1080p][Multiple Subtitle]</title>
    <link>https://nyaa.si/download/1837471.torrent</link>
    <nyaa:seeders>923</nyaa:seeders>
    <nyaa:leechers>12</nyaa:leechers>
    <nyaa:downloads>4051</nyaa:downloads>
    <nyaa:size>1.4 GiB</nyaa:size>
    <nyaa:category>Anime - English-translated</nyaa:category>
  </item>
</channel>
</rss>"""

ANIRENA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>AniRena</title>
  <item>
    <title>[Anime &gt; Subs] Sayonara Lara - 02 [1080p CR WEBRip][768DB037]</title>
    <link>https://www.anirena.com/torrents/019f57b3</link>
    <description><![CDATA[Size: 485.1 MB | Uploader: Erai-raws | Category: Anime > Subs]]></description>
    <enclosure url="https://www.anirena.com/torrents/019f57b3.torrent" type="application/x-bittorrent" length="0"/>
  </item>
  <item>
    <title>Bare Title Without Prefix - 03</title>
    <link>https://www.anirena.com/torrents/019f57a5</link>
  </item>
</channel>
</rss>"""


def test_parse_nyaa_feed_unchanged():
    items = nyaa.parse_rss(NYAA_XML)
    assert len(items) == 1
    it = items[0]
    assert it.title == "[Erai-raws] Show - 05 [1080p][Multiple Subtitle]"
    assert it.link == "https://nyaa.si/download/1837471.torrent"
    assert (it.seeders, it.leechers, it.downloads) == (923, 12, 4051)
    assert it.size == "1.4 GiB"
    assert it.category == "Anime - English-translated"


def test_parse_generic_feed_prefers_torrent_enclosure():
    it = nyaa.parse_rss(ANIRENA_XML)[0]
    assert it.link == "https://www.anirena.com/torrents/019f57b3.torrent"


def test_parse_generic_feed_description_fallbacks():
    it = nyaa.parse_rss(ANIRENA_XML)[0]
    assert it.size == "485.1 MB"
    assert it.category == "Anime > Subs"
    assert (it.seeders, it.leechers, it.downloads) == (0, 0, 0)


def test_parse_generic_feed_strips_category_title_prefix():
    it = nyaa.parse_rss(ANIRENA_XML)[0]
    assert it.title == "Sayonara Lara - 02 [1080p CR WEBRip][768DB037]"


def test_parse_generic_feed_item_without_extras():
    it = nyaa.parse_rss(ANIRENA_XML)[1]
    assert it.title == "Bare Title Without Prefix - 03"
    assert it.link == "https://www.anirena.com/torrents/019f57a5"
    assert it.size == "" and it.category == ""


def test_parse_rss_bad_xml_raises_fetch_error():
    import pytest
    with pytest.raises(nyaa.FetchError):
        nyaa.parse_rss("<not-xml", endpoint_name="anirena")
```

Note: `parse_rss` takes an optional `endpoint_name: str = "feed"` used only in error messages.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rss_parsing.py -q`
Expected: FAIL with `AttributeError: ... no attribute 'parse_rss'`.

- [ ] **Step 3: Implement**

Rewrite the fetch/parse half of `anirss_lib/nyaa.py` (keep `search_url`, `fetch_items`, `_int_text`, `_str_text` as-is for now; module docstring becomes `"""RSS fetching and parsing: nyaa stat extensions plus generic-feed fallbacks."""`). Add `import re` and:

```python
class FetchError(Exception):
    """A search endpoint couldn't be fetched or parsed."""


_DESC_SIZE_RE = re.compile(r"Size:\s*([\d.,]+\s*[KMGT]?i?B)", re.IGNORECASE)
_DESC_CAT_RE = re.compile(r"Category:\s*([^|]+)")


def _item_link(entry: ET.Element, link_text: str) -> str:
    """Prefer a torrent-file enclosure over <link>: on generic trackers the
    <link> is a web page, while the enclosure is the actual .torrent."""
    enclosure = entry.find("enclosure")
    if enclosure is not None:
        enc_url = (enclosure.get("url") or "").strip()
        enc_type = (enclosure.get("type") or "").lower()
        if enc_url and ("bittorrent" in enc_type
                        or enc_url.lower().endswith(".torrent")):
            return enc_url
    return link_text


def parse_rss(data: bytes | str, endpoint_name: str = "feed") -> list[Item]:
    """Parse an RSS feed into Items. Reads nyaa's stat extensions when
    present; otherwise falls back to Size:/Category: hints in <description>.
    Raises FetchError on malformed XML."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FetchError(f"bad feed from {endpoint_name}: {e}") from e
    out: list[Item] = []
    for entry in root.iter("item"):
        title = entry.find("title")
        link = entry.find("link")
        if title is None or not title.text or link is None or not link.text:
            continue
        title_text = title.text.strip()
        size = _str_text(entry.find("nyaa:size", NYAA_NS))
        category = _str_text(entry.find("nyaa:category", NYAA_NS))
        if not size or not category:
            desc = _str_text(entry.find("description"))
            if desc:
                if not size and (m := _DESC_SIZE_RE.search(desc)):
                    size = m.group(1).strip()
                if not category and (m := _DESC_CAT_RE.search(desc)):
                    category = m.group(1).strip()
        # Generic trackers prefix titles with "[<category>] ": strip it so
        # release-group detection doesn't mistake the category for a poster.
        if category and title_text.startswith(f"[{category}] "):
            title_text = title_text[len(category) + 3:]
        out.append(Item(
            title=title_text,
            link=_item_link(entry, link.text.strip()),
            seeders=_int_text(entry.find("nyaa:seeders", NYAA_NS)),
            leechers=_int_text(entry.find("nyaa:leechers", NYAA_NS)),
            downloads=_int_text(entry.find("nyaa:downloads", NYAA_NS)),
            size=size,
            category=category,
        ))
    return out


def fetch_rss(url: str, endpoint_name: str = "feed") -> list[Item]:
    """GET `url` and parse it as an RSS feed. Raises FetchError on failure."""
    log("INFO", f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "anirss/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        raise FetchError(f"can't reach {endpoint_name}: {e}") from e
    items = parse_rss(data, endpoint_name)
    log("INFO", f"  -> {len(items)} items")
    return items


# Back-compat alias for main.py's -S flow; removed once main migrates (Task 8).
_fetch_items_from_url = fetch_rss
```

The old `_fetch_items_from_url` body is deleted (replaced by `fetch_rss`). `fetch_items(query, search)` now calls `fetch_rss(search_url(query, search), "nyaa")`. IMPORTANT interim behavior: `fetch_items` callers currently expect `die` on network failure; wrap in this task to preserve behavior until Task 8/9 rewires them:

```python
def fetch_items(query: str, search: SearchConfig) -> list[Item]:
    """Return list of Items from the nyaa RSS for `query`."""
    try:
        return fetch_rss(search_url(query, search), "nyaa")
    except FetchError as e:
        die(str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass (including existing suite).

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/nyaa.py tests/test_rss_parsing.py
git commit -m "Generalize RSS parser: FetchError, enclosure links, description fallbacks"
```

---

### Task 4: Fetch dispatch, client-side exclusions, fallback probe

**Files:**
- Modify: `anirss_lib/endpoints.py`
- Test: `tests/test_endpoints.py`

**Interfaces:**
- Consumes: `nyaa.fetch_rss`, `nyaa.FetchError` from Task 3; `Endpoint`, `EndpointState`, `search_url` from Task 2.
- Produces (in `anirss_lib.endpoints`):
  - `FIELD_RE` (compiled regex, the query-field splitter shared with refine).
  - `split_exclusions(query: str) -> tuple[str, list[str]]`.
  - `filter_excluded(items: list[Item], terms: list[str]) -> list[Item]`.
  - `fetch_items(ep: Endpoint, query: str) -> list[Item]` (raises `nyaa.FetchError`).
  - `probe_fallback(state: EndpointState, query: str, fetch=None) -> tuple[list[Item], list[str]]`; on success mutates `state.active` and returns the winning items plus per-endpoint notes; on total failure returns `([], notes)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_endpoints.py`)

```python
from anirss_lib.nyaa import FetchError
from anirss_lib.types import Item


def test_split_exclusions():
    q, excl = endpoints.split_exclusions('show 1080p -HEVC -"dual audio"')
    assert q == "show 1080p"
    assert excl == ["HEVC", "dual audio"]


def test_split_exclusions_no_exclusions_roundtrip():
    q, excl = endpoints.split_exclusions("[Erai-raws] show 1080p")
    assert q == "[Erai-raws] show 1080p"
    assert excl == []


def test_filter_excluded_case_insensitive():
    items = [Item("Show 05 HEVC x265", "l1"), Item("Show 05 AVC", "l2")]
    kept = endpoints.filter_excluded(items, ["hevc"])
    assert [i.link for i in kept] == ["l2"]


def test_fetch_items_rss_kind_applies_exclusions(monkeypatch):
    fetched_urls = []

    def fake_fetch_rss(url, endpoint_name="feed"):
        fetched_urls.append(url)
        return [Item("Show 05 HEVC", "l1"), Item("Show 05 AVC", "l2")]

    monkeypatch.setattr(endpoints.nyaa, "fetch_rss", fake_fetch_rss)
    items = endpoints.fetch_items(ANIRENA, "show -HEVC")
    assert [i.link for i in items] == ["l2"]
    # The exclusion never reaches the wire; only positive terms are sent.
    assert "HEVC" not in fetched_urls[0]


def test_fetch_items_nyaa_kind_sends_exclusions(monkeypatch):
    import urllib.parse

    def fake_fetch_rss(url, endpoint_name="feed"):
        assert "-HEVC" in urllib.parse.unquote_plus(url)
        return [Item("t", "l")]

    monkeypatch.setattr(endpoints.nyaa, "fetch_rss", fake_fetch_rss)
    assert endpoints.fetch_items(NYAA, "show -HEVC")


def test_probe_fallback_switches_to_first_hit():
    st = EndpointState([NYAA, ANIRENA])

    def fake_fetch(ep, query):
        return [Item("t", "l")] if ep.name == "anirena" else []

    items, notes = endpoints.probe_fallback(st, "q", fetch=fake_fetch)
    assert items and st.active is ANIRENA
    assert notes == ["anirena: 1"]


def test_probe_fallback_all_fail_keeps_active():
    third = Endpoint(name="tosho", kind="rss", url="https://x/?q={query}")
    st = EndpointState([NYAA, ANIRENA, third])

    def fake_fetch(ep, query):
        if ep.name == "anirena":
            raise FetchError("can't reach anirena: boom")
        return []

    items, notes = endpoints.probe_fallback(st, "q", fetch=fake_fetch)
    assert items == [] and st.active is NYAA
    assert notes == ["anirena: unreachable", "tosho: 0"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_endpoints.py -q`
Expected: FAIL with `AttributeError` on `split_exclusions`.

- [ ] **Step 3: Implement** (append to `anirss_lib/endpoints.py`; add `import re` and `from anirss_lib import nyaa` and `from anirss_lib.types import Item` at the top)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/endpoints.py tests/test_endpoints.py
git commit -m "Add endpoint fetch dispatch, client-side exclusions, fallback probe"
```

---

### Task 5: CLI flag `-e` / `--endpoint`

**Files:**
- Modify: `anirss_lib/cli/args.py`
- Test: `tests/test_cli_args.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ParsedArgs.endpoint: str | None` (default None). `parse_cli_args` recognizes `-e NAME`, `--endpoint NAME`, `--endpoint=NAME` and keeps them out of `positional`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_args.py`, matching its existing import style)

```python
def test_endpoint_flag_short():
    out = parse_cli_args(["-e", "anirena", "some", "query"])
    assert out.endpoint == "anirena"
    assert out.positional == ["some", "query"]


def test_endpoint_flag_long_and_equals():
    assert parse_cli_args(["--endpoint", "nyaa"]).endpoint == "nyaa"
    assert parse_cli_args(["--endpoint=nyaa"]).endpoint == "nyaa"


def test_endpoint_flag_missing_value_dies():
    with pytest.raises(SystemExit):
        parse_cli_args(["-e"])
```

(If the file doesn't already import pytest / parse_cli_args, add the imports in that file's existing style.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli_args.py -q`
Expected: FAIL (`AttributeError: 'ParsedArgs' object has no attribute 'endpoint'` or positional mismatch).

- [ ] **Step 3: Implement**

In `ParsedArgs` add (next to `name`):

```python
    endpoint: str | None = None
```

In `parse_cli_args`'s elif chain, before the `a.startswith("--")` catch-all:

```python
        elif a in ("-e", "--endpoint"):
            i += 1
            if i >= len(argv):
                die("--endpoint requires a name")
            out.endpoint = argv[i]
        elif a.startswith("--endpoint="):
            out.endpoint = a.split("=", 1)[1]
```

Also update the `parse_cli_args` docstring to mention `-e/--endpoint`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/cli/args.py tests/test_cli_args.py
git commit -m "Parse -e/--endpoint CLI flag"
```

---

### Task 6: URL classification knows configured endpoints

**Files:**
- Modify: `anirss_lib/cli/urls.py`
- Test: `tests/test_cli_urls.py`

**Interfaces:**
- Consumes: objects with `.kind` and `.url` attributes (works with `Endpoint` without importing it).
- Produces:
  - `UrlKind.ENDPOINT_RSS` (new member).
  - `classify_url(s, *, nyaa_hosts: frozenset[str] = frozenset(), rss_hosts: frozenset[str] = frozenset()) -> UrlKind`. Existing positional call sites keep working unchanged.
  - `endpoint_hosts(endpoints) -> tuple[frozenset[str], frozenset[str]]` returning `(nyaa_hosts, rss_hosts)`, hostnames lowercased.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_urls.py`, matching its import style)

```python
class _Ep:
    def __init__(self, kind, url):
        self.kind, self.url = kind, url


def test_endpoint_hosts_partition_by_kind():
    nyaa_hosts, rss_hosts = endpoint_hosts([
        _Ep("nyaa", "https://mirror.example/"),
        _Ep("rss", "https://www.anirena.com/rss?q={query}&adult=1"),
    ])
    assert nyaa_hosts == frozenset({"mirror.example"})
    assert rss_hosts == frozenset({"www.anirena.com"})


def test_classify_url_nyaa_kind_host_is_nyaa_rss():
    kind = classify_url("https://mirror.example/?page=rss&q=x",
                        nyaa_hosts=frozenset({"mirror.example"}))
    assert kind == UrlKind.NYAA_RSS


def test_classify_url_rss_kind_host_is_endpoint_rss():
    kind = classify_url("https://www.anirena.com/rss?q=x&adult=1",
                        rss_hosts=frozenset({"www.anirena.com"}))
    assert kind == UrlKind.ENDPOINT_RSS


def test_classify_url_unknown_host_still_other_http():
    assert classify_url("https://elsewhere.example/feed") == UrlKind.OTHER_HTTP
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli_urls.py -q`
Expected: FAIL (no `endpoint_hosts`, no `ENDPOINT_RSS`).

- [ ] **Step 3: Implement**

In `UrlKind` add `ENDPOINT_RSS = enum.auto()`. Change `classify_url` signature and the host check:

```python
def classify_url(s: str, *, nyaa_hosts: frozenset[str] = frozenset(),
                 rss_hosts: frozenset[str] = frozenset()) -> UrlKind:
```

and replace the `host == "nyaa.si"` block body with:

```python
        if host == "nyaa.si" or host.endswith(".nyaa.si") or host in nyaa_hosts:
            return UrlKind.NYAA_RSS
        if host in rss_hosts:
            return UrlKind.ENDPOINT_RSS
        return UrlKind.OTHER_HTTP
```

Add at the bottom:

```python
def endpoint_hosts(endpoints) -> tuple[frozenset[str], frozenset[str]]:
    """(nyaa_hosts, rss_hosts) for the configured endpoints, lowercased.
    Accepts anything with .kind and .url so it doesn't import Endpoint."""
    nyaa_hosts: set[str] = set()
    rss_hosts: set[str] = set()
    for ep in endpoints:
        host = (urllib.parse.urlparse(ep.url).hostname or "").lower()
        if not host:
            continue
        (nyaa_hosts if ep.kind == "nyaa" else rss_hosts).add(host)
    return frozenset(nyaa_hosts), frozenset(rss_hosts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/cli/urls.py tests/test_cli_urls.py
git commit -m "Classify URLs against configured endpoint hosts"
```

---

### Task 7: fzf plumbing: expect keys and prompt labels

**Files:**
- Modify: `anirss_lib/fzf.py`
- Modify: `anirss_lib/refine.py` (call-site unpack only)
- Modify: `anirss_lib/main.py` (call-site unpack only)

**Interfaces:**
- Consumes: existing `_parse_fzf_output`.
- Produces:
  - `fzf_pick_with_query(options, header, *, height=None, prompt_label=PROMPT_FILTER, extra_expect="") -> tuple[str, str | None, bool, str]`; the new 4th element is the expect key that ended the session (`""` for plain Enter, e.g. `"ctrl-e"`).
  - `fzf_search_prompt(prompt_label, *, default="") -> tuple[str | None, str]` returning `(query_or_None, key)` with key in `{"enter", "esc", "ctrl-e"}`. Empty-query Enter returns `(None, "esc")` (preserves today's cancel behavior). Ctrl-E returns the currently typed text (possibly empty string) so the caller can keep it.

No new unit tests (thin subprocess wrappers); the compile-level guarantee is the full suite plus call-site updates in this task.

- [ ] **Step 1: Change `fzf_pick_with_query`**

Signature: add `prompt_label: str = PROMPT_FILTER, extra_expect: str = ""`. Replace `"--prompt", PROMPT_FILTER` with `"--prompt", prompt_label`. Replace `"--expect", "ctrl-c"` with:

```python
        "--expect", "ctrl-c" + (f",{extra_expect}" if extra_expect else ""),
```

Return statements become (respectively): `return query, (choice or None), False, out.expect_key`, `return query, None, False, out.expect_key`, and `return "", None, True, ""`. Update the docstring.

- [ ] **Step 2: Change `fzf_search_prompt`**

- `--expect` list becomes `"ctrl-c,enter,ctrl-e"`.
- Fallback (no fzf) becomes:

```python
    if not shutil.which("fzf"):
        q = readline_prompt(prompt_label, history="search") or None
        return q, ("enter" if q else "esc")
```

- Tail of the function becomes:

```python
    proc = subprocess.run(fzf_args, input="", text=True, stdout=subprocess.PIPE)
    out = _parse_fzf_output(proc.stdout or "", print_query=True, expect=True)
    q = out.query.strip()
    if out.expect_key == "ctrl-e":
        return q, "ctrl-e"
    if out.expect_key == "enter" and q:
        try:
            with history_file.open("a") as f:
                f.write(q + "\n")
        except OSError:
            pass
        return q, "enter"
    return None, "esc"
```

- [ ] **Step 3: Update the two call sites so everything still compiles and behaves identically**

`refine.py` `pick_group` (line 171): `query, choice, cancelled = fzf_pick_with_query(...)` becomes `query, choice, cancelled, _key = fzf_pick_with_query(options, header, height=height)`.

`main.py` `_run_search_state_machine` (line 194): replace

```python
            result = fzf_search_prompt(PROMPT_SEARCH, default=last_search_query)
```

with

```python
            result, key = fzf_search_prompt(PROMPT_SEARCH, default=last_search_query)
            if key == "ctrl-e":
                # Endpoint switching lands with the wiring task; ignore for now.
                if result:
                    last_search_query = result
                continue
```

- [ ] **Step 4: Run the suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/fzf.py anirss_lib/refine.py anirss_lib/main.py
git commit -m "fzf wrappers: configurable prompt and expect keys, keyed search-prompt result"
```

---

### Task 8: Thread EndpointState through refine and main; Ctrl-E switching in refine

**Files:**
- Modify: `anirss_lib/types.py` (PICK_ENDPOINT)
- Modify: `anirss_lib/cli/pickers.py` (pick_endpoint)
- Modify: `anirss_lib/refine.py`
- Modify: `anirss_lib/main.py`
- Modify: `anirss_lib/nyaa.py` (delete legacy `search_url`/`fetch_items`/alias)
- Test: `tests/test_config.py` etc. keep passing; new picker test optional (skip: fzf wrapper).

**Interfaces:**
- Consumes: `EndpointState`, `endpoints.fetch_items`, `endpoints.search_url` (Tasks 2/4); `fzf_pick_with_query` 4-tuple (Task 7).
- Produces:
  - `types.PICK_ENDPOINT = Pick("endpoint", [])` (and `"endpoint"` documented in `Pick.kind`).
  - `cli.pickers.pick_endpoint(state: EndpointState) -> Endpoint | None`: cycles when 2 endpoints, fzf pick when 3+, None when unchanged/cancelled/only 1. Mutates `state.active` on success.
  - `refine(initial_query, items, state: EndpointState, bestfit_cfg)` (was `search: SearchConfig`); same return tuple.
  - `pick_group(groups, selected, *, height=None, state: EndpointState | None = None)`: Ctrl-E returns `PICK_ENDPOINT`; prompt shows `state.active.name`; header advertises Ctrl-E.
  - `main._run_search_state_machine(initial_query, cfg, eps)`, `main._run_interactive(initial_query, force_url, parsed, cfg, eps)`, `main._run_noninteractive(parsed, cfg, eps, initial_query, force_url)`. IMPORTANT: the `EndpointState` parameter is named `eps` everywhere in `main.py` because `_run_search_state_machine` already has a local string variable named `state`.
  - `main._print_search_rss_rows(query: str, ep: Endpoint)`.
  - `nyaa.search_url`, `nyaa.fetch_items`, `_fetch_items_from_url` alias, and nyaa's `SearchConfig`/`die` imports are REMOVED (grep to confirm no remaining users).

- [ ] **Step 1: types.py**

Append `PICK_ENDPOINT = Pick("endpoint", [])` and add `endpoint` to the kind comment on `Pick`.

- [ ] **Step 2: cli/pickers.py: add pick_endpoint**

```python
def pick_endpoint(state) -> "Endpoint | None":
    """Switch the active endpoint. With two configured, just cycle; with
    more, open a small fzf pick. Returns the new active endpoint (state is
    mutated), or None when cancelled/unchanged/nothing to switch to."""
    if len(state.endpoints) < 2:
        return None
    if len(state.endpoints) == 2:
        return state.cycle()
    options = [
        f"{e.name} (active)" if e is state.active else e.name
        for e in state.endpoints
    ]
    choice = fzf_pick_one(options, "switch endpoint", prompt_label="endpoint > ")
    if choice is None:
        return None
    ep = state.by_name(choice.removesuffix(" (active)"))
    if ep is None or ep is state.active:
        return None
    state.active = ep
    return ep
```

Import `Endpoint` under `TYPE_CHECKING` or just drop the annotation to a docstring if pickers.py has no typing imports; follow the file's existing style. `fzf_pick_one` is already imported there (or add it to the existing `from anirss_lib.fzf import ...` line).

- [ ] **Step 3: refine.py rewiring**

- Imports: drop `from anirss_lib.nyaa import fetch_items` and `SearchConfig`; add `from anirss_lib import endpoints as endpoints_mod`, `from anirss_lib.endpoints import EndpointState`, `from anirss_lib.nyaa import FetchError`, `from anirss_lib.cli.pickers import pick_endpoint`, and `PICK_ENDPOINT` to the types import.
- `refine(initial_query, items, state: EndpointState, bestfit_cfg)` and `_refine_loop(query, selected, state, bestfit_cfg)`: replace every `fetch_items(X, search)` with `endpoints_mod.fetch_items(state.active, X)` and wrap each in:

```python
            try:
                new_items = endpoints_mod.fetch_items(state.active, new_query)
            except FetchError as e:
                die(str(e))
```

(add `die` to the logging import; this preserves the current die-on-error behavior with the endpoint named).
- Replace the four `"refetching nyaa with ..."` message strings with `f"{C_DIM}refetching {state.active.name} with {new_query!r}...{C_OFF}"` and `"nyaa returned"` result-count strings with `f"{state.active.name} returned"`.
- `pick_group` gains `state: EndpointState | None = None`:
  - prompt: `prompt_label = (f"{C_YEL}{state.active.name} >{C_OFF} {C_BLU}Filter >{C_OFF} " if state else PROMPT_FILTER)` (import `C_BLU`/`PROMPT_FILTER` as needed).
  - header: when `state` and `len(state.endpoints) > 1`, append `f" · {C_BLD}Ctrl-E{C_OFF} endpoint"` to the existing header string.
  - call: `query, choice, cancelled, key = fzf_pick_with_query(options, header, height=height, prompt_label=prompt_label, extra_expect="ctrl-e")` and immediately after the `cancelled` check:

```python
    if key == "ctrl-e":
        return PICK_ENDPOINT
```

  - `_refine_loop` passes `state=state` to `pick_group`.
- New branch in `_refine_loop`, right after the `show_all` branch:

```python
        if pick.kind == "endpoint":
            prev = state.active
            new_ep = pick_endpoint(state)
            if new_ep is None:
                continue
            print(f"{C_DIM}refetching {new_ep.name} with {query!r}...{C_OFF}")
            try:
                new_items = endpoints_mod.fetch_items(new_ep, query)
            except FetchError as e:
                print(f"{C_YEL}{e}, staying on {prev.name}{C_OFF}")
                state.active = prev
                continue
            if not new_items:
                print(f"{C_YEL}{new_ep.name}: 0 results for this query, "
                      f"staying on {prev.name}{C_OFF}")
                state.active = prev
                continue
            print(f"{C_YEL}switched to {new_ep.name}: {len(new_items)} result(s){C_OFF}")
            selected = new_items
            log("INFO", f"endpoint switch -> {new_ep.name}: {len(new_items)} results")
            continue
```

- [ ] **Step 4: main.py rewiring**

- Imports: replace `from anirss_lib.nyaa import _fetch_items_from_url, fetch_items, search_url` with `from anirss_lib import endpoints as endpoints_mod`, `from anirss_lib.endpoints import EndpointState, load_endpoints`, `from anirss_lib.nyaa import FetchError, fetch_rss`.
- `main()`: after `cfg = load_config()` add `endpoint_list = load_endpoints(cfg)`. The `--_search-rss` branch calls `_print_search_rss_rows(query, endpoint_list[0])`. After `parse_cli_args` add `eps = EndpointState(endpoint_list)` (the `-e` name activates in Task 9) and pass `eps` into `_run_noninteractive(parsed, cfg, eps, initial_query, force_url)` / `_run_interactive(initial_query, force_url, parsed, cfg, eps)`. The `EndpointState` parameter is named `eps` throughout `main.py` (never `state`, which `_run_search_state_machine` already uses for its state-machine string).
- `_print_search_rss_rows(query: str, ep)` body: first line becomes

```python
    try:
        items = endpoints_mod.fetch_items(ep, query)
    except FetchError as e:
        print(f"{C_DIM}{e}{C_OFF}")
        return
```

- `_run_search_state_machine(initial_query, cfg, eps)`: fetch state becomes

```python
            print(f"{C_DIM}fetching {eps.active.name}...{C_OFF}")
            try:
                items = endpoints_mod.fetch_items(eps.active, initial_query)
            except FetchError as e:
                die(str(e))
```

(temporary die; the fallback probe replaces it in Task 9), and the refine call becomes `refine(query, selected, eps, cfg["bestfit"])`.
- `_run_noninteractive`: `fetch_items(initial_query, cfg["search"])` becomes the try/except-die form with `endpoints_mod.fetch_items(eps.active, initial_query)`; `search_url(initial_query, cfg["search"])` becomes `endpoints_mod.search_url(eps.active, initial_query)`; `_fetch_items_from_url(force_url)` becomes `fetch_rss(force_url)` wrapped in try/except FetchError -> `die(str(e))`.
- `_run_interactive`: `_fetch_items_from_url(force_url)` likewise becomes `fetch_rss(force_url)` (same try/except); `search_url(query, cfg["search"])` becomes `endpoints_mod.search_url(eps.active, query)`; `_run_search_state_machine(initial_query, cfg)` gains `eps`.
- DRY cleanup while in refine.py: delete refine's module-level `_FIELD_RE` and use `endpoints_mod.FIELD_RE` in `_insert_positional` instead (same regex, now owned by endpoints.py).
- nyaa.py: delete `search_url`, `fetch_items`, the `_fetch_items_from_url` alias, and the now-unused `SearchConfig` and `die` imports. Verify nothing else references them:

Run: `rg -n "nyaa import|nyaa\.(search_url|fetch_items|_fetch_items_from_url)" anirss_lib tests test_anirss.py`
Expected: only `FetchError` / `fetch_rss` / `parse_rss` / `NYAA_NS` style imports remain; fix any stragglers found.

- [ ] **Step 5: Run the suite**

Run: `python -m pytest -q`
Expected: all pass. Also sanity-compile: `python -c "from anirss_lib import main, refine, endpoints"`.

- [ ] **Step 6: Commit**

```bash
git add anirss_lib/types.py anirss_lib/cli/pickers.py anirss_lib/refine.py anirss_lib/main.py anirss_lib/nyaa.py
git commit -m "Thread EndpointState through main and refine; Ctrl-E endpoint switch in refine"
```

---

### Task 9: main.py: -e activation, auto-fallback, live-search endpoint, bare-URL flow

**Files:**
- Modify: `anirss_lib/main.py`
- Modify: `anirss_lib/fzf.py` (search header + reload cmd endpoint flag)
- Test: `tests/test_endpoints.py` already covers probe_fallback; manual verification step below.

**Interfaces:**
- Consumes: `parsed.endpoint` (Task 5), `probe_fallback` (Task 4), `endpoint_hosts`/`ENDPOINT_RSS` (Task 6), keyed `fzf_search_prompt` (Task 7), `pick_endpoint` (Task 8).
- Produces: the fully wired behavior described in the spec.

- [ ] **Step 1: `-e` activation**

In `main()`: `state = EndpointState(endpoint_list, parsed.endpoint)` (dies with the configured names on an unknown name, already implemented).

- [ ] **Step 2: live search carries the endpoint**

`fzf_search_prompt` gains a keyword arg `endpoint_name: str = ""`. In it, when `endpoint_name` is non-empty, extend both reload commands:

```python
    ep_flag = f"--_endpoint {shlex.quote(endpoint_name)} " if endpoint_name else ""
    search_cmd = f"sleep 0.5 && {quoted} --_search-rss {ep_flag}{{q}}"
    initial_cmd = f"{quoted} --_search-rss {ep_flag}{{q}}"
```

and the header's first words become `f"type to search {endpoint_name or 'nyaa'}"`; when the caller passes `switch_hint=True` (new keyword, default False) append `f" · {C_BLD}Ctrl-E{C_OFF} endpoint"` to the header.

`main()`'s `--_search-rss` branch parses the flag:

```python
    if argv and argv[0] == "--_search-rss":
        rest = argv[1:]
        active = endpoint_list[0]
        if len(rest) >= 2 and rest[0] == "--_endpoint":
            active = next((e for e in endpoint_list if e.name == rest[1]), active)
            rest = rest[2:]
        query = " ".join(rest).strip()
        if query:
            _print_search_rss_rows(query, active)
        return
```

- [ ] **Step 3: search state: dynamic prompt + Ctrl-E**

In `_run_search_state_machine`, the search state becomes:

```python
        if state == "search":
            prompt_label = f"{C_YEL}{eps.active.name} >{C_OFF} "
            result, key = fzf_search_prompt(
                prompt_label, default=last_search_query,
                endpoint_name=eps.active.name,
                switch_hint=len(eps.endpoints) > 1)
            if key == "ctrl-e":
                if result:
                    last_search_query = result
                pick_endpoint(eps)
                continue
            if result is None:
                return "", [], ACT_CANCEL
            last_search_query = result
            initial_query = result
            state = "fetch"
            continue
```

(Import `pick_endpoint` from `anirss_lib.cli.pickers` in main.py; `C_YEL` is already imported there via the ansi import if not, add it.)

- [ ] **Step 4: fetch state: auto-fallback**

Replace the Task 8 interim fetch state with:

```python
        if state == "fetch":
            print(f"{C_BLD}Query:{C_OFF} {initial_query}")
            print(f"{C_DIM}fetching {eps.active.name}...{C_OFF}")
            prev_name = eps.active.name
            reason = "0 results"
            try:
                items = endpoints_mod.fetch_items(eps.active, initial_query)
            except FetchError as e:
                items = []
                reason = "unreachable"
                print(f"{C_YEL}{e}{C_OFF}")
            if not items and len(eps.endpoints) > 1:
                items, notes = endpoints_mod.probe_fallback(eps, initial_query)
                if items:
                    print(f"{C_YEL}{prev_name}: {reason}, switched to "
                          f"{eps.active.name} ({len(items)}){C_OFF}")
                elif notes:
                    print(f"{C_DIM}also tried: {', '.join(notes)}{C_OFF}")
            if not items:
                where = " anywhere" if len(eps.endpoints) > 1 else ""
                print(f"{C_YEL}no results for {initial_query!r}{where}; edit and "
                      f"try again ({C_DIM}↑ recalls last query{C_OFF}{C_YEL}){C_OFF}")
                state = "search"
                continue
            query, selected = initial_query, items
            state = "refine"
            continue
```

- [ ] **Step 5: bare-URL flow honors endpoint hosts**

In `_run_interactive`, compute the host sets once and use them in classification:

```python
        nyaa_hosts, rss_hosts = endpoint_hosts(eps.endpoints)
        kind = (classify_url(arg, nyaa_hosts=nyaa_hosts, rss_hosts=rss_hosts)
                if arg else UrlKind.NOT_URL)
        if kind == UrlKind.ENDPOINT_RSS:
            # A pasted URL for a configured rss-kind endpoint: treat it as a
            # raw feed, same as `anirss -S <url>`.
            force_url = arg
            kind = UrlKind.NOT_URL
```

and the `OTHER_HTTP` die message becomes: `die(f"bare URL doesn't match any configured endpoint - use `anirss -S {arg}` to subscribe")`. Import `endpoint_hosts` from `anirss_lib.cli.urls`.

- [ ] **Step 6: manual smoke verification** (needs network; skip gracefully if offline)

```bash
python -c "
from anirss_lib.endpoints import Endpoint, EndpointState, fetch_items, probe_fallback
anirena = Endpoint(name='anirena', kind='rss', url='https://www.anirena.com/rss?q={query}&adult=1')
items = fetch_items(anirena, '1080p')
print(len(items), 'items;', items[0].title[:60] if items else '-')
print('link:', items[0].link if items else '-')
print('size:', items[0].size if items else '-')
"
```

Expected: a nonzero count, a `.torrent` link, a parsed size. Then run the real TUI once: `./anirss -e anirena "one piece"` and confirm the prompt shows `anirena >`, Ctrl-E flips to nyaa, and a nonsense query on nyaa auto-falls-back with the switch notice.

- [ ] **Step 7: Run the suite and commit**

Run: `python -m pytest -q`
Expected: all pass.

```bash
git add anirss_lib/main.py anirss_lib/fzf.py
git commit -m "Wire -e flag, auto-fallback on empty search, live-search endpoint, endpoint-host URLs"
```

---

### Task 10: Subscribe: rule-name dedupe across endpoints

**Files:**
- Modify: `anirss_lib/qbt/actions.py`
- Modify: `anirss_lib/main.py` (pass endpoint name, use returned name in summary)
- Test: `tests/test_qbt_subscribe.py` (new)

**Interfaces:**
- Consumes: `QbtSession.get_json` (exists, see `qbt/feeds.py`).
- Produces: `do_subscribe(qbt, feed_url, name, save_base, endpoint_name: str = "") -> str` returning the final rule/feed name (suffixed with ` @<endpoint>` when the name exists with a different feed URL). Internal helper `_unique_rule_name(qbt, name, feed_url, endpoint_name) -> str`.

- [ ] **Step 1: Write the failing tests** (create `tests/test_qbt_subscribe.py`)

```python
from anirss_lib.qbt import actions


class FakeQbt:
    def __init__(self, rules):
        self._rules = rules

    def get_json(self, path):
        assert path == "/api/v2/rss/rules"
        return self._rules


def test_unique_rule_name_no_collision():
    qbt = FakeQbt({})
    assert actions._unique_rule_name(qbt, "Show", "http://f", "anirena") == "Show"


def test_unique_rule_name_same_feed_overwrites():
    qbt = FakeQbt({"Show": {"affectedFeeds": ["http://f"]}})
    assert actions._unique_rule_name(qbt, "Show", "http://f", "anirena") == "Show"


def test_unique_rule_name_other_feed_suffixes():
    qbt = FakeQbt({"Show": {"affectedFeeds": ["http://nyaa-feed"]}})
    assert (actions._unique_rule_name(qbt, "Show", "http://anirena-feed", "anirena")
            == "Show @anirena")


def test_unique_rule_name_survives_api_errors():
    class Boom:
        def get_json(self, path):
            raise RuntimeError("down")
    assert actions._unique_rule_name(Boom(), "Show", "http://f", "x") == "Show"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_qbt_subscribe.py -q`
Expected: FAIL with `AttributeError: ... _unique_rule_name`.

- [ ] **Step 3: Implement**

In `qbt/actions.py`, above `do_subscribe`:

```python
def _unique_rule_name(qbt: QbtSession, name: str, feed_url: str,
                      endpoint_name: str) -> str:
    """Same show subscribed from a different endpoint gets ' @endpoint'
    suffixed so the existing rule and feed aren't silently overwritten.
    Re-subscribing the same feed keeps the name (idempotent overwrite)."""
    try:
        rules = qbt.get_json("/api/v2/rss/rules")
    except Exception:
        return name
    if not isinstance(rules, dict) or name not in rules:
        return name
    feeds = rules[name].get("affectedFeeds") or []
    if feed_url in feeds or not endpoint_name:
        return name
    return f"{name} @{endpoint_name}"
```

`do_subscribe` becomes:

```python
def do_subscribe(qbt: QbtSession, feed_url: str, name: str, save_base: str,
                 endpoint_name: str = "") -> str:
    name = _unique_rule_name(qbt, name, feed_url, endpoint_name)
    save_path = os.path.join(save_base, name)
    ...unchanged body...
    return name
```

In `main.py`, both `do_subscribe` calls pass `endpoint_name=eps.active.name` and use the return value: interactive summary uses the returned name (`name = do_subscribe(qbt, feed_url, name, save_base, endpoint_name=eps.active.name)` before building `summary`); non-interactive call is `do_subscribe(qbt, feed_url_for_sub, default_name, _save_base_for(parsed, cfg), endpoint_name=eps.active.name)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add anirss_lib/qbt/actions.py anirss_lib/main.py tests/test_qbt_subscribe.py
git commit -m "Suffix subscription names with @endpoint on cross-endpoint collisions"
```

---

### Task 11: Docs: help text, README, completions

**Files:**
- Modify: `anirss_lib/__init__.py` (usage docstring)
- Modify: `README.md`
- Modify: `completions/_anirss`, `completions/anirss.bash`

**Interfaces:** none (documentation only).

- [ ] **Step 1: help docstring** (`anirss_lib/__init__.py`)

Add under the first Usage block, after the `anirss <magnet|*.torrent>` lines:

```
    anirss -e <endpoint> [query]  Start on a specific endpoint (see [[endpoint]]
                                  in config; Ctrl-E switches inside the picker).
```

- [ ] **Step 2: README**

Add an "Endpoints" section after the existing configuration docs: what `[[endpoint]]` is, the two kinds with the nyaa and AniRena examples from the default config, priority order, Ctrl-E, `-e <name>`, auto-fallback on empty initial search, and a note that `[search]` is the deprecated legacy fallback used only when no `[[endpoint]]` is defined. Follow the README's existing tone and formatting (read it first).

- [ ] **Step 3: completions**

`completions/_anirss`: add to `ops`:

```
    '-e:start on a specific endpoint'
    '--endpoint:start on a specific endpoint'
```

`completions/anirss.bash`: add `-e --endpoint` to the `ops` string.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest -q` (docstring edit is import-sensitive, so run the suite).

```bash
git add anirss_lib/__init__.py README.md completions/_anirss completions/anirss.bash
git commit -m "Document multi-endpoint support in help, README, completions"
```

---

## Post-plan notes for the executor

- Version bump / packaging (PKGBUILD, brew, .SRCINFO) is a release chore done separately by the user's release flow; do NOT bump `__version__` in this plan.
- Every task's suite run is `python -m pytest -q` from the repo root; there is also a legacy `test_anirss.py` at the root that pytest picks up automatically.
- `_deep_merge` replaces lists wholesale, so a user `[[endpoint]]` list fully overrides the default one; nothing merges per-item.
- Watch the `state` name collision in `_run_search_state_machine` (string state vs EndpointState): the EndpointState parameter is named `eps` in main.py.
