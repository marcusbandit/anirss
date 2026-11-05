> **Agent documentation.** Everything in `docs/agents/` is written mainly for
> coding agents implementing against anirss: a frontend, an app that embeds it,
> or a change to the backend itself. If anirss grows a wider audience,
> human-facing documentation will be hosted on a proper docs site rather than
> spread across repo files; until then, these files are the source of truth.

# Implementing against anirss

anirss searches anime torrent RSS endpoints (nyaa, Erai-raws, any RSS search
URL), narrows the results, and drives qBittorrent: subscriptions (RSS feed plus
auto-download rule), one-off downloads, movies, `.torrent` uploads. The TUI is
one client of a UI-free core; this guide is for building other clients.

Two files:

- **This file**: pick a branch, follow its steps to the done-state, obey the
  rules.
- **[api-reference.md](api-reference.md)**: every method, route, parameter,
  and data shape. Look things up there; this file links the exact section.

## Pick a branch

| The task | Branch |
| --- | --- |
| A frontend in anything other than Python (web, Electron, mobile, another service) | [A: HTTP client](#a-http-client) |
| A Python app that uses anirss as its download backend (media streamer, bot, script) | [B: Python library](#b-python-library) |
| New or changed backend capability (a route, an operation, an endpoint kind) | [C: Extending the backend](#c-extending-the-backend) |

A frontend that needs a capability the API lacks takes branch C first, then
returns to A.

## Rules every branch obeys

1. **The caller owns state.** anirss keeps no search session. The client holds
   `{endpoint, query, items}`, sends it back with every refine and subscribe
   call, and keeps its own history stack for "back".
2. **The stable surface is narrow:** `anirss_lib.api`, `anirss_lib.errors`,
   the types those return, and the `/api/v1` routes. Every other module is
   CLI internals that may change without notice; import nothing else.
3. **Unknown is `null`, never `0`.** Erai and most generic RSS feeds carry no
   seeders/downloads. Show `null` as unknown. Before ranking by downloads
   (top-N, movie pick, best fit), call `enrich` (`POST /enrich`,
   `Anirss.enrich`) so the ranking uses real tracker counts.
4. **Paths belong to qBittorrent's host.** `save_path` and `content_path` are
   as qBittorrent sees them; a media app maps them (configured prefix) before
   opening files. See [Paths](api-reference.md#paths).
5. **A name is one path component.** Subscription and download names become a
   directory under `save_base`. anirss rejects `/`, `\`, `.`, `..`, and control
   characters with `validation_error`; surface that message to the user as-is.
6. **Two auth layers, two errors.** `401 unauthorized` means the API bearer
   token is wrong. `503 qbt_auth_required` means qBittorrent credentials are
   missing or rejected. After a rejection anirss waits out a 5-minute
   cool-down before trying the password again (qBittorrent IP-bans repeated
   failures), so show the user "fix the qBittorrent password" and stop
   retrying.
7. **Secrets stay server-side.** Endpoint URLs carry tokens and API keys.
   Display `GET /endpoints` / `api.endpoint_info()` (secrets masked as `***`);
   `Anirss.endpoints()` returns raw URLs for internal use only.
8. **Branch on error type, never on message text.** The `error.type` values
   and their HTTP statuses are fixed; messages are for humans. Table:
   [Error envelope and status mapping](api-reference.md#error-envelope-and-status-mapping).

## A: HTTP client

The known clients are Rust (an app using anirss as its download backend, and
the planned anirss frontend), so write serde types from
[Data shapes](api-reference.md#data-shapes) and treat every stats field as
`Option<_>` (rule 3). When the client launches the server itself as a
sidecar, choose the port and token on the client side instead of parsing the
startup banner (it is colored human text): spawn
`anirss --serve --port <chosen>` with `ANIRSS_API_TOKEN=<generated>` in its
environment, then poll `GET /api/v1/health` until it returns 200.

1. **Run the server.** Get the command with
   `uv tool install git+https://github.com/marcusbandit/anirss` (or any
   existing install), then `anirss --serve` (default `127.0.0.1:8787`; `--host`,
   `--port`, or config `[server]`). For development, set `ANIRSS_API_TOKEN` so
   the token is stable, and add the dev server's origin to
   `[server].cors_origins` for browser clients. qBittorrent's password comes
   from `ANIRSS_QBT_PASSWORD` or the one the CLI saved. Done when
   `GET /api/v1/health` returns 200 and `GET /api/v1/endpoints` with the
   bearer token returns 200. A fresh anirss has no endpoints: let the user
   paste an endpoint repo URL (`POST /repos`, show its fingerprint, then
   `POST /repos/trust`) and install from it (`GET /repos/endpoints`,
   `POST /repos/install`), or add one by hand (`POST /endpoints`). Details:
   [Endpoint repos](api-reference.md#endpoint-repos),
   [Starting the server](api-reference.md#starting-the-server),
   [Authentication](api-reference.md#authentication-and-token-resolution),
   [CORS](api-reference.md#cors).
2. **Search.** `GET /search?q=…&endpoint=…`. Fallback to the other endpoints
   is on by default; when `fell_back` is true, the results came from
   `endpoint`, not the one asked for, and `tried` says why (show it, as the TUI
   does). Store `{endpoint, query, items}` as the current state. Done when an
   empty result and an unreachable endpoint (`502 fetch_error`) each render a
   distinct message.
3. **Refine.** Call `POST /refine/analyze` once with the search state; it may
   append a resolution token (`auto_resolution`), so adopt its `query` and
   `items` as the new state. Render `groups` (`label`, `member_count`;
   `members` are item indexes to highlight) and a best-fit control enabled by
   `best_fit.available`. Each user action is one `POST /refine/apply` with the
   current state and an `op`: `tokens` (a group's `tokens`, echoed back
   verbatim), `term`, `exclude`, or `best_fit`. Push the current state onto
   the history stack, then adopt the response; when `applied` is false the
   state is unchanged and `reason` (`noop`, `zero_results`, `no_match`,
   `empty_term`) is the message to show. The response already carries the
   next screen's `groups` and `best_fit`. "Back" pops the stack; switching
   endpoint is a fresh `/search` with `fallback=0`. Done when all four ops,
   back, and endpoint switch work against both a nyaa endpoint (refetches,
   `mode: "refetch"`) and an rss one (filters locally, `mode: "client"`).
   Contract: [The refine loop](api-reference.md#the-refine-loop-stateless).
4. **Act.** Subscribe with `POST /subscriptions {query, endpoint, items, name?,
   hidden?}`; call `POST /subscriptions/preview` first to show the feed URL and
   filters the rule will use, and display the returned `name` (it may gain an
   ` @endpoint` suffix). Download with `POST /downloads {items, name?}` (name defaults to the
   show name) or `{links, name}` (name required),
   a single movie with `POST /movie {link}`, a file with `POST /torrents/upload`
   (raw `application/x-bittorrent` body). `hidden: true` targets
   `[downloads].hidden_base`. Done when each action returns 201 and the user
   sees the resulting name and save path.
5. **Library view.** `GET /subscriptions` lists rules with torrent/file counts;
   `GET /torrents?name=…` gives state, progress, and `content_path` (or
   `?hashes=` with `AddResult.hashes`). Remove in two calls:
   `DELETE /subscriptions/{name}?dry_run=1` (plus `torrents=1`, `files=1` as
   chosen), show the returned plan, and only on confirmation repeat without
   `dry_run`. Done when a remove can never happen without the user seeing its
   plan.

**Branch done-state:** every row of the CLI's feature set is reachable from
the UI (search, the four refine ops, back, endpoint switch, subscribe,
download, movie, upload, subscription list, torrent progress, remove), and
every `error.type` in the mapping table has a handled path.

## B: Python library

1. **Install.** `uv add git+https://github.com/marcusbandit/anirss`
   (Python 3.11+, no runtime dependencies), or put a checkout on `sys.path`.
   Done when `from anirss_lib.api import Anirss` imports.
2. **Construct one instance and share it.** `Anirss(config={...})` takes the
   `config.toml` shape as a dict, merged over defaults, with no disk access:
   use it when your app owns its settings. `Anirss()` reads
   `~/.config/anirss/config.toml` instead. Pass `qbt_password=` or rely on
   `ANIRSS_QBT_PASSWORD`. The instance is thread-safe: qBittorrent calls queue
   behind one lock, `enrich` runs at most two at a time.
   [Constructor](api-reference.md#constructor),
   [Thread safety](api-reference.md#thread-safety).
3. **Call the flow.** `search` → optional `analyze`/`refine` → `subscribe`,
   `download_items`, `movie`, or `upload_torrent` → `torrents(name=…)` to track
   progress. Every return value is a NamedTuple or dataclass;
   `to_jsonable()` turns any of them into plain dicts.
   [Quickstart](api-reference.md#quickstart),
   [Methods](api-reference.md#methods).
4. **Handle errors by class.** Catch `anirss_lib.errors.AnirssError`
   subclasses; `QbtAuthError.rejected` is true when qBittorrent refused the
   password (the cool-down from rule 6 applies; `set_qbt_password()` clears
   it). [Error classes](api-reference.md#error-classes).
5. **Route the logs.** anirss logs to the stdlib logger `"anirss"` (silent
   until you configure it) and never writes to stdout or stdin.

**Branch done-state:** your app drives search to download with anirss
writing nothing to the terminal, and every `AnirssError` subclass it can hit
is caught by class.

## C: Extending the backend

The core is layered; each capability touches the layers top to bottom:

| Layer | Where | Contract |
| --- | --- | --- |
| Core logic | `endpoints.py`, `refinement.py`, `qbt/actions.py`, `stats.py` | Silent. Returns NamedTuples/dataclasses. Raises `AnirssError` subclasses. |
| Facade | `api.py` (`Anirss`) | Validates caller input, picks config, runs qBittorrent work through `_with_qbt`. |
| HTTP | `server.py` | Route table entry plus an `h_*` handler: parse, call one facade method, `to_jsonable`. |
| CLI | `main.py`, `cli/`, `qbt/actions.py` `do_*` | Thin printing wrappers over the core. The only place `die()`, `print`, prompts, and fzf live. |

1. **Core.** Add the logic as a silent function in the module that owns the
   domain. Error conditions raise the matching class from `errors.py` (a new
   class also needs a row in `server._ERRORS`). Done when a unit test drives
   it with a fake `QbtSession` or a monkeypatched `fetch_items`, with no
   network.
2. **Facade.** Add an `Anirss` method. Caller strings that become paths go
   through `validate_name`; torrent links through `_check_links`; anything
   that talks to qBittorrent runs inside `self._with_qbt(...)` so it gets the
   lock and the one-shot re-login. Done when `tests/test_api.py` covers the
   success path and each `ValidationError`.
3. **HTTP.** Add the route to `Handler._route`'s table and an `h_*` handler
   that only parses and delegates. Request bodies are JSON objects read with
   `_json_body`; item lists go through `_items` (capped at `MAX_ITEMS`).
   Done when `tests/test_server.py` exercises the route over a real socket,
   including auth and one error status.
4. **CLI (when the TUI should offer it too).** Wrap the core function and
   print there.
5. **Docs.** Add the method or route to [api-reference.md](api-reference.md)
   in the same shape as its neighbours, and update the rules above if the
   change alters one. Done when every new parameter and response field
   appears in the reference.
6. **Build gate.** Run the three checks in the repo's `AGENTS.md`: tests all
   green, ruff with no new findings against the baseline, `./anirss --version`
   runs. This is the branch's done-state.

Existing tests to pattern-match: `tests/test_api.py` (facade, fake
qBittorrent, refine parity with the TUI), `tests/test_server.py` (HTTP over a
loopback socket), `tests/test_backend_hardening.py` (security regressions).
