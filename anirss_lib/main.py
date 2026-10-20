"""Top-level dispatch: argv parsing, state machine, action dispatch."""

import json
import os
import sys

import anirss_lib
from anirss_lib import erai, ondemand, proctitle, responsive, terminal
from anirss_lib.ansi import (
    C_BLD, C_DIM, C_GRN, C_OFF, C_YEL, FILTER_PICKER_LINES,
    right_anchor,
)
from anirss_lib.cli.args import ParsedArgs, parse_cli_args, parse_op_flag, take_client_flag
from anirss_lib.cli.commands import cmd_query, cmd_remove, cmd_sync
from anirss_lib.cli.pickers import (
    ACT_BACK, ACT_CANCEL, ACT_DL_ALL, ACT_DL_PICK, ACT_SUB,
    pick_action, pick_downloads, pick_endpoint, pick_movie,
)
from anirss_lib.cli.urls import (
    UrlKind, classify_url, endpoint_hosts, extract_nyaa_query,
)
from anirss_lib.config import (
    AnirssConfig, CONFIG_PATH, load_config, migrate_config,
)
from anirss_lib import endpoints as endpoints_mod
from anirss_lib.endpoints import Endpoint, EndpointState, load_endpoints
from anirss_lib.errors import AnirssError, ConfigError
from anirss_lib.format import (
    category_chip, colorize_title, format_stats, show_titles,
    source_header_row,
)
from anirss_lib.fzf import fzf_search_prompt
from anirss_lib.logging import die, init_log, log
from anirss_lib.nyaa import FetchError, fetch_rss
from anirss_lib.qbt.actions import (
    do_download, do_movie, do_subscribe, do_upload_local_torrent,
)
from anirss_lib.qbt.clients import download_root, local_files, qbt_client, qbt_clients
from anirss_lib.qbt.feeds import maybe_refresh_feed_cache
from anirss_lib.qbt.refresh import cmd_refresh_on_demand, settle
from anirss_lib.qbt.session import apply_no_seed, login_with_password, login_with_retry
from anirss_lib.readline_input import get_name, setup_readline
from anirss_lib.refine import refine
from anirss_lib import stats as stats_mod
from anirss_lib.titles import show_name
from anirss_lib.types import Item


# ---- small helpers ----

_pick_top_n = stats_mod.top_by_downloads


def _save_base_for(parsed: "ParsedArgs", cfg: "AnirssConfig",
                   ep: Endpoint | None = None) -> str:
    """Pick the destination root: hidden_base when `h` modifier is set
    (e.g. -Sh, -Th), otherwise the client's own save_base, otherwise the
    endpoint's, otherwise the regular one. A client's own comes first: it
    is a path on that client's machine, and an endpoint's need not exist
    there."""
    client = cfg["qbittorrent"]
    if parsed.hidden:
        return download_root(cfg, client, "hidden_base")
    if client.get("save_base"):
        return client["save_base"]
    if ep is not None and ep.save_base:
        return ep.save_base
    return cfg["downloads"]["save_base"]


def _movie_path_for(cfg: "AnirssConfig", ep: Endpoint | None = None) -> str:
    client = cfg["qbittorrent"]
    if client.get("movie_path"):
        return client["movie_path"]
    if ep is not None and ep.movie_path:
        return ep.movie_path
    return cfg["downloads"]["movie_path"]


def _tags_for(cfg: "AnirssConfig", ep: Endpoint | None = None) -> list[str]:
    """Tags stamped on every torrent and RSS rule anirss creates."""
    if ep is not None and ep.tags is not None:
        return list(ep.tags)
    return list(cfg["qbittorrent"].get("tags") or [])


def _resolve_links(links: list[str], endpoints: list[Endpoint]) -> list[str]:
    """Hand qB magnets instead of links that need an on-demand service."""
    if not any(ep.on_demand for ep in endpoints):
        return links
    resolved = ondemand.resolve_links(links, endpoints)
    if resolved != links:
        n = sum(1 for a, b in zip(links, resolved) if a != b)
        print(f"{C_DIM}resolved {n} link(s) to magnets{C_OFF}")
    return resolved


def _settle_subscription(qbt, ep: Endpoint | None, feed_path: str) -> None:
    """An on-demand feed only works while its service is up: give qB the
    chance to fetch it and grab the current matches before we let go."""
    if ep is None or not ep.on_demand:
        return
    print(f"{C_DIM}{ep.name}: letting qBittorrent fetch the feed and grab "
          f"matches...{C_OFF}")
    added = settle(qbt, [feed_path])
    print(f"{C_DIM}{ep.name}: {added} torrent(s) added{C_OFF}")


def _ensure_url_owner(url: str, endpoints: list[Endpoint]) -> Endpoint | None:
    """Start the on-demand endpoint a pasted feed URL belongs to, if any."""
    owner = endpoints_mod.owner_of(endpoints, url)
    if owner is not None and owner.on_demand:
        try:
            ondemand.ensure(owner)
        except FetchError as e:
            die(str(e))
    return owner


def _resolve_password(*, password_stdin: bool) -> str | None:
    """Locate a qBittorrent password without prompting.
    Env var wins; --password-stdin reads one line from stdin; otherwise None.
    """
    pw = os.environ.get("ANIRSS_QBT_PASSWORD")
    if pw:
        return pw
    if password_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\n") or None
    return None


def _login_for(parsed: ParsedArgs, cfg: AnirssConfig):
    """Pick the right login flow based on whether we're non-interactive."""
    if not parsed.non_interactive:
        return login_with_retry(cfg["qbittorrent"])
    pw = _resolve_password(password_stdin=parsed.password_stdin) or ""
    return login_with_password(cfg["qbittorrent"], pw)


def _print_search_rss_rows(query: str, ep: Endpoint,
                           endpoints: list[Endpoint]) -> None:
    """Hidden self-invocation handler called by fzf's reload binding.

    Prints the active endpoint's rows first. While the visible list area
    still has unfilled lines (which covers the empty and the sparse case
    alike), the remaining endpoints are fetched in priority order and
    appended in full under a source-header row each. Rows here are display
    only (the live-search fzf runs --disabled with Enter as an exit key),
    so headers can never be selected."""
    term = terminal.get_size()
    width = max(40, term.columns - 3)
    # fzf runs with --height 90% and its prompt + header eat ~3 lines; only
    # an unfilled remainder triggers appending further sources.
    visible = max(10, int(term.lines * 0.9) - 3)
    printed = 0

    def print_rows(found: list[Item]) -> None:
        nonlocal printed
        # Fill unknown tracker stats from the torrents' trackers (cached, so
        # repeat keystroke pauses cost nothing).
        for item in stats_mod.enrich(found, deadline_s=8.0):
            line = f"{format_stats(item)}  {colorize_title(item.title)}"
            if item.category:
                line = right_anchor(line, category_chip(item), width)
            print(line)
            printed += 1

    try:
        print_rows(endpoints_mod.fetch_items(ep, query, boot=False))
    except FetchError as e:
        print(f"{C_DIM}{e}{C_OFF}")
    for other in (e for e in endpoints if e.name != ep.name):
        if printed >= visible:
            break
        try:
            found = endpoints_mod.fetch_items(other, query, boot=False)
        except FetchError:
            continue
        if found:
            print(source_header_row(other.name, width))
            printed += 1
            print_rows(found)
    if printed == 0 and len(endpoints) > 1:
        names = ", ".join(e.name for e in endpoints)
        print(f"{C_DIM}(no results on {names}){C_OFF}")


def pin_client(cfg: AnirssConfig, argv: list[str], client: str | None) -> None:
    """One qBittorrent for the whole run (`-c NAME`, else the first client):
    every CLI path reads cfg["qbittorrent"], so it becomes that client's
    config. The API server is the exception: its callers pick a client per
    request, so it keeps them all unless -c names one."""
    if client is None and argv[:1] == ["--serve"]:
        return
    pinned = qbt_client(cfg, client)
    # ANIRSS_QBT_PASSWORD is the default client's password. Every CLI path
    # (and a pinned --serve) reads it straight from the environment, so a run
    # pinned to another client drops it rather than send it to that server.
    if pinned["name"] != qbt_clients(cfg)[0]["name"]:
        os.environ.pop("ANIRSS_QBT_PASSWORD", None)
    cfg["qbittorrent"] = pinned


# ---- meta-flag handler ----

def _handle_meta_flags(argv: list[str], cfg: AnirssConfig) -> bool:
    """Returns True if a meta flag was found and handled (caller should return).
    Note: --_search-rss is handled separately in main() before this runs.
    """
    if not argv:
        return False
    a = argv[0]
    if a in ("-h", "--help"):
        print(anirss_lib.__doc__ or "anirss")
        return True
    if a in ("-V", "--version"):
        print(f"anirss {anirss_lib.__version__}")
        return True
    if a in ("--no-seed", "--apply-no-seed"):
        apply_no_seed(cfg["qbittorrent"])
        return True
    if a == "--config":
        print(f"config path: {CONFIG_PATH}")
        print(json.dumps(cfg, indent=2))
        return True
    if a == "--migrate-config":
        migrate_config()
        return True
    if a == "--serve":
        from anirss_lib.server import serve
        serve(argv[1:], cfg)
    if a == "--refresh-on-demand":
        init_log(cfg["logging"]["log_path"])
        cmd_refresh_on_demand(cfg["qbittorrent"], load_endpoints(cfg))
        return True
    return False


# ---- op-flag dispatch (-Q/-S/-R) ----

def _handle_op_flag(argv: list[str], cfg: AnirssConfig, parsed: ParsedArgs
                    ) -> tuple[bool, str | None]:
    """Returns (handled, force_action_menu_url).

    - handled=True means the op flag terminated execution (e.g. -Q, -Sy, -R*).
    - force_action_menu_url is set when `-S <url>` was given and the URL flow
      must show the action menu (interactive case) or be treated as the
      feed URL (non-interactive case).
    """
    if not argv:
        return False, None
    op_parsed = parse_op_flag(argv[0])
    if op_parsed is None:
        return False, None
    op, mods = op_parsed
    rest = argv[1:]
    if op == "Q":
        cmd_query(cfg["qbittorrent"], json_format=("j" in mods))
        return True, None
    if op == "S" and "y" in mods:
        cmd_sync(cfg["qbittorrent"])
        return True, None
    if op == "S":
        if not rest:
            die("usage: anirss -S <rss-url>")
        url = rest[0]
        if classify_url(url) == UrlKind.NOT_URL:
            die(f"-S expects a URL, got {url!r}")
        if "h" in mods:
            parsed.hidden = True
        return False, url
    if op == "R":
        client = cfg["qbittorrent"]
        save_base = download_root(cfg, client, "hidden_base" if "h" in mods
                                  else "save_base")
        cmd_remove(client, args=rest,
                   drop_torrents=("s" in mods),
                   drop_files=("n" in mods),
                   noconfirm=parsed.noconfirm,
                   save_base=save_base,
                   local_files=local_files(client))
        return True, None
    if op == "T":
        if not rest:
            die("usage: anirss -T <torrent-file>")
        if parsed.action_flag_count > 0:
            die("-T cannot be combined with --subscribe / --download / --movie")
        path = rest[0]
        if not path.lower().endswith(".torrent"):
            die(f"-T expects a .torrent file, got {path!r}")
        if not os.path.isfile(path):
            die(f"file not found: {path}")
        parsed.local_torrent_path = path
        if "h" in mods:
            parsed.hidden = True
        return False, None
    return False, None


# ---- interactive search → refine → action state machine ----

def _run_search_state_machine(initial_query: str, cfg: AnirssConfig,
                              eps: EndpointState) -> tuple[str, list[Item], str]:
    """Loops search → fetch → refine → action picker. Returns
    (final_query, final_items, action). Caller proceeds based on `action`.
    Esc at search returns ("", [], ACT_CANCEL).
    """
    last_search_query = initial_query
    items: list[Item] = []
    query: str = ""
    selected: list[Item] = []
    action: str = ACT_CANCEL
    state = "fetch" if initial_query else "search"

    while True:
        # Wipe whatever the previous state left behind so each phase
        # renders into a clean alt-screen frame.
        terminal.clear_screen()
        if state == "search":
            if eps.active.on_demand:
                try:
                    ondemand.ensure(eps.active)
                except FetchError as e:
                    print(f"{C_YEL}{e}{C_OFF}")
            prompt_label = f"{C_YEL}{eps.active.name} >{C_OFF} "
            result, key = fzf_search_prompt(
                prompt_label, default=last_search_query,
                endpoint_name=eps.active.name,
                switch_hint=len(eps.endpoints) > 1,
                slow=eps.active.on_demand)
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
        if state == "fetch":
            print(f"{C_BLD}Query:{C_OFF} "
                  f"{erai.render_query(initial_query, eps.active)}")
            slow = (f" {C_DIM}(can take up to {eps.active.timeout:.0f}s){C_OFF}"
                    if eps.active.on_demand else "")
            print(f"{C_DIM}fetching {eps.active.name}...{C_OFF}{slow}")
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
            # Feeds without tracker stats (erai) get them scraped from the
            # torrents' trackers - what qBittorrent would show after adding.
            selected = stats_mod.enrich(items)
            state = "refine"
            continue
        if state == "refine":
            query, selected, status = refine(query, selected, eps,
                                             cfg["bestfit"])
            if status == "back":
                state = "search"
                continue
            state = "action"
            continue
        # state == "action"
        action = pick_action(len(selected))
        log("INFO", f"action={action}")
        if action == ACT_BACK:
            state = "refine"
            continue
        break

    return query, selected, action


# ---- non-interactive runner ----

def _run_noninteractive(parsed: ParsedArgs, cfg: AnirssConfig, eps: EndpointState,
                        initial_query: str, force_url: str | None) -> None:
    """Execute the chosen action without ever opening fzf or prompting."""
    # `-T <path>` already gave us a validated file - upload it and exit.
    if parsed.local_torrent_path is not None:
        qbt = _login_for(parsed, cfg)
        name = parsed.name or "anirss"
        do_upload_local_torrent(qbt, parsed.local_torrent_path, name,
                                _save_base_for(parsed, cfg), tags=_tags_for(cfg))
        maybe_refresh_feed_cache(qbt)
        log("INFO", "done (non-interactive, -T)")
        print(f"{C_GRN}done.{C_OFF}")
        return

    # Local .torrent file - upload bytes, no nyaa search.
    if force_url is None and initial_query:
        kind = classify_url(initial_query)
        if kind == UrlKind.LOCAL_TORRENT:
            if not os.path.isfile(initial_query):
                die(f"file not found: {initial_query}")
            qbt = _login_for(parsed, cfg)
            name = parsed.name or "anirss"
            do_upload_local_torrent(qbt, initial_query, name,
                                    _save_base_for(parsed, cfg),
                                    tags=_tags_for(cfg))
            maybe_refresh_feed_cache(qbt)
            log("INFO", "done (non-interactive, local torrent)")
            print(f"{C_GRN}done.{C_OFF}")
            return
        if kind == UrlKind.ONE_SHOT:
            qbt = _login_for(parsed, cfg)
            name = parsed.name or "anirss"
            do_download(qbt, [initial_query], name,
                        _save_base_for(parsed, cfg), tags=_tags_for(cfg))
            maybe_refresh_feed_cache(qbt)
            log("INFO", "done (non-interactive, one-shot URL)")
            print(f"{C_GRN}done.{C_OFF}")
            return

    sub_endpoint_name = ""
    must_not_contain = ""
    must_contain = ""
    source: Endpoint | None = None
    if force_url is not None:
        source = _ensure_url_owner(force_url, eps.endpoints)
        try:
            items = fetch_rss(force_url, timeout=source.timeout if source else 10.0)
        except FetchError as e:
            die(str(e))
        if not items:
            die(f"no items returned by {force_url}")
        feed_url_for_sub = force_url
    elif initial_query:
        try:
            items = endpoints_mod.fetch_items(eps.active, initial_query)
        except FetchError as e:
            die(str(e))
        if not items:
            die(f"no results for {initial_query!r}")
        if parsed.subscribe:
            # subscription_feed may cost one probe request (erai per-show
            # resolution), so only the subscribe action pays for it.
            feed_url_for_sub, must_contain, excluded = (
                endpoints_mod.subscription_feed(eps.active, initial_query,
                                                items))
        else:
            feed_url_for_sub, excluded = endpoints_mod.feed_url(eps.active,
                                                                initial_query)
        sub_endpoint_name = eps.active.name
        must_not_contain = "|".join(excluded)
        source = eps.active
    else:
        die("non-interactive action requires a query or -S <url>")

    default_name = parsed.name or show_name(items[0].title) or initial_query or "anirss"
    qbt = _login_for(parsed, cfg)
    save_base = _save_base_for(parsed, cfg, source)
    tags = _tags_for(cfg, source)

    if parsed.subscribe:
        name = do_subscribe(qbt, feed_url_for_sub, default_name, save_base,
                            endpoint_name=sub_endpoint_name,
                            must_not_contain=must_not_contain,
                            must_contain=must_contain,
                            tags=tags)
        if must_not_contain:
            print(f"{C_DIM}exclusions apply via the qB rule (mustNotContain): "
                  f"{must_not_contain}{C_OFF}")
        _settle_subscription(qbt, source, name)
    elif parsed.download_all:
        links = _resolve_links([it.link for it in items], eps.endpoints)
        do_download(qbt, links, default_name, save_base, tags=tags)
    elif parsed.download_n is not None:
        # "Top N" ranks by downloads - scrape real counts first when the
        # feed didn't carry them (otherwise every rank is a fake tie at 0).
        items = stats_mod.enrich(items)
        chosen = _pick_top_n(items, parsed.download_n)
        links = _resolve_links([it.link for it in chosen], eps.endpoints)
        do_download(qbt, links, default_name, save_base, tags=tags)
    elif parsed.movie:
        items = stats_mod.enrich(items)
        top = _pick_top_n(items, 1)[0]
        link = _resolve_links([top.link], eps.endpoints)[0]
        do_movie(qbt, top.title, link, _movie_path_for(cfg, source), tags=tags)

    maybe_refresh_feed_cache(qbt)
    log("INFO", "done (non-interactive)")
    print(f"{C_GRN}done.{C_OFF}")


# ---- interactive runner ----

# Chrome printed *outside* show_titles in the -S URL action-menu flow:
#   1 - "Subscribe URL: …"
#   1 - "fetching feed…"
#   1 - "{N} item(s):"
# Plus the action picker (capped at FILTER_PICKER_LINES).
_URL_FLOW_CHROME_LINES = 3 + FILTER_PICKER_LINES


def _run_interactive(initial_query: str, force_url: str | None,
                     parsed: ParsedArgs, cfg: AnirssConfig,
                     eps: EndpointState) -> None:
    """Two-phase flow:

      Phase 1 (alt screen): search → refine → action picker. Wiped on
        exit so no list/picker debris is left in your scrollback.
      Phase 2 (normal output): login, do_*, single ✓ summary line.

    Cancellation at any picker leaves silently - the absence of a ✓ line
    is the signal that nothing happened. Validation errors that need to
    persist (bad URL, no items) are raised *before* alt screen entry.
    """
    feed_url: str | None = None
    sub_endpoint_name: str = ""
    must_not_contain: str = ""
    must_contain: str = ""
    download_links: list[str] | None = None
    local_torrent_path: str | None = None
    movie_choice: Item | None = None
    default_name: str | None = None
    source: Endpoint | None = None

    # `-T <path>` short-circuits URL handling entirely: path was already
    # validated by _handle_op_flag.
    if parsed.local_torrent_path is not None:
        local_torrent_path = parsed.local_torrent_path
        default_name = parsed.name or "anirss"
        print(f"{C_BLD}Local torrent:{C_OFF} {local_torrent_path}")
    elif force_url is None:
        arg = initial_query
        nyaa_hosts, rss_hosts = endpoint_hosts(eps.endpoints)
        kind = (classify_url(arg, nyaa_hosts=nyaa_hosts, rss_hosts=rss_hosts)
                if arg else UrlKind.NOT_URL)
        if kind == UrlKind.ENDPOINT_RSS:
            # A pasted URL for a configured rss-kind endpoint: treat it as a
            # raw feed, same as `anirss -S <url>`.
            force_url = arg
            kind = UrlKind.NOT_URL
        if kind == UrlKind.OTHER_HTTP:
            die(f"bare URL doesn't match any configured endpoint - use `anirss -S {arg}` to subscribe")
        if kind == UrlKind.NYAA_RSS:
            extracted = extract_nyaa_query(arg)
            if not extracted:
                die("nyaa URL has no `q=` parameter to search with - "
                    "use `anirss -S <url>` to subscribe")
            print(f"{C_DIM}extracted search:{C_OFF} {extracted}")
            initial_query = extracted
            kind = UrlKind.NOT_URL
        if kind == UrlKind.ONE_SHOT:
            download_links = [arg]
            default_name = parsed.name or "anirss"
            print(f"{C_BLD}Direct download:{C_OFF} {arg}")
        elif kind == UrlKind.LOCAL_TORRENT:
            if not os.path.isfile(arg):
                die(f"file not found: {arg}")
            local_torrent_path = arg
            default_name = parsed.name or "anirss"
            print(f"{C_BLD}Local torrent:{C_OFF} {arg}")

    # Phase 1 - gather intent inside the alt screen (only if we still
    # have a UI to show; ONE_SHOT / LOCAL_TORRENT bypassed it above).
    if download_links is None and local_torrent_path is None:
        with terminal.alt_screen():
            if force_url is not None:
                print(f"{C_BLD}Subscribe URL:{C_OFF} {force_url}")
                source = _ensure_url_owner(force_url, eps.endpoints)
                print(f"{C_DIM}fetching feed...{C_OFF}")
                try:
                    items = fetch_rss(force_url,
                                      timeout=source.timeout if source else 10.0)
                except FetchError as e:
                    die(str(e))
                if not items:
                    die(f"no items returned by {force_url}")
                items = stats_mod.enrich(items)
                print(f"{C_BLD}{len(items)} item(s):{C_OFF}")
                show_titles(items, reserved_lines=_URL_FLOW_CHROME_LINES)
                action = pick_action(len(items))
                log("INFO", f"action={action}")
                if action in (ACT_CANCEL, ACT_BACK):
                    return
                default_name = show_name(items[0].title) or force_url
                if action == ACT_SUB:
                    feed_url = force_url
                elif action == ACT_DL_ALL:
                    download_links = [it.link for it in items]
                elif action == ACT_DL_PICK:
                    chosen = pick_downloads(items)
                    if not chosen:
                        return
                    download_links = [it.link for it in chosen]
                    default_name = show_name(chosen[0].title) or default_name
                else:  # ACT_MOVIE
                    movie_choice = pick_movie(items)
                    if movie_choice is None:
                        return
            else:
                query, selected, action = _run_search_state_machine(initial_query, cfg, eps)
                if action == ACT_CANCEL:
                    return
                source = eps.active
                default_name = initial_query or "anirss"
                if selected:
                    default_name = show_name(selected[0].title) or default_name
                if action == ACT_SUB:
                    spec = endpoints_mod.build_subscription(eps.active, query,
                                                            selected)
                    feed_url, must_contain, must_not_contain, sub_endpoint_name = spec
                elif action == ACT_DL_ALL:
                    download_links = [it.link for it in selected]
                elif action == ACT_DL_PICK:
                    chosen = pick_downloads(selected)
                    if not chosen:
                        return
                    download_links = [it.link for it in chosen]
                    default_name = show_name(chosen[0].title) or default_name
                else:  # ACT_MOVIE
                    movie_choice = pick_movie(selected)
                    if movie_choice is None:
                        return

    # Phase 2 - normal screen: name prompt, login, action, summary.
    name = "" if movie_choice is not None else get_name(parsed.name or default_name)
    movie_title_log = repr(movie_choice.title) if movie_choice else None
    n_dl = len(download_links) if download_links else 0
    log("INFO", f"name={name!r} feed={feed_url!r} downloads={n_dl} movie={movie_title_log}")

    qbt = _login_for(parsed, cfg)
    save_base = _save_base_for(parsed, cfg, source)
    tags = _tags_for(cfg, source)
    hidden_tag = f" {C_DIM}(hidden){C_OFF}" if parsed.hidden else ""
    summary: str | None = None
    if feed_url:
        name = do_subscribe(qbt, feed_url, name, save_base,
                            endpoint_name=sub_endpoint_name,
                            must_not_contain=must_not_contain,
                            must_contain=must_contain,
                            tags=tags)
        summary = (f"{C_GRN}✓{C_OFF} Subscribed: {C_BLD}{name}{C_OFF}{hidden_tag}"
                   f"  {C_DIM}(feed: {feed_url}){C_OFF}")
        if must_not_contain:
            summary += (f"\n{C_DIM}exclusions apply via the qB rule "
                       f"(mustNotContain): {must_not_contain}{C_OFF}")
        _settle_subscription(qbt, source, name)
    elif download_links:
        download_links = _resolve_links(download_links, eps.endpoints)
        do_download(qbt, download_links, name, save_base, tags=tags)
        plural = "" if n_dl == 1 else "s"
        summary = (f"{C_GRN}✓{C_OFF} Downloaded {C_BLD}{n_dl}{C_OFF} "
                   f"torrent{plural} → {C_BLD}{name}{C_OFF}{hidden_tag}")
    elif local_torrent_path:
        do_upload_local_torrent(qbt, local_torrent_path, name, save_base, tags=tags)
        summary = (f"{C_GRN}✓{C_OFF} Uploaded "
                   f"{C_BLD}{os.path.basename(local_torrent_path)}{C_OFF}"
                   f" → {C_BLD}{name}{C_OFF}{hidden_tag}")
    elif movie_choice:
        link = _resolve_links([movie_choice.link], eps.endpoints)[0]
        do_movie(qbt, movie_choice.title, link,
                 _movie_path_for(cfg, source), tags=tags)
        summary = f"{C_GRN}✓{C_OFF} Movie: {C_BLD}{movie_choice.title}{C_OFF}"

    maybe_refresh_feed_cache(qbt)
    log("INFO", "done")
    if summary:
        print(summary)


# ---- entry point ----

def main() -> None:
    """CLI entry. Library code raises AnirssError; only here does it become
    the red `error:` line + exit 1 the CLI has always printed."""
    try:
        _main()
    except AnirssError as e:
        die(str(e))


def _main() -> None:
    # Before anything else, so `ps`/`htop`/`pidof` never show a bare "python3".
    proctitle.apply()

    cfg = load_config()
    responsive.set_display(cfg["display"])

    # Hidden: the client names, one per line, for the shell completions.
    if sys.argv[1:2] == ["--_clients"]:
        try:
            print("\n".join(c["name"] for c in qbt_clients(cfg)))
        except ConfigError:
            pass
        return

    argv, client = take_client_flag(sys.argv[1:])

    # --_search-rss is special: stay quiet, no readline, no log init;
    # this fires on every keystroke pause inside the live search picker.
    if argv and argv[0] == "--_search-rss":
        endpoint_list = load_endpoints(cfg)
        if not endpoint_list:
            return
        rest = argv[1:]
        active = endpoint_list[0]
        if len(rest) >= 2 and rest[0] == "--_endpoint":
            active = next((e for e in endpoint_list if e.name == rest[1]), active)
            rest = rest[2:]
        query = " ".join(rest).strip()
        if query:
            _print_search_rss_rows(query, active, endpoint_list)
        return

    # Help and version answer even when a client block is broken.
    if argv[:1] in (["-h"], ["--help"], ["-V"], ["--version"]):
        _handle_meta_flags(argv, cfg)
        return

    pin_client(cfg, argv, client)

    if _handle_meta_flags(argv, cfg):
        return

    endpoint_list = load_endpoints(cfg)

    init_log(cfg["logging"]["log_path"])
    setup_readline()
    log("INFO", f"=== anirss invoked: argv={sys.argv[1:]} ===")

    parsed = parse_cli_args(argv)
    handled, force_url = _handle_op_flag(parsed.positional, cfg, parsed)
    if handled:
        return
    # After the op flags: -Q, -Sy and -R only talk to qBittorrent and must
    # work on a config with no endpoint yet.
    eps = EndpointState(endpoint_list, parsed.endpoint)

    # If -S consumed the URL into force_url, drop -S and the URL from positional.
    if force_url is not None:
        # The first positional was the op flag (`-S`), the second was the URL.
        # Remove both so the rest of positional is empty (no extra args expected).
        parsed.positional = parsed.positional[2:]

    # Same dance for `-T <path>`: the path is already on parsed; drop the
    # two positional tokens so they don't get treated as a search query.
    if parsed.local_torrent_path is not None:
        parsed.positional = parsed.positional[2:]

    initial_query = " ".join(parsed.positional).strip()

    if parsed.non_interactive:
        _run_noninteractive(parsed, cfg, eps, initial_query, force_url)
        return

    _run_interactive(initial_query, force_url, parsed, cfg, eps)
