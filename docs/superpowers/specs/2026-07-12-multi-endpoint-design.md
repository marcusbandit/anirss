# Multi-Endpoint Support: Design

Date: 2026-07-12
Status: approved (behavior model chosen: switch + auto-fallback)

## Problem

anirss is hardwired to a single search endpoint (`search.nyaa_url` in
`config.toml`). When a show is not on nyaa but is on another tracker
(concrete case: AniRena), the only recourse is editing the config file.
The user wants multiple endpoints available at once, switchable on the
fly, without merging complexity.

## Goals

- Configure any number of endpoints in `config.toml`, ordered by priority.
- Switch the active endpoint on the fly inside the picker (keybind) and
  from the CLI (`-e <name>`).
- Auto-fallback: when the initial search returns zero results (or the
  endpoint is unreachable), probe the remaining endpoints in priority
  order and hop to the first one with hits, with a visible notice.
- Support two endpoint kinds: `nyaa` (nyaa-style sites, full stats) and
  `rss` (any RSS search URL with a `{query}` placeholder).
- Existing single-endpoint configs keep working with zero edits.

## Non-Goals (explicitly out of scope)

- Merged results across endpoints and cross-endpoint dedup.
- Per-show endpoint memory.
- Multi-feed (one-show-many-trackers) subscriptions.
- Auto-probing other endpoints during refine or live typing.

## Config Schema

New `[[endpoint]]` array of tables. Order is priority; the first entry
is the default active endpoint.

```toml
[[endpoint]]
name = "nyaa"
kind = "nyaa"            # nyaa-style site: page/q/c/f params, stats namespace
url  = "https://nyaa.si/"
category = "1_0"         # nyaa-kind only
filter   = "0"           # nyaa-kind only

[[endpoint]]
name = "anirena"
kind = "rss"             # generic RSS search template
url  = "https://www.anirena.com/rss?q={query}&adult=1"
```

- `name` must be unique; it is what `-e` and the switcher display use.
- `kind` is `"nyaa"` or `"rss"`. Unknown kinds are a config error at load.
- For `kind = "rss"`, `url` must contain `{query}`; the query is
  URL-encoded into it. Extra fixed params (like `adult=1`) just live in
  the template. `category`/`filter` are ignored for `rss` kind.

### Backward compatibility and migration

- If the loaded config contains no `[[endpoint]]` blocks, synthesize one
  at load time from the existing `[search]` section
  (`nyaa_url`/`category`/`filter`), named `nyaa`. No file rewrite; old
  configs work untouched.
- `DEFAULT_CONFIG_TOML` gains the `[[endpoint]]` nyaa block plus a
  commented-out `rss` example. `[search]` keys remain in the default
  for now (deprecated in README) so `migrate_config()`'s
  section-granular append logic stays valid.
- Naming: the config key is `endpoint`, never `source`, to avoid
  colliding with `[bestfit].source_order` (video source: WEB-DL/BluRay).

## Endpoint Abstraction

New module `anirss_lib/endpoints.py`:

```python
class Endpoint(NamedTuple):
    name: str
    kind: str        # "nyaa" | "rss"
    url: str
    category: str    # nyaa kind only
    filter: str      # nyaa kind only

def load_endpoints(cfg) -> list[Endpoint]   # incl. legacy [search] synthesis
def search_url(ep: Endpoint, query: str) -> str
def fetch_items(ep: Endpoint, query: str) -> list[Item]
```

Dispatch on `kind`:

- `kind = "nyaa"`: delegates to the existing `nyaa.py` logic unchanged
  (`page=rss&q&c&f` params, `https://nyaa.si/xmlns/nyaa` namespace for
  seeders/leechers/downloads/size/category).
- `kind = "rss"`: fills `{query}` into the template and parses standard
  RSS 2.0 `<item>` elements:
  - `title`: item title as-is.
  - `link`: prefer the `<enclosure url>` when its type is a torrent
    (verified on AniRena: `<link>` is the torrent's web page, the
    `.torrent` file is the enclosure; on nyaa the link is the .torrent
    directly). Fall back to `<link>` when no enclosure exists.
  - Stats: seeders/leechers/downloads are 0 (the responsive renderer
    already hides stats columns that are absent).
  - Best-effort extras from `<description>`: parse `Size:\s*([\d.]+\s*[KMGT]i?B)`
    into `size` and `Category:\s*([^|]+)` into `category` when present
    (AniRena embeds both); leave blank otherwise.

The `Item` NamedTuple (`types.py`) is already endpoint-neutral and does
not change.

### Threading the active endpoint

The active `Endpoint` replaces `SearchConfig` on every path where
`search` is threaded today: `main.py` state machine, `refine()`,
`fetch_items`, and the hidden live-search self-invocation. The
`anirss --_search-rss {q}` reload command gains an internal
`--_endpoint <name>` flag so search-as-you-type hits the active
endpoint.

## Switching UI

- `ctrl-e` in the search picker and the refine picker switches endpoint
  and re-runs the current query. With exactly two endpoints it cycles;
  with three or more it opens a small fzf pick listing names.
- The fzf prompt always shows the active endpoint, e.g. `nyaa >` /
  `anirena >`.
- CLI: `-e <name>` / `--endpoint <name>` starts the session on that
  endpoint. Unknown names die with the list of configured names.

## Auto-Fallback

Applies only to the initial search fetch after query submit:

- Zero results, or a network/parse error on the active endpoint: probe
  the remaining endpoints in priority order (sequential, same timeout as
  a normal fetch). Switch to the first endpoint with more than zero hits
  and show a notice line: `nyaa: 0 results, switched to anirena (27)`.
  On error the notice says so: `nyaa: unreachable, switched to ...`.
- All endpoints empty: report per-endpoint counts and exit as today.
- During refine: never auto-hop (zero results usually means the
  exclusions worked; refine already reverts to the previous result set).
  Discoverability comes from a persistent `Ctrl-E endpoint` hint in the
  refine picker header instead of transient messages.
- Live typing: never probes non-active endpoints.

## Refine, Best Fit, Exclusions

All operate against the active endpoint, as today. Differences by kind:

- nyaa understands `-tag` / `-"phrase"` exclusion in the query string;
  generic RSS sites do not. For `kind = "rss"`, exclusion terms are kept
  out of the sent query and applied client-side by filtering the fetched
  items (case-insensitive substring on title).
- Token picks and Try Best Fit rebuild plain-word queries, which work in
  both kinds unchanged.

## Subscribe and URL Handling

- `do_subscribe` uses the active endpoint's `search_url` as the qB feed
  URL; feed/rule mechanics are untouched. qBittorrent consumes RSS
  enclosures natively, so `rss`-kind feeds subscribe fine.
- Feed naming: unchanged for the first subscription of a show. If the
  same feed name already exists (same show subscribed on another
  endpoint), suffix the new feed name with ` @<endpoint>` to avoid
  collisions in qB and `feeds.txt`.
- Bare-URL flow (`-S <url>` and pasted URLs): `cli/urls.py` currently
  accepts only `nyaa.si`. Change: accept any URL whose host matches a
  configured endpoint's host. nyaa-kind hosts keep query extraction;
  rss-kind hosts are treated as raw RSS URLs (the existing
  `_fetch_items_from_url` path).

## Error Handling

- Fetch errors name the endpoint (`can't reach anirena: ...`) instead of
  the hardcoded `can't reach nyaa`.
- Initial fetch errors trigger the fallback probe (above). Errors during
  refine keep dying as today, but with the endpoint named.
- Config validation at load: duplicate names, unknown `kind`, missing
  `{query}` in an rss template are all fatal with a clear message.

## Testing

- Legacy synthesis: config with only `[search]` yields one nyaa-kind
  endpoint carrying its url/category/filter.
- `[[endpoint]]` parsing: order preserved, validation errors fire.
- `search_url` per kind, including URL-encoding into `{query}` and
  preservation of fixed params (`adult=1`).
- Generic RSS parser against a captured AniRena feed sample: enclosure
  preferred over link, size/category best-effort parsing, zero stats.
- Client-side exclusion filtering for rss kind.
- Fallback decision logic (pure function: results-per-endpoint in, chosen
  endpoint + notice out).
- All existing nyaa-path tests pass unchanged.
