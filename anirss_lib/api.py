"""anirss as a library: the stable, UI-free entry point.

    from anirss_lib.api import Anirss

    ani = Anirss()                                   # ~/.config/anirss/config.toml
    res = ani.search("Frieren 1080p")
    sub = ani.subscribe("Frieren 1080p", items=res.items)
    print(sub.name, sub.feed_url)

Everything here returns plain data (NamedTuples / dataclasses; to_jsonable()
turns any result into JSON-ready dicts) and raises anirss_lib.errors.AnirssError
subclasses instead of printing or exiting. It never prompts, never starts
qBittorrent unless asked (autostart_qbt=True), and is safe to share across
threads: the HTTP server (anirss_lib.server) is one instance of this class.

Only this module, errors.py, and the types it returns are the supported
surface; everything else in anirss_lib is internal to the CLI.
"""

import dataclasses
import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, NamedTuple, TypeVar

import anirss_lib
from anirss_lib import endpoints as endpoints_mod
from anirss_lib import refinement
from anirss_lib import stats as stats_mod
from anirss_lib.config import AnirssConfig, QbtConfig, build_config, load_config
from anirss_lib.endpoints import Endpoint, SubscriptionSpec, load_endpoints
from anirss_lib.errors import (
    AnirssError,
    FetchError,
    NotFoundError,
    QbtAuthError,
    ValidationError,
)
from anirss_lib.nyaa import fetch_rss
from anirss_lib.qbt import actions
from anirss_lib.qbt.actions import (
    AddResult,
    RemovePlan,
    RuleInfo,
    SubscribeResult,
    validate_name,
)
from anirss_lib.qbt.clients import qbt_clients
from anirss_lib.qbt.session import QbtSession, _qbt_reachable, login_with_password
from anirss_lib.refinement import RefineOutcome
from anirss_lib.titles import show_name
from anirss_lib.types import Item

__all__ = [
    "AddResult", "Anirss", "AnirssError", "Endpoint", "Item", "RefineOutcome",
    "RemovePlan", "RuleInfo", "SearchResult", "SubscribeResult",
    "SubscriptionSpec", "client_info", "endpoint_info", "item_from_dict",
    "redact_url", "to_jsonable",
]

T = TypeVar("T")

# enrich() fans out 8 download threads and can block ~20s; a server must not
# let every concurrent request do that at once.
_ENRICH_SLOTS = threading.BoundedSemaphore(2)

LINK_SCHEMES = ("magnet:", "http://", "https://")
# How long download() spends fetching .torrent files before handing the rest
# to qB as URLs. AniBeam waits 60s for the whole call.
FETCH_DEADLINE_S = 20.0
LOGIN_COOLDOWN_S = 300


class SearchResult(NamedTuple):
    query: str
    endpoint: str              # where the items came from
    items: list[Item]
    # One entry per endpoint consulted, in order:
    # {"endpoint", "status": "ok"|"empty"|"unreachable", "count", "error"}
    tried: list[dict]

    @property
    def fell_back(self) -> bool:
        return len(self.tried) > 1 and bool(self.items)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert results (NamedTuples, dataclasses, lists, dicts)
    to JSON-serializable builtins."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name))
                for f in dataclasses.fields(obj)}
    if isinstance(obj, tuple) and hasattr(obj, "_asdict"):
        return {k: to_jsonable(v) for k, v in obj._asdict().items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


_ITEM_FIELDS = Item._fields
_INT_FIELDS = ("seeders", "leechers", "downloads")


def item_from_dict(d: dict) -> Item:
    """Rebuild an Item from its to_jsonable() form (unknown keys ignored)."""
    if not isinstance(d, dict) or not isinstance(d.get("title"), str) \
            or not isinstance(d.get("link"), str):
        raise ValidationError("each item needs string `title` and `link`")
    kw: dict[str, Any] = {}
    for k in _ITEM_FIELDS:
        if k not in d or d[k] is None:
            continue
        v = d[k]
        if k in _INT_FIELDS:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValidationError(f"item `{k}` must be an integer or null")
        elif not isinstance(v, str):
            raise ValidationError(f"item `{k}` must be a string")
        kw[k] = v
    return Item(**kw)


_SECRET_PARAMS = ("token", "apikey", "api_key", "key", "passkey", "auth", "secret",
                  "pass", "password", "pwd", "rsskey", "uid", "sid", "session")


def redact_url(url: str) -> str:
    """`url` with secret-looking query values (token=, apikey=, …) masked,
    for showing endpoint config to a frontend."""
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"***@{host}" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    if not parts.query:
        return urllib.parse.urlunsplit(parts)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in _SECRET_PARAMS and v else v) for k, v in pairs]
    query = urllib.parse.urlencode(masked, safe="{}*")
    return urllib.parse.urlunsplit(parts._replace(query=query))


def endpoint_info(ep: Endpoint) -> dict:
    """Display-safe description of an endpoint (secrets in its URL masked)."""
    return {"name": ep.name, "kind": ep.kind, "url": redact_url(ep.url),
            "client_side_refine": refinement.is_client_side(ep)}


def client_info(client: QbtConfig) -> dict:
    """Display-safe description of a qBittorrent client (credentials in its
    URL masked)."""
    return {"name": client.get("name", ""), "url": redact_url(client["url"])}


@dataclasses.dataclass
class _QbtState:
    """One client's login: the password given for it, the live session, and
    when a password was last rejected."""
    password: str | None = None
    session: QbtSession | None = None
    failed_at: float | None = None


def _check_link(link: object) -> str | None:
    """The link as qB should get it, or None when it can't be sent."""
    if (not isinstance(link, str) or not link.startswith(LINK_SCHEMES)
            or any(ord(c) < 32 or ord(c) == 127 for c in link)):
        return None
    if link.startswith(("http://", "https://")):
        try:
            link = stats_mod.encode_http_link(link)
        except ValueError:              # malformed host, e.g. an unclosed '['
            return None
    # qB takes `urls` newline-separated, so whitespace left inside one link
    # (a magnet, or a host) would smuggle in a second, unchecked one.
    return None if any(c.isspace() for c in link) else link


def _check_links(links: list[str]) -> list[str]:
    if not links:
        raise ValidationError("no links given")
    checked = [_check_link(link) for link in links]
    if None in checked:
        bad = links[checked.index(None)]
        raise ValidationError(f"links must be magnet: or http(s) URLs, got {bad!r}")
    return checked


class Anirss:
    """One configured anirss instance.

    config        a full or partial config dict (config.toml shape); merged
                  over the defaults with no disk access. Omit to read
                  `config_path` (default ~/.config/anirss/config.toml).
    qbt_password  the default qBittorrent client's WebUI password. Falls back
                  to the ANIRSS_QBT_PASSWORD env var, then the password the CLI
                  saved. Any other client logs in with the password the CLI
                  saved for it, or set_qbt_password(..., client=name).
    autostart_qbt start a local qBittorrent when it isn't running (CLI
                  behavior). Off by default: a library shouldn't spawn it.
    persist_session  write the qB session cookie to the shared SID cache the
                  CLI uses (default False: the cache is only read).
    """

    def __init__(self, config: dict | None = None, *,
                 config_path: str | os.PathLike | None = None,
                 qbt_password: str | None = None,
                 autostart_qbt: bool = False,
                 persist_session: bool = False) -> None:
        if config is not None:
            self.config: AnirssConfig = build_config(config)
        else:
            self.config = load_config(config_path, bootstrap=False, strict=True)
        self._endpoints = load_endpoints(self.config)
        self._clients = qbt_clients(self.config)
        self._qbt_state = {c["name"]: _QbtState() for c in self._clients}
        self._qbt_state[self._clients[0]["name"]].password = qbt_password
        self._autostart = autostart_qbt
        self._persist = persist_session
        self._qbt_lock = threading.RLock()

    # ---- endpoints / search -------------------------------------------

    @property
    def version(self) -> str:
        return anirss_lib.__version__

    def endpoints(self) -> list[Endpoint]:
        """Configured search endpoints in priority order (first = default)."""
        return list(self._endpoints)

    def endpoint(self, name: str | None = None) -> Endpoint:
        if name is None:
            return self._endpoints[0]
        for ep in self._endpoints:
            if ep.name == name:
                return ep
        names = ", ".join(e.name for e in self._endpoints)
        raise NotFoundError(f"unknown endpoint {name!r} (configured: {names})")

    def search(self, query: str, endpoint: str | None = None, *,
               fallback: bool = True, enrich: bool = False) -> SearchResult:
        """Search one endpoint; when it is empty or unreachable and
        `fallback` is on, try the others in priority order (first hit wins,
        its items carry `source`). Raises FetchError only when nothing was
        reachable at all; zero results everywhere is an empty SearchResult."""
        query = (query or "").strip()
        if not query:
            raise ValidationError("empty query")
        active = self.endpoint(endpoint)
        order = [active] + ([e for e in self._endpoints if e is not active]
                            if fallback else [])
        tried: list[dict] = []
        errors: list[str] = []
        for i, ep in enumerate(order):
            try:
                items = endpoints_mod.fetch_items(ep, query)
            except FetchError as e:
                tried.append({"endpoint": ep.name, "status": "unreachable",
                              "count": 0, "error": str(e)})
                errors.append(str(e))
                continue
            tried.append({"endpoint": ep.name, "status": "ok" if items else "empty",
                          "count": len(items), "error": None})
            if items:
                if i > 0:
                    items = [it._replace(source=ep.name) for it in items]
                if enrich:
                    items = self.enrich(items)
                return SearchResult(query, ep.name, items, tried)
        if len(errors) == len(order):
            raise FetchError("; ".join(errors))
        return SearchResult(query, active.name, [], tried)

    def fetch_feed(self, url: str, *, enrich: bool = False) -> list[Item]:
        """Fetch and parse any RSS feed URL (what `anirss -S <url>` lists)."""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValidationError("feed url must be http(s)")
        items = fetch_rss(url)
        return self.enrich(items) if enrich else items

    def enrich(self, items: list[Item], *, deadline_s: float = 20.0) -> list[Item]:
        """Fill unknown seeders/leechers/downloads by scraping the torrents'
        trackers (cached). Never raises."""
        with _ENRICH_SLOTS:
            return stats_mod.enrich(items, deadline_s=deadline_s)

    # ---- refine ----------------------------------------------------------

    def analyze(self, query: str, items: list[Item], endpoint: str | None = None,
                *, auto_resolution: bool = True) -> dict:
        """Everything the TUI's refine screen shows for (query, items): the
        auto-resolution step, the token groups to narrow by (with the item
        indexes each selects), and what best fit would do."""
        ep = self.endpoint(endpoint)
        auto = (refinement.auto_resolution(query.strip(), items) if auto_resolution
                else refinement.AutoResolution(query.strip(), items))
        groups = refinement.compute_groups(auto.items)
        return {
            "endpoint": ep.name,
            "query": auto.query,
            "items": auto.items,
            "auto_resolution": {"applied": bool(auto.token), "token": auto.token,
                                "skipped_missing": auto.skipped_missing},
            "groups": [{**g._asdict(),
                        "members": refinement.group_members(auto.items, g)}
                       for g in groups],
            "best_fit": refinement.best_fit_info(auto.query, auto.items,
                                                 self.config["bestfit"]),
            "client_side": refinement.is_client_side(ep),
        }

    def refine(self, query: str, items: list[Item], op: str, *,
               endpoint: str | None = None, tokens: list[str] | None = None,
               term: str = "", enrich: bool = False) -> RefineOutcome:
        """One refine step (op: tokens | term | exclude | best_fit); see
        refinement.apply. The caller keeps the history for "back"."""
        return refinement.apply(
            self.endpoint(endpoint), query, items, op, tokens=tokens, term=term,
            bestfit_cfg=self.config["bestfit"],
            enrich=self.enrich if enrich else None)

    def best_item(self, items: list[Item]) -> Item | None:
        """The release best fit would pick (config [bestfit] ranking)."""
        from anirss_lib import bestfit
        return bestfit.best_item(items, self.config["bestfit"])

    def top(self, items: list[Item], n: int = 1) -> list[Item]:
        """Top-n by downloads (unknown counts rank last); enrich() first if
        the feed carries no stats."""
        return stats_mod.top_by_downloads(items, n)

    # ---- qBittorrent clients and sessions ---------------------------------

    def clients(self) -> list[QbtConfig]:
        """Configured qBittorrent clients in priority order (first = default)."""
        return list(self._clients)

    def client(self, name: str | None = None) -> QbtConfig:
        """The client called `name`, or the default one. NotFoundError for an
        unknown name."""
        if not name:
            return self._clients[0]
        for c in self._clients:
            if c["name"] == name:
                return c
        names = ", ".join(c["name"] for c in self._clients)
        raise NotFoundError(f"unknown qBittorrent client {name!r} (configured: {names})")

    def set_qbt_password(self, password: str | None, *, client: str | None = None) -> None:
        """Swap a client's credentials (clears a previous login failure)."""
        name = self.client(client)["name"]
        with self._qbt_lock:
            self._qbt_state[name] = _QbtState(password=password)

    def qbt_status(self, client: str | None = None) -> dict:
        """Reachability and session state, without attempting a login."""
        c = self.client(client)
        state = self._qbt_state[c["name"]]
        url = c["url"]
        return {"client": c["name"], "url": url,
                "reachable": _qbt_reachable(url, timeout=2.0),
                "authenticated": state.session is not None,
                "login_failed": state.failed_at is not None}

    def _login(self, client: QbtConfig) -> QbtSession:
        state = self._qbt_state[client["name"]]
        password = state.password
        if password is None:
            # The environment names the default client's password only: each
            # client is its own server, and one's password is no business of
            # another's.
            default = client["name"] == self._clients[0]["name"]
            password = os.environ.get("ANIRSS_QBT_PASSWORD", "") if default else ""
        failed_at = state.failed_at
        if failed_at is not None and time.monotonic() - failed_at < LOGIN_COOLDOWN_S:
            # A rejected password is not retried right away: qBittorrent bans
            # an IP after a handful of failures. A still-valid cached session
            # (e.g. the CLI logged in meanwhile) or set_qbt_password() gets us
            # back in sooner; otherwise one attempt per cool-down.
            from anirss_lib.qbt.session import _try_qbt_sid
            sess = _try_qbt_sid(client["url"], persist=False, client=client["name"])
            if sess is None:
                raise QbtAuthError("qBittorrent rejected the password; retrying "
                                   f"after a {LOGIN_COOLDOWN_S // 60}-minute cool-down "
                                   "(or call set_qbt_password())", rejected=True)
            state.failed_at = None
            return sess
        try:
            sess = login_with_password(client, password,
                                       autostart=self._autostart, notify=None,
                                       persist=self._persist)
        except QbtAuthError as e:
            if e.rejected:
                state.failed_at = time.monotonic()
            raise
        state.failed_at = None
        return sess

    def _with_qbt(self, fn: Callable[[QbtSession], T], client: str | None = None) -> T:
        """Run fn(session) against a client under the qB lock (rule naming is
        check-then-act). An expired session (401/403) is re-established once
        and retried."""
        c = self.client(client)
        with self._qbt_lock:
            state = self._qbt_state[c["name"]]
            if state.session is None:
                state.session = self._login(c)
            try:
                return fn(state.session)
            except QbtAuthError:
                state.session = None
                state.session = self._login(c)
                return fn(state.session)

    # ---- actions -----------------------------------------------------------

    def _save_base(self, hidden: bool) -> str:
        d = self.config["downloads"]
        return d["hidden_base"] if hidden else d["save_base"]

    def _tags(self, client: str | None = None) -> list[str]:
        return list(self.client(client).get("tags") or [])

    def _name_for(self, name: str | None, items: list[Item], fallback: str) -> str:
        if name is None or not name.strip():
            name = (show_name(items[0].title) if items else "") or fallback or "anirss"
        return validate_name(name)

    def subscription_spec(self, query: str, items: list[Item],
                          endpoint: str | None = None) -> SubscriptionSpec:
        """What subscribe() would register, without touching qBittorrent."""
        return endpoints_mod.build_subscription(self.endpoint(endpoint), query, items)

    def subscribe(self, query: str, *, endpoint: str | None = None,
                  items: list[Item] | None = None, name: str | None = None,
                  hidden: bool = False, client: str | None = None) -> SubscribeResult:
        """Subscribe to a search: a qB RSS feed + auto-download rule saving
        under save_base/<name> (hidden_base with hidden=True). `items` are
        the (refined) results the search returned; fetched when omitted.
        `name` defaults to the show name parsed from the first result."""
        query = (query or "").strip()
        if not query:
            raise ValidationError("empty query")
        if items is None:
            items = self.search(query, endpoint, fallback=False).items
        if not items:
            raise NotFoundError(f"no results for {query!r}")
        name = self._name_for(name, items, query)
        spec = self.subscription_spec(query, items, endpoint)
        return self._with_qbt(lambda q: actions.subscribe_rule(
            q, spec.feed_url, name, self._save_base(hidden),
            endpoint_name=spec.endpoint_name,
            must_not_contain=spec.must_not_contain,
            must_contain=spec.must_contain, tags=self._tags(client)), client)

    def subscribe_feed(self, url: str, *, name: str | None = None,
                       hidden: bool = False, client: str | None = None) -> SubscribeResult:
        """Subscribe to a raw RSS feed URL as-is (`anirss -S <url>`)."""
        items = self.fetch_feed(url)
        if not items:
            raise NotFoundError(f"no items returned by {url}")
        name = self._name_for(name, items, "anirss")
        return self._with_qbt(lambda q: actions.subscribe_rule(
            q, url, name, self._save_base(hidden), tags=self._tags(client)), client)

    def download(self, links: list[str], *, name: str, hidden: bool = False,
                 client: str | None = None) -> AddResult:
        """Add magnet / http(s) .torrent links now, into save_base/<name>."""
        links = _check_links(links)
        save_path = os.path.join(self._save_base(hidden), validate_name(name))
        return self._add_links(links, save_path, client)

    def download_items(self, items: list[Item], *, name: str | None = None,
                       hidden: bool = False, client: str | None = None) -> AddResult:
        """download() for search results; `name` defaults to the show name."""
        if not items:
            raise ValidationError("no items given")
        return self.download([it.link for it in items],
                             name=self._name_for(name, items, "anirss"),
                             hidden=hidden, client=client)

    def movie(self, link: str, *, client: str | None = None) -> AddResult:
        """Add one torrent straight into [downloads].movie_path (no subdir)."""
        links = _check_links([link])
        return self._add_links(links, self.config["downloads"]["movie_path"], client)

    def _add_links(self, links: list[str], save_path: str,
                   client: str | None = None) -> AddResult:
        # qB fetches every `urls=` link at once over one HTTP/2 connection,
        # and a host that caps concurrent streams (t.erai-raws.info allows
        # 8) refuses the rest. So anirss fetches the .torrent files itself,
        # outside the qB lock, and uploads them; a link it can't fetch still
        # goes to qB as a URL.
        torrents = stats_mod.fetch_torrents(
            [link for link in links if link.startswith(("http://", "https://"))],
            deadline_s=FETCH_DEADLINE_S)
        return self._with_qbt(lambda q: actions.add_links(
            q, links, save_path, self._tags(client), torrents=torrents), client)

    def upload_torrent(self, data: bytes, *, file_name: str = "file.torrent",
                       name: str | None = None, hidden: bool = False,
                       client: str | None = None) -> AddResult:
        """Add a .torrent from its bytes (works with a remote qBittorrent)."""
        file_name = os.path.basename(file_name or "") or "file.torrent"
        if any(c in '"\\' or ord(c) < 32 or ord(c) == 127 for c in file_name):
            raise ValidationError(f"invalid file_name {file_name!r}")
        name = validate_name(name or os.path.splitext(file_name)[0] or "anirss")
        save_path = os.path.join(self._save_base(hidden), name)
        return self._with_qbt(lambda q: actions.add_torrent_bytes(
            q, data, file_name, save_path, self._tags(client)), client)

    # ---- subscriptions / torrents --------------------------------------------

    def subscriptions(self, *, client: str | None = None) -> list[RuleInfo]:
        """Every qB RSS rule with torrent/file counts (`anirss -Qj`)."""
        return self._with_qbt(actions.list_subscriptions, client)

    def remove(self, names: list[str], *, drop_torrents: bool = False,
               drop_files: bool = False, hidden: bool = False,
               dry_run: bool = False, client: str | None = None) -> list[RemovePlan]:
        """Remove subscriptions (feed + rule); optionally their torrents
        (drop_torrents) and files on disk (drop_files). Returns the plan;
        with dry_run nothing is changed. File deletion is confined to the
        configured download roots."""
        names = list(dict.fromkeys(names))  # a repeat would fail half-way through
        if not names:
            raise ValidationError("no subscription names given")
        d = self.config["downloads"]

        def run(q: QbtSession) -> list[RemovePlan]:
            plans = actions.plan_remove(q, names, drop_torrents=drop_torrents,
                                        drop_files=drop_files,
                                        save_base=self._save_base(hidden))
            roots = [d["save_base"], d["hidden_base"]]
            if drop_files:
                actions.check_confined(plans, roots)  # dry runs refuse too
            if not dry_run:
                actions.execute_remove(
                    q, plans, drop_torrents=drop_torrents, drop_files=drop_files,
                    confine_to=roots)
            return plans
        return self._with_qbt(run, client)

    def torrents(self, *, name: str | None = None, hashes: list[str] | None = None,
                 hidden: bool = False, client: str | None = None) -> list[dict]:
        """qBittorrent's torrents (state, progress, content_path, …), all of
        them or those of one subscription/download `name`, or by info-hash."""
        save_path = (os.path.join(self._save_base(hidden), validate_name(name))
                     if name else None)
        return self._with_qbt(lambda q: actions.list_torrents(
            q, save_path=save_path, hashes=hashes), client)
