> **Agent documentation.** This file is written mainly for coding agents
> implementing against anirss. It is exhaustive and literal on purpose. If
> anirss grows a wider audience, human-facing documentation will be hosted on
> a proper docs site rather than spread across repo files; until then, the
> files in `docs/agents/` are the source of truth.

# anirss API reference

anirss searches anime torrent RSS endpoints (nyaa, Erai-raws, AniRena, any RSS
search URL) and drives qBittorrent. This is the lookup half of the agent docs:
every constructor option, method, route, and data shape. The steps for
building a frontend, embedding anirss, or extending it live in
[README.md](README.md); start there.

Contents: [Overview](#overview), [Installing](#installing-as-a-dependency),
[Python API](#python-api), [HTTP API](#http-api), [Data shapes](#data-shapes),
[Stability](#stability).

## Overview

There are three ways in. They all run the same code.

| Way in | Use it when |
| --- | --- |
| Python import (`anirss_lib.api.Anirss`) | Your app is Python and runs on the same machine. No network, no token. |
| HTTP server (`anirss --serve`) | Your frontend is not Python (web UI, Electron, mobile, another service). JSON over HTTP, bearer token. |
| CLI (`anirss ...`) | Interactive use, shell scripts. See the README. Not covered here. |

The HTTP server is a thin wrapper: it parses the request, calls one method of a
single shared `Anirss` instance, and serializes the result with
`to_jsonable()`. Every HTTP response shape below is the JSON form of the
corresponding Python return value.

Two facts to keep in mind throughout:

- anirss holds no search state. The caller keeps the current `{endpoint,
  query, items}` and sends it back with each refine request.
- Paths in results (`save_path`, `content_path`, subscription paths) are paths
  on the machine running qBittorrent, not necessarily the machine running
  anirss or your client.

## Installing as a dependency

anirss uses `tomllib`, so it needs Python 3.11 or newer. The only
non-Python tools it shells out to are optional CLI conveniences (fzf for the
TUI); the library and server do not need them.

Install with [uv](https://docs.astral.sh/uv/). There are no runtime
dependencies.

For the HTTP route (any non-Python client, including Rust), only the `anirss`
command has to exist on the machine that runs the server:

```
uv tool install git+https://github.com/marcusbandit/anirss   # puts `anirss` on PATH
anirss --serve
```

An existing AUR, brew, or `install.sh` install provides the same command.

For the Python route, add it to the consuming project:

```
uv add git+https://github.com/marcusbandit/anirss
```

`uv add /path/to/anirss` works with a local checkout. Without an install, a
checkout on the import path also works (`PYTHONPATH=/path/to/anirss`, the
directory containing `anirss_lib/`). The package ships a `py.typed` marker.

## Python API

### Quickstart

```python
from anirss_lib.api import Anirss
from anirss_lib.errors import AnirssError

ani = Anirss()                                   # reads ~/.config/anirss/config.toml

res = ani.search("Frieren 1080p")                # SearchResult
print(res.endpoint, len(res.items), res.fell_back)

# Analyze: what can be narrowed by, and what "best fit" would do.
view = ani.analyze(res.query, res.items, res.endpoint)
for g in view["groups"]:
    print(g["label"], g["member_count"])

# Refine one step. You own the history; keep the old state to go "back".
history = [(res.query, res.items)]
out = ani.refine(res.query, res.items, "tokens",
                 endpoint=res.endpoint, tokens=view["groups"][0]["tokens"])
if out.applied:
    history.append((out.query, out.items))
else:
    print("nothing changed:", out.reason)

# Subscribe (qBittorrent RSS feed + auto-download rule) ...
query, items = history[-1]
sub = ani.subscribe(query, endpoint=res.endpoint, items=items)
print(sub.name, sub.feed_url, sub.save_path)

# ... or download these releases once.
added = ani.download_items(items[:3], name="Frieren")
print(added.count, added.hashes)

# Then watch progress.
for t in ani.torrents(name="Frieren"):
    print(t["name"], t["state"], t["progress"], t["content_path"])
```

Everything raises a subclass of `AnirssError`; nothing prints, prompts, or
exits.

### Constructor

```python
Anirss(config=None, *, config_path=None, qbt_password=None,
       autostart_qbt=False, persist_session=False, config_file=None)
```

| Option | Meaning |
| --- | --- |
| `config` | A full or partial config dict in `config.toml` shape (`{"qbittorrent": {"url": ...}, "downloads": {...}, "endpoint": [...]}`). Merged over the defaults, with no disk access. When given, `config_path` is ignored. |
| `config_path` | Path to a config TOML file. Default `~/.config/anirss/config.toml`. Loaded strictly (an invalid config raises `ConfigError`) and never created if missing. |
| `qbt_password` | The default qBittorrent client's WebUI password. If `None`, falls back to the `ANIRSS_QBT_PASSWORD` environment variable, then to the password the CLI saved (the mode-600 `qbt.pass` file under `~/.local/state/anirss/`) when the cached session is not enough. An empty password is valid for WebUI setups with auth bypass. Other clients: see [qBittorrent clients](#qbittorrent-clients). |
| `autostart_qbt` | Start a local qBittorrent when it is not running, as the CLI does. Default `False`: a library should not spawn processes. |
| `persist_session` | Write the qBittorrent session cookie to the SID cache the CLI shares. Default `False`: the cache is read but not written. |
| `config_file` | The file `add_endpoint` and `remove_endpoint` write. Defaults to the file the config was read from; `None` with a `config` dict, which makes both raise `ConfigError`. |

anirss ships with no endpoints. A fresh config has an empty list, and every
call that needs one raises `NoEndpointsError` until the user adds their own.

The constructor does not contact qBittorrent. Login happens lazily on the first
call that needs it (`subscribe*`, `download*`, `movie`, `upload_torrent`,
`subscriptions`, `remove`, `torrents`). Search, analyze, refine, and enrich
need only the network path to the endpoints.

A password qBittorrent rejects is not retried on later calls, because
qBittorrent bans an IP after a few failed logins. Fix the credentials and call
`set_qbt_password(...)`. A still-valid cached session lets calls proceed in the
meantime. An expired session (401/403 mid-call) is re-established once and the
call retried.

### qBittorrent clients

A config can name several qBittorrents (a seedbox and the one on this machine,
say) in `[[qbittorrent.client]]` blocks:

```toml
[qbittorrent]
username = "me"        # every key here is the default a block can override

[[qbittorrent.client]]
name = "seedbox"
url = "http://192.0.2.10:8080"

[[qbittorrent.client]]
name = "local"
url = "http://localhost:8080"
username = "admin"
```

Each block needs a unique `name` (letters, digits, `.`, `_`, `-`) and a `url`;
any other `[qbittorrent]` key it leaves out (`username`, `tags`,
`save_password`, ...) comes from `[qbittorrent]`. The first block is the
default. A config with no blocks has one client, named `default`, built from
`[qbittorrent]` itself. An invalid block raises `ConfigError` at construction.

A block may set its own `save_base`, `movie_path` and `hidden_base`. These are
paths on the machine that client runs on; a block that leaves one out uses
`[downloads]`'s. A client's files count as on this machine when its url is
local (`localhost`, `127.0.0.1`, `::1`), unless the block sets
`local_files = true` or `false` (a NAS mounted at the same path, say). Only a
local client's paths may start with `~`; anirss expands it.

Every method that reaches qBittorrent takes `client=None` (the default client)
or a client's name; an unknown name raises `NotFoundError`. Each client logs in
on its own, with its own session, rejected-password cool-down and password: the
`qbt_password` option and `ANIRSS_QBT_PASSWORD` belong to the default client
only, and another client uses the password the CLI saved for it
(`anirss -c <name>` once, interactively), its WebUI's auth bypass, or
`set_qbt_password(password, client=name)`. The CLI keeps a named client's
session and password in `qbt-<name>.sid` and `qbt-<name>.pass` beside the
default client's `qbt.sid` and `qbt.pass`.

### Methods

Endpoints and search:

| Method | Returns | Notes |
| --- | --- | --- |
| `version` (property) | `str` | |
| `endpoints()` | `list[Endpoint]` | Priority order; the first is the default. |
| `endpoint(name=None)` | `Endpoint` | `NotFoundError` for an unknown name, `NoEndpointsError` when none is configured. |
| `add_endpoint(spec)` | `(Endpoint, bool)` | Writes `{name, kind, url, category?, filter?}` into the config file and starts using it, after the others or in place of the one with that name; the bool is whether it replaced one. Any other key, `start` and `stop` included, is a `ValidationError`: on-demand endpoints run commands and stay hand-written. The previous file is kept as `config.toml.bak`; comments and other sections are untouched. |
| `remove_endpoint(name)` | `None` | Takes the endpoint out of the file and out of use. Its subscriptions keep running. `NotFoundError` for an unknown name. |
| `search(query, endpoint=None, *, fallback=True, enrich=False)` | `SearchResult` | With `fallback`, an empty or unreachable endpoint falls through to the others in order. Items from a fallback endpoint carry `source`. Raises `FetchError` only when every consulted endpoint was unreachable; zero results is an empty `SearchResult`, not an error. |
| `fetch_feed(url, *, enrich=False)` | `list[Item]` | Any http(s) RSS URL. |
| `enrich(items, *, deadline_s=20.0)` | `list[Item]` | Fills unknown `seeders`/`leechers`/`downloads` by scraping trackers (cached). Never raises. Can block about 20 seconds. |

`SearchResult` has `query`, `endpoint` (where the items came from), `items`,
`tried`, and the property `fell_back`. `tried` has one entry per endpoint
consulted: `{"endpoint", "status": "ok"|"empty"|"unreachable", "count",
"error"}`.

Refining:

| Method | Returns | Notes |
| --- | --- | --- |
| `analyze(query, items, endpoint=None, *, auto_resolution=True)` | `dict` | See [analyze shape](#post-refineanalyze). Contains `Item` objects; run `to_jsonable()` for JSON. |
| `refine(query, items, op, *, endpoint=None, tokens=None, term="", enrich=False)` | `RefineOutcome` | `op` is one of `tokens`, `term`, `exclude`, `best_fit`. |
| `best_item(items)` | `Item` or `None` | The release best fit would pick, per `[bestfit]` config. |
| `top(items, n=1)` | `list[Item]` | Top n by downloads, unknown counts last. |

Subscribing and downloading (these need qBittorrent):

| Method | Returns | Notes |
| --- | --- | --- |
| `subscription_spec(query, items, endpoint=None)` | `SubscriptionSpec` | What `subscribe` would register. Does not touch qBittorrent (an Erai endpoint may cost one probe request). |
| `subscribe(query, *, endpoint=None, items=None, name=None, hidden=False, client=None)` | `SubscribeResult` | `items` are fetched (no fallback) if omitted; `NotFoundError` if there are none. `name` defaults to the show name parsed from the first item. |
| `subscribe_feed(url, *, name=None, hidden=False, client=None)` | `SubscribeResult` | Subscribe to a raw RSS URL as is. |
| `download(links, *, name, hidden=False, client=None)` | `AddResult` | `magnet:` or http(s) links only. Saves under `<save_base>/<name>`. |
| `download_items(items, *, name=None, hidden=False, client=None)` | `AddResult` | `download()` for search results. |
| `movie(link, *, client=None)` | `AddResult` | One link into `[downloads].movie_path`, no subdirectory. |
| `upload_torrent(data, *, file_name="file.torrent", name=None, hidden=False, client=None)` | `AddResult` | Raw `.torrent` bytes. Works with a remote qBittorrent. |

`client` picks the qBittorrent (see [qBittorrent clients](#qbittorrent-clients)),
and its `tags` are the ones stamped on what it adds.

Managing:

| Method | Returns | Notes |
| --- | --- | --- |
| `subscriptions(*, client=None)` | `list[RuleInfo]` | |
| `remove(names, *, drop_torrents=False, drop_files=False, hidden=False, dry_run=False, client=None)` | `list[RemovePlan]` | Removes the feed and rule. `drop_torrents` also removes their torrents, `drop_files` also deletes files on disk (confined to the client's download roots). On a client whose files are not on this machine, anirss never touches the disk: `drop_files` needs `drop_torrents`, so qBittorrent deletes them, and the plan's size is the torrents' size as qBittorrent reports it. `drop_files` alone raises `ValidationError` there. `dry_run` returns the plans and changes nothing. Unknown names raise `NotFoundError` before anything is removed. |
| `torrents(*, name=None, hashes=None, hidden=False, client=None)` | `list[dict]` | All torrents, or those saved under `<save_base>/<name>`, or by info-hash. |
| `clients()` | `list[dict]` | Every client's resolved `[qbittorrent]` settings plus `name`, in priority order; the first is the default. |
| `client(name=None)` | `dict` | One of those. `NotFoundError` for an unknown name. |
| `qbt_status(client=None)` | `dict` | Never logs in. See [GET /qbt/status](#get-qbtstatus). |
| `set_qbt_password(password, *, client=None)` | `None` | Swap a client's credentials and clear its login failure. |

`hidden=True` switches the base directory from `[downloads].save_base` to
`[downloads].hidden_base`. `name` is validated; a name containing path
separators or otherwise unsafe raises `ValidationError`.

Helpers also exported from `anirss_lib.api`:

- `to_jsonable(obj)`: results to JSON-ready builtins.
- `item_from_dict(d)`: rebuild an `Item` from its JSON form (unknown keys
  ignored, `null` fields take their default).
- `endpoint_info(ep)`: display-safe dict for an `Endpoint`: `name`, `kind`,
  `url` (redacted), `client_side_refine`. This is what `GET /endpoints` returns
  per endpoint. Note that `Anirss.endpoints()` returns the full `Endpoint`
  with its real URL, including any token.
- `client_info(client)`: display-safe dict for one of `Anirss.clients()`:
  `name` and `url` (credentials in the URL masked). This is what `GET /clients`
  returns per client.
- `redact_url(url)`: returns `url` with the values of query parameters named
  `token`, `apikey`, `api_key`, `key`, `passkey`, `auth`, or `secret`
  (case-insensitive, non-empty values only) replaced by `***`. A URL without a
  query string is returned unchanged.

### Thread safety

One `Anirss` instance can be shared across threads; the HTTP server does
exactly that.

- Calls that talk to qBittorrent are serialized by an internal lock (rule
  naming is check-then-act). Expect them to queue, not interleave.
- `enrich()` is limited to two concurrent runs process-wide, because each
  fans out many tracker scrapes.
- Search, analyze, and refine are stateless and run in parallel.
- Separate `Anirss` instances do not share a lock or a login. If you create
  several against one qBittorrent, rule-name collisions are possible.

### Error classes

All in `anirss_lib.errors`:

| Class | Parent | Raised when |
| --- | --- | --- |
| `AnirssError` | `Exception` | Base. Catch this to catch everything anirss raises on purpose. |
| `ConfigError` | `AnirssError` | Config or an `[[endpoint]]` block is invalid. |
| `NoEndpointsError` | `ConfigError` | No endpoint is configured, so there is nothing to search. |
| `ValidationError` | `AnirssError` | A caller-supplied value was rejected (empty query, bad name, bad link scheme, malformed item). |
| `NotFoundError` | `AnirssError` | Unknown endpoint, subscription, or no results to subscribe to. |
| `FetchError` | `AnirssError` | A search endpoint could not be fetched or parsed. |
| `QbtError` | `AnirssError` | qBittorrent unreachable, or it rejected a call. |
| `QbtAuthError` | `QbtError` | No usable credentials, or they were rejected. |

Other exceptions (bugs, `OSError`) are not wrapped.

## HTTP API

### Starting the server

```
anirss --serve [--host HOST] [--port PORT]
```

Defaults come from `[server]` in `config.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8787
token = ""             # bearer token; ANIRSS_API_TOKEN overrides
cors_origins = []      # e.g. ["http://localhost:5173"]
```

`--host` and `--port` override the file. On start it prints the base URL
(`http://HOST:PORT/api/v1`) and the token, then serves until Ctrl-C. Port `0`
picks a free port (the printed URL shows which). The server is stdlib
`http.server` with one thread per connection; there is no TLS. If you bind
beyond loopback, put a TLS reverse proxy in front.

The server builds its `Anirss` with default options: it never autostarts
qBittorrent, does not persist the session cookie, and takes the qBittorrent
password from `ANIRSS_QBT_PASSWORD` or the saved password file. Set the
variable in the environment of the `anirss --serve` process.
`ANIRSS_QBT_URL` and `ANIRSS_QBT_USERNAME` override `[qbittorrent] url` and
`username` from the config file the same way, for a client that runs its own
qBittorrent beside anirss. `ANIRSS_QBT_URL` also drops any
`[[qbittorrent.client]]` blocks, so the server has that one qBittorrent and no
other. When that address is not the one `[qbittorrent] url` names, it keeps its
session and saved password apart (`qbt-env-<host>_<port>.sid` and `.pass`), so
anirss never sends the configured server's cookie or saved password to it.
`anirss -c <name> --serve` likewise serves one client from the config. If qBittorrent
needs a login and none is available, qBittorrent routes return `503
qbt_auth_required`.

All routes live under `/api/v1`. Examples below assume:

```
BASE=http://127.0.0.1:8787/api/v1
TOKEN=...   # printed at startup when generated; else your ANIRSS_API_TOKEN / [server].token
```

### Authentication and token resolution

Every route except `GET /health` needs `Authorization: Bearer <token>`. The
comparison is constant-time. A missing or wrong token gives `401
unauthorized`.

The token is resolved once at startup, first match wins:

1. `ANIRSS_API_TOKEN` environment variable.
2. `[server].token` in the config.
3. `~/.local/state/anirss/api.token` (the `STATE_DIR`), if it exists and is not
   empty.
4. Otherwise a new random token is generated, written to that file with mode
   600, and used from then on. If the file cannot be written, the token is
   used for this run only and a warning is logged.

The startup banner states which source was used.

### Loopback default and the Host-header guard

The server binds `127.0.0.1` by default. Loopback is not treated as trusted on
its own, since any web page in the user's browser can send requests to it. Two
protections apply:

- The bearer token (above).
- When the bind host is `127.0.0.1`, `localhost`, or `::1`, requests whose
  `Host` header names anything else get `403 forbidden` ("unexpected Host
  header"). This blocks DNS-rebinding. When you bind elsewhere (`0.0.0.0`, a
  LAN address) the guard is off and the token is your only protection.

The guard checks the `Host` header name only (port ignored), so reaching a
loopback-bound server through a reverse proxy requires the proxy to send a
loopback `Host`.

### CORS

Cross-origin access is off by default. List allowed origins in
`[server].cors_origins` (exact string match, e.g. `"http://localhost:5173"`;
no wildcard). For a request whose `Origin` is listed, responses carry
`Access-Control-Allow-Origin: <that origin>` and `Vary: Origin`. Preflight
`OPTIONS` requests on any path return `204` with
`Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS`,
`Access-Control-Allow-Headers: Authorization, Content-Type`, and
`Access-Control-Max-Age: 600`. Preflights do not require the token. Credentials
(cookies) are not used; send the token in the `Authorization` header.

Non-browser clients (a native app, a server) are not affected by CORS.

### Requests and responses

- JSON bodies must be an object and sent with `Content-Type:
  application/json`. An empty body is treated as `{}`. Max 8 MiB.
- `items` arrays are capped at 2000 entries.
- Booleans in query strings and bodies accept `true/false`, `1/0`, `yes/no`,
  `on/off` (case-insensitive). Anything else is `400`.
- Responses are `application/json`. Trailing slashes on paths are ignored.
- Unknown JSON keys in a body are ignored.

### Error envelope and status mapping

Every error has the same shape:

```json
{"error": {"type": "validation_error", "message": "empty query"}}
```

| HTTP | `error.type` | Cause |
| --- | --- | --- |
| 400 | `validation_error` | `ValidationError`: bad or missing parameter, bad JSON, wrong Content-Type, body too large. |
| 400 | `error` | Any other `AnirssError` not listed below. |
| 401 | `unauthorized` | Missing or bad bearer token. |
| 403 | `forbidden` | Host-header guard rejected the request. |
| 404 | `not_found` | Unknown route, unknown endpoint, unknown subscription, no results to subscribe to (`NotFoundError`). |
| 405 | `method_not_allowed` | Route exists, method does not. |
| 409 | `no_endpoints` | `NoEndpointsError`: no endpoint is configured yet. Offer the user a way to add one. |
| 500 | `config_error` | `ConfigError`. |
| 500 | `internal_error` | Unexpected exception. The message is generic; the traceback goes to `anirss.log`. |
| 502 | `fetch_error` | `FetchError`: every search endpoint was unreachable, or a refetch/feed fetch failed. |
| 502 | `qbt_error` | `QbtError`: qBittorrent unreachable or rejected a call. |
| 503 | `qbt_auth_required` | `QbtAuthError`: no usable qBittorrent credentials, or they were rejected. |

Order of checks: Host guard, then route prefix, then auth, then route and
method, then the handler. An unauthenticated request to a nonexistent
`/api/v1/...` route therefore gets `401`, not `404`.

### The refine loop (stateless)

The server remembers nothing between requests. The client holds the state
`{endpoint, query, items}` and its own history stack.

1. Search: `GET /search?q=...` returns `items`.
2. Call `POST /refine/analyze` once with `{query, items, endpoint}`. It returns
   the possibly auto-resolved state (`query`, `items`), the `groups` you can
   narrow by, and `best_fit`. Push this state onto your history.
3. For each user action, call `POST /refine/apply` with the current state
   plus `op`. If `applied` is true, push the returned `{query, items}` onto
   history and render the returned `groups` and `best_fit` (they are already
   recomputed, so no second analyze call is needed). If `applied` is false,
   the returned `query` and `items` equal what you sent; show `reason`.
4. "Back" is popping your history stack. The server is not involved.
5. When the user is done, send the current `{query, items}` to
   `POST /subscriptions` or `POST /downloads`.

Auto-resolution (analyze only): if the query has no `<n>p` token and every item
title has one, analyze appends the highest resolution to the query and keeps
only items at that resolution. `auto_resolution.applied` says whether it
happened, `token` is what was appended, and `skipped_missing` counts titles
without a resolution when it was skipped for that reason. Pass
`"auto_resolution": false` to skip it. `/refine/apply` never auto-resolves.

How an op executes depends on the endpoint kind. `analyze` reports it as
`client_side`, and `apply` reports it as `mode`:

| Endpoint kind | `mode` | Behavior |
| --- | --- | --- |
| `nyaa` | `refetch` | The query is rebuilt and the endpoint is searched again, so a narrower query can surface older episodes beyond the first RSS page. The result replaces `items`. A failed refetch is `502 fetch_error`. |
| `rss` (Erai-raws, AniRena, any generic feed) | `client` | The query is rebuilt for display and subscription, but `items` are filtered locally. |

Ops (`op.type`):

| `type` | Fields | Effect |
| --- | --- | --- |
| `tokens` | `tokens`: non-empty list of strings, taken from one entry of `groups[].tokens` | Add those tokens to the query, positioned where they appear in release names. A leading-`[` token is a release group and goes to the front. Client mode keeps items matching all tokens. |
| `term` | `term`: string | Add a custom positive term (quoted if it has spaces). Client mode keeps items whose title (or Erai metadata) contains it. |
| `exclude` | `term`: string | Append `-term` to the query. Client mode drops items whose title contains it. |
| `best_fit` | none | Pin the top release's profile (release group, season, resolution, source, audio) per `[bestfit]` config. Use `best_fit.available` and `best_fit.query` from the last analyze/apply to preview and to enable the button. |

An unknown `type`, or `tokens` with an empty list, is `400 validation_error`.

Reason codes, set when `applied` is `false`:

| `reason` | Meaning |
| --- | --- |
| `noop` | The op would change nothing: `tokens` or `best_fit` in client mode matched nothing or everything, or best fit has no new query to offer. |
| `zero_results` | The result would be empty: a refetch returned nothing, or a client-side `exclude` would drop every item. |
| `no_match` | `term` matched none of the current items (checked before any refetch). |
| `empty_term` | `term` or `exclude` was blank. |

### Routes

Summary:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness and version (no auth). |
| GET | `/qbt/status` | qBittorrent reachability and session state. |
| GET | `/clients` | Configured qBittorrent clients. |
| GET | `/endpoints` | Configured search endpoints. |
| POST | `/endpoints` | Add an endpoint, or replace the one with its name. |
| DELETE | `/endpoints/{name}` | Remove an endpoint. |
| GET, POST | `/search` | Search. |
| POST | `/feed` | Fetch any RSS URL. |
| POST | `/enrich` | Fill in missing seeder/download stats. |
| POST | `/refine/analyze` | Groups and best fit for a result set. |
| POST | `/refine/apply` | One refine step. |
| GET | `/subscriptions` | List subscriptions. |
| POST | `/subscriptions` | Subscribe. |
| POST | `/subscriptions/preview` | What a subscription would register. |
| POST | `/subscriptions/remove` | Remove subscriptions. |
| DELETE | `/subscriptions/{name}` | Remove one subscription. |
| POST | `/downloads` | Download releases now. |
| POST | `/movie` | Download one torrent as a movie. |
| GET | `/torrents` | List qBittorrent torrents. |
| POST | `/torrents/upload` | Upload a `.torrent` file. |

Item objects are described under [Item](#item).

#### GET /health

No auth. The Host guard still applies.

```
curl $BASE/health
```
```json
{"ok": true, "version": "0.5.0", "features": ["qbt_clients", "qbt_env_session", "endpoint_writes"]}
```

`features` names what this anirss can do. Check for the one you rely on rather
than comparing `version`: a fork or a checkout can report any version, and an
anirss from before a name existed simply does not list it (one older than 0.5.0
has no `features` at all). A name keeps its meaning once listed.

| Feature | Since | Meaning |
| --- | --- | --- |
| `qbt_clients` | 0.5.0 | Several qBittorrents in `[[qbittorrent.client]]` blocks, [GET /clients](#get-clients), and `client` on every route that reaches qBittorrent. |
| `endpoint_writes` | 0.6.0 | [POST /endpoints](#post-endpoints) and [DELETE /endpoints/{name}](#delete-endpointsname), and a fresh config with no endpoints (`409 no_endpoints` until one is added). |
| `qbt_env_session` | 0.5.0 | An `ANIRSS_QBT_URL` that the config does not name keeps its own session and saved password, so an app that points anirss at another qBittorrent never sends it the configured one's. An anirss without it fails at nothing, it only misbehaves quietly, so an app that sets `ANIRSS_QBT_URL` should refuse one that does not list it. |

Every route that reaches qBittorrent (`/qbt/status`, `/subscriptions` and
`/subscriptions/remove`, `DELETE /subscriptions/{name}`, `/downloads`, `/movie`,
`/torrents`, `/torrents/upload`) takes an optional `client`: a query parameter
on GET, DELETE and `/torrents/upload`, a body field on the others. Omitted, it
is the default client. An unknown name is `404 not_found`.

#### GET /qbt/status

Checks reachability (2 second timeout) without attempting a login.
`?client=<name>` asks about a client other than the default.

```
curl -H "Authorization: Bearer $TOKEN" $BASE/qbt/status
```
```json
{"client": "default", "url": "http://localhost:8080", "reachable": true,
 "authenticated": false, "login_failed": false}
```

`authenticated` is true once this server has a live session. `login_failed`
is true after qBittorrent rejected the password. To stay clear of
qBittorrent's IP ban, a rejected password is retried at most once per
5-minute cool-down (a still-valid cached session from the CLI is picked up
sooner). An unreachable qBittorrent never trips the cool-down.

#### GET /clients

```
curl -H "Authorization: Bearer $TOKEN" $BASE/clients
```
```json
{"clients": [
  {"name": "seedbox", "url": "http://192.0.2.10:8080"},
  {"name": "local", "url": "http://localhost:8080"}
]}
```

Priority order; the first is the default. A config without
`[[qbittorrent.client]]` blocks has one, named `default`. Credentials in a URL
(`http://user:pass@host`) are masked.

#### GET /endpoints

```
curl -H "Authorization: Bearer $TOKEN" $BASE/endpoints
```
```json
{"endpoints": [
  {"name": "nyaa", "kind": "nyaa", "url": "https://nyaa.si/?page=rss",
   "client_side_refine": false},
  {"name": "erai", "kind": "rss",
   "url": "https://www.erai-raws.info/episodes/feed/?s={query}&token=***",
   "client_side_refine": true}
]}
```

First entry is the default. `kind` is `nyaa` or `rss`. `client_side_refine` is
true when refine ops on that endpoint filter items locally (see the refine
loop). Values of secret-looking query parameters in `url` (`token`, `apikey`,
`api_key`, `key`, `passkey`, `auth`, `secret`) are replaced with `***`, so the
URL is safe to display but is not usable as a feed URL. This route does not
return `category` or `filter`.

#### POST /endpoints

```
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "main", "kind": "nyaa", "url": "https://nyaa-style.example/", "category": "1_0"}' \
  $BASE/endpoints
```
```json
{"endpoint": {"name": "main", "kind": "nyaa", "url": "https://nyaa-style.example/",
              "client_side_refine": false},
 "replaced": false}
```

Body: `name` (letters, digits, `.`, `_`, `-`), `kind` (`nyaa` or `rss`), `url`
(http or https; an `rss` URL needs `{query}`), and for `nyaa` optionally
`category` and `filter`. Any other key is a `400 validation_error`. A new
endpoint answers `201` and goes after the others; one whose name is in use
replaces it in place and answers `200` with `replaced: true`. The endpoint in
the response is redacted like `GET /endpoints`. The change is written to
`config.toml` at once, keeping its comments and other sections, with the
previous file as `config.toml.bak`.

#### DELETE /endpoints/{name}

```json
{"removed": "main"}
```

`404 not_found` for an unknown name. Subscriptions on the endpoint keep running
in qBittorrent.

#### GET /search

Query parameters: `q` (required), `endpoint` (name; default: first), `fallback`
(default `true`), `enrich` (default `false`).

```
curl -G -H "Authorization: Bearer $TOKEN" $BASE/search \
  --data-urlencode "q=Frieren 1080p" --data-urlencode "enrich=true"
```
```json
{"query": "Frieren 1080p",
 "endpoint": "nyaa",
 "items": [ {Item}, ... ],
 "tried": [{"endpoint": "nyaa", "status": "ok", "count": 75, "error": null}],
 "fell_back": false}
```

`endpoint` is where the items actually came from, which differs from the
requested one when `fell_back` is true. In that case the items also carry
`source`. `tried[].status` is `ok`, `empty`, or `unreachable` (with `error`
set). Zero results everywhere is `200` with `items: []`. Only "nothing
reachable" is `502 fetch_error`. `enrich=true` can add up to about 20 seconds.

#### POST /search

Same as GET, with a JSON body, for queries awkward to put in a URL.

Body: `{"query": str (required), "endpoint": str?, "fallback": bool?, "enrich": bool?}`

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "Frieren 1080p", "endpoint": "nyaa"}' $BASE/search
```

Response: as GET.

#### POST /feed

Fetch and parse an arbitrary http(s) RSS feed.

Body: `{"url": str (required), "enrich": bool?}`

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://nyaa.si/?page=rss&q=Frieren"}' $BASE/feed
```
```json
{"items": [ {Item}, ... ]}
```

#### POST /enrich

Fill unknown `seeders`, `leechers`, `downloads` by scraping trackers. Items
that cannot be enriched come back unchanged. Never fails on tracker errors.

Body: `{"items": [Item, ...]}` (required)

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @items.json $BASE/enrich        # items.json: {"items": [...]}
```
```json
{"items": [ {Item}, ... ]}
```

#### POST /refine/analyze

Body: `{"query": str (required), "items": [Item] (required), "endpoint": str?,
"auto_resolution": bool? (default true)}`

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @state.json $BASE/refine/analyze   # {"query": "...", "items": [...], "endpoint": "nyaa"}
```
```json
{
  "endpoint": "nyaa",
  "query": "Frieren 1080p",
  "items": [ {Item}, ... ],
  "auto_resolution": {"applied": false, "token": "", "skipped_missing": 0},
  "groups": [
    {"label": "[SubsPlease]", "tokens": ["[SubsPlease]"],
     "member_count": 12, "has_poster": true, "members": [0, 1, 4]}
  ],
  "best_fit": {"available": true, "query": "[SubsPlease] Frieren 1080p",
               "item": {Item}, "profile": ["[SubsPlease]", "1080p"]},
  "client_side": false
}
```

- `query` and `items` are the state after auto-resolution. Use these, not what
  you sent.
- `groups` are tokens that split the item set into subsets (a token that all
  items share, or only one item has, is not offered). Tokens with identical
  membership are merged into one group; `label` is the tokens joined with
  `+`. Groups with a release-group poster (`[...]`) sort first, then by
  `member_count`. `members` are indexes into the returned `items`. To apply a
  group, send its `tokens` as a `tokens` op.
- `best_fit.available` is false when best fit would change nothing; `query` is
  then `""`. `item` is the release it would pick (`null` when there are no
  items). `profile` lists the facets it pins.

#### POST /refine/apply

Body: `{"query": str, "items": [Item], "endpoint": str?, "op": {"type": str,
"tokens": [str]?, "term": str?}, "enrich": bool?}` (`query`, `items`, `op` required)

`enrich: true` enriches the items after a refetch (nyaa mode only).

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "Frieren 1080p", "endpoint": "nyaa", "items": [...],
       "op": {"type": "tokens", "tokens": ["[SubsPlease]"]}}' \
  $BASE/refine/apply
```
```json
{
  "applied": true,
  "reason": null,
  "mode": "refetch",
  "query": "[SubsPlease] Frieren 1080p",
  "items": [ {Item}, ... ],
  "previous_count": 75,
  "delta": -63,
  "endpoint": "nyaa",
  "groups": [ ...same shape as analyze... ],
  "best_fit": { ...same shape as analyze... }
}
```

`delta` is `len(items) - previous_count`. `groups` and `best_fit` describe the
returned state (not the one you sent, when `applied` is true). They are
computed without auto-resolution. See [the refine loop](#the-refine-loop-stateless)
for `mode` and `reason`.

#### GET /subscriptions

Needs qBittorrent.

```
curl -H "Authorization: Bearer $TOKEN" $BASE/subscriptions
```
```json
{"subscriptions": [
  {"name": "Frieren", "feed_url": "https://nyaa.si/?page=rss&q=...",
   "save_path": "/home/me/Downloads/Anime/Frieren", "rule_enabled": true,
   "torrent_count": 28, "file_count": 28}
]}
```

`torrent_count` is how many qBittorrent torrents sit under `save_path`.
`file_count` is files on disk there, and is `0` when qBittorrent runs on
another host.

#### POST /subscriptions

Create a qBittorrent RSS feed and an auto-download rule. Two body forms.

Search form: `{"query": str (required), "endpoint": str?, "items": [Item]?,
"name": str?, "hidden": bool?}`. The query and `endpoint` determine the feed;
`items` are the refined results (used to derive the feed, filters, and default
name). If `items` is omitted, a search runs without fallback. No results gives
`404 not_found`.

Feed form: `{"feed_url": str, "name": str?, "hidden": bool?}`. If `feed_url` is
present and non-empty, this form is used and `query`/`items` are ignored. The
feed is fetched to check it has items and to derive a default name.

`name` defaults to the show name parsed from the first item. `hidden: true`
saves under `[downloads].hidden_base` instead of `save_base`.

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "[SubsPlease] Frieren 1080p", "endpoint": "nyaa", "name": "Frieren"}' \
  $BASE/subscriptions
```

Status `201`:

```json
{"name": "Frieren", "feed_url": "https://nyaa.si/?page=rss&q=...",
 "save_path": "/home/me/Downloads/Anime/Frieren",
 "must_contain": "", "must_not_contain": "", "tags": ["anirss"]}
```

`name` is the rule name actually used and can differ from the requested one
(it may gain an ` @<endpoint>` suffix).
`must_contain` / `must_not_contain` are the qBittorrent rule filters;
`must_not_contain` is `|`-joined.

#### POST /subscriptions/preview

What `POST /subscriptions` would register for a search, without touching
qBittorrent. Does not need qBittorrent credentials.

Body: `{"query": str (required), "endpoint": str?, "items": [Item]?}` (search
runs without fallback if `items` is omitted)

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "Frieren 1080p", "endpoint": "nyaa"}' $BASE/subscriptions/preview
```
```json
{"feed_url": "https://nyaa.si/?page=rss&q=Frieren+1080p",
 "must_contain": "", "must_not_contain": "", "endpoint_name": "nyaa"}
```

#### POST /subscriptions/remove

Remove subscriptions (the RSS feed and the rule).

Body: `{"names": [str] (required, non-empty), "torrents": bool?, "files": bool?,
"hidden": bool?, "dry_run": bool?}`

- `torrents`: also remove their torrents from qBittorrent.
- `files`: also delete their files on disk. File deletion is confined to
  `save_base` and `hidden_base`.
- `hidden`: look for the save path under `hidden_base`.
- `dry_run`: return the plan and change nothing.

All names are validated first; one unknown name is `404` and nothing is
removed.

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"names": ["Frieren"], "torrents": true, "dry_run": true}' \
  $BASE/subscriptions/remove
```
```json
{"dry_run": true,
 "plans": [
   {"name": "Frieren", "feed_url": "https://...", "feed_path": "Frieren",
    "save_path": "/home/me/Downloads/Anime/Frieren",
    "torrents": [ {...} ], "size_bytes": 12345678, "torrent_count_in_qb": 28}
 ]}
```

`torrents` in a plan is populated only when `torrents` or `files` was
requested. `feed_path` is `null` if the rule has no feed or the feed was not found.
`size_bytes` is the size of the affected content. Always do a `dry_run` first
when exposing `files` to end users.

#### DELETE /subscriptions/{name}

Remove one subscription. `name` is URL-encoded in the path. Options are query
parameters: `torrents`, `files`, `hidden`, `dry_run` (all booleans, default
false). Response is the same as `/subscriptions/remove`.

```
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "$BASE/subscriptions/Frieren?torrents=true&dry_run=true"
```

#### POST /downloads

Add releases to qBittorrent now (no subscription).

Body, items form: `{"items": [Item], "name": str?, "hidden": bool?}`. Links
come from `items[].link`; `name` defaults to the show name.

Body, links form: `{"links": [str], "name": str (required), "hidden": bool?}`.
Links must start with `magnet:`, `http://`, or `https://`.

If `items` is present, `links` is ignored. Files go to `<save_base>/<name>`
(`hidden_base` with `hidden`).

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"links": ["magnet:?xt=urn:btih:..."], "name": "Frieren"}' $BASE/downloads
```

Status `201`:

```json
{"count": 1, "save_path": "/home/me/Downloads/Anime/Frieren",
 "tags": ["anirss"], "hashes": ["0123abcd..."]}
```

`hashes` are the info-hashes known up front (magnets). Remote `.torrent` URLs
are fetched by qBittorrent itself, so their hashes are not listed. Use them
with `GET /torrents?hashes=` to track progress.

#### POST /movie

Add one torrent straight into `[downloads].movie_path` (no subdirectory).

Body: `{"link": str (required)}`

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"link": "magnet:?xt=urn:btih:..."}' $BASE/movie
```

Status `201`, response shaped like `/downloads` (`AddResult`).

#### GET /torrents

Query parameters (all optional): `name` (only torrents saved under
`<save_base>/<name>`, i.e. one subscription or download), `hashes`
(`|`-separated info-hashes), `hidden` (look under `hidden_base` for `name`).

```
curl -H "Authorization: Bearer $TOKEN" "$BASE/torrents?name=Frieren"
```
```json
{"torrents": [
  {"hash": "0123abcd...", "name": "[SubsPlease] Sousou no Frieren - 01 (1080p).mkv",
   "state": "stalledUP", "progress": 1.0, "size": 1468006400,
   "dlspeed": 0, "upspeed": 0, "eta": 8640000,
   "num_seeds": 0, "num_leechs": 0, "ratio": 0.4,
   "save_path": "/home/me/Downloads/Anime/Frieren",
   "content_path": "/home/me/Downloads/Anime/Frieren/[SubsPlease] ... .mkv",
   "added_on": 1760000000, "completion_on": 1760000300,
   "tags": "anirss", "category": ""}
]}
```

These are qBittorrent's own torrent fields, trimmed to exactly this set; the
values and units are qBittorrent's (`progress` 0 to 1, sizes in bytes, times
in Unix seconds, `tags` a comma-separated string). A field qBittorrent did not
report is `null`. `state` is qBittorrent's state string (`downloading`,
`stalledUP`, `pausedUP`, ...). Paths are as qBittorrent sees them.

#### POST /torrents/upload

Upload a `.torrent` file. The request body is the raw file, not JSON.
Content-Type must be `application/x-bittorrent` or `application/octet-stream`.
Max 16 MiB. Options are query parameters: `file_name` (default `file.torrent`),
`name` (default: file name without extension), `hidden`.

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/x-bittorrent" \
  --data-binary @show.torrent "$BASE/torrents/upload?name=Frieren"
```

Status `201`, response shaped like `/downloads` (`AddResult`).

## Data shapes

### Item

One release from a feed. JSON object with exactly these keys:

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string | Release title. |
| `link` | string | Magnet or `.torrent` URL. What gets downloaded. |
| `seeders` | int or null | `null` means the feed did not carry it (unknown). It is not zero. |
| `leechers` | int or null | Same. |
| `downloads` | int or null | Same. `POST /enrich` may fill these. |
| `size` | string | As the feed reports it (for example `"1.4 GiB"`), `""` if absent. |
| `category` | string | `""` if absent. |
| `page` | string | Release post URL when the feed exposes one (Erai), else `""`. |
| `source` | string | Endpoint name when a search fell back to another endpoint, else `""` (meaning the endpoint you asked for). |
| `resolution` | string | Erai metadata (`"1080p"`), `""` for other feeds. |
| `infomkv` | string | Erai metadata (`"NF WEB-DL AVC AAC"`), `""` otherwise. |
| `linktype` | string | Erai metadata (`"Torrent"`), `""` otherwise. |

When you send items back to the server, `title` and `link` are required
strings; the integer fields must be integers or `null`; the other fields must
be strings if present; missing or `null` fields take their defaults. Round-trip
the objects you received and you are fine. Items are matched by position, so
keep their order (`groups[].members` are indexes).

### Other result objects

| Object | Fields |
| --- | --- |
| `Endpoint` (Python) | `name`, `kind` (`"nyaa"` or `"rss"`), `url`, `category`, `filter` (unredacted `url`) |
| endpoint info (HTTP `/endpoints`, Python `endpoint_info`) | `name`, `kind`, `url` (redacted), `client_side_refine` |
| `SubscriptionSpec` | `feed_url`, `must_contain`, `must_not_contain`, `endpoint_name` |
| `SubscribeResult` | `name`, `feed_url`, `save_path`, `must_contain`, `must_not_contain`, `tags` |
| `AddResult` | `count`, `save_path`, `tags`, `hashes` |
| `RuleInfo` | `name`, `feed_url`, `save_path`, `rule_enabled`, `torrent_count`, `file_count` |
| `RemovePlan` | `name`, `feed_url`, `feed_path`, `save_path`, `torrents`, `size_bytes`, `torrent_count_in_qb` |
| `RefineOutcome` (Python) | `applied`, `query`, `items`, `mode`, `previous_count`, `reason`, property `delta` |
| `SearchResult` (Python) | `query`, `endpoint`, `items`, `tried`, property `fell_back` |

In Python these are NamedTuples or dataclasses; over HTTP they are the JSON
objects above (the HTTP `refine/apply` response adds `groups`, `best_fit`, and
`endpoint`; the HTTP `search` response adds `fell_back`).

### Paths

All filesystem paths in results (`save_path`, `content_path`) are as
qBittorrent sees them. (`feed_path` in a `RemovePlan` is a qBittorrent RSS feed
path, not a filesystem path.)
If qBittorrent runs in a container or on another host, those paths mean
nothing on your machine; a media app needs to map them (for example by a
configured prefix) before opening files.

## Stability

Only these are the supported surface:

- `anirss_lib/api.py` (the `Anirss` class and the helpers in its `__all__`).
- `anirss_lib/errors.py`.
- The types `api.py` returns: `Item`, `Endpoint`, `SubscriptionSpec`,
  `SubscribeResult`, `AddResult`, `RuleInfo`, `RemovePlan`, `RefineOutcome`,
  `SearchResult`.
- The HTTP routes under `/api/v1` documented above.

Everything else in `anirss_lib` is internal to the CLI and can change without
notice. Do not import it.
