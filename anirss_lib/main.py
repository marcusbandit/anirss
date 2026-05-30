"""Top-level dispatch: argv parsing, state machine, action dispatch."""

import json
import os
import sys

import anirss_lib
from anirss_lib import responsive, terminal
from anirss_lib.ansi import (
    C_BLD, C_DIM, C_GRN, C_OFF, C_YEL, FILTER_PICKER_LINES,
    PROMPT_SEARCH, right_anchor,
)
from anirss_lib.cli.args import ParsedArgs, parse_cli_args, parse_op_flag
from anirss_lib.cli.commands import cmd_query, cmd_remove, cmd_sync
from anirss_lib.cli.pickers import (
    ACT_BACK, ACT_CANCEL, ACT_DL_ALL, ACT_DL_PICK, ACT_MOVIE, ACT_SUB,
    pick_action, pick_downloads, pick_movie,
)
from anirss_lib.cli.urls import UrlKind, classify_url, extract_nyaa_query
from anirss_lib.config import (
    AnirssConfig, CONFIG_PATH, load_config, migrate_config,
)
from anirss_lib.format import (
    category_chip, colorize_title, format_stats, show_titles,
)
from anirss_lib.fzf import fzf_search_prompt
from anirss_lib.logging import die, init_log, log
from anirss_lib.nyaa import _fetch_items_from_url, fetch_items, search_url
from anirss_lib.qbt.actions import (
    do_download, do_movie, do_subscribe, do_upload_local_torrent,
)
from anirss_lib.qbt.feeds import maybe_refresh_feed_cache
from anirss_lib.qbt.session import apply_no_seed, login_with_password, login_with_retry
from anirss_lib.readline_input import get_name, setup_readline
from anirss_lib.refine import refine
from anirss_lib.titles import show_name
from anirss_lib.types import Item


# ---- small helpers ----

def _pick_top_n(items: list[Item], n: int) -> list[Item]:
    """Top-N by downloads, clamped to len(items). Stable for ties."""
    return sorted(items, key=lambda i: i.downloads, reverse=True)[:n]


def _save_base_for(parsed: "ParsedArgs", cfg: "AnirssConfig") -> str:
    """Pick the destination root: hidden_base when `h` modifier is set
    (e.g. -Sh, -Th), otherwise the regular save_base."""
    if parsed.hidden:
        return cfg["downloads"]["hidden_base"]
    return cfg["downloads"]["save_base"]


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


def _print_search_rss_rows(query: str, cfg: AnirssConfig) -> None:
    """Hidden self-invocation handler called by fzf's reload binding."""
    items = fetch_items(query, cfg["search"])
    width = max(40, terminal.get_size().columns - 3)
    for item in items:
        line = f"{format_stats(item)}  {colorize_title(item.title)}"
        if item.category:
            line = right_anchor(line, category_chip(item), width)
        print(line)


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
        save_base = (cfg["downloads"]["hidden_base"] if "h" in mods
                     else cfg["downloads"]["save_base"])
        cmd_remove(cfg["qbittorrent"], args=rest,
                   drop_torrents=("s" in mods),
                   drop_files=("n" in mods),
                   noconfirm=parsed.noconfirm,
                   save_base=save_base)
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

def _run_search_state_machine(initial_query: str, cfg: AnirssConfig
                              ) -> tuple[str, list[Item], str]:
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
            result = fzf_search_prompt(PROMPT_SEARCH, default=last_search_query)
            if result is None:
                return "", [], ACT_CANCEL
            if not result:
                print(f"{C_DIM}(empty — type something or Esc to quit){C_OFF}")
                continue
            last_search_query = result
            initial_query = result
            state = "fetch"
            continue
        if state == "fetch":
            print(f"{C_BLD}Query:{C_OFF} {initial_query}")
            print(f"{C_DIM}fetching nyaa.si...{C_OFF}")
            items = fetch_items(initial_query, cfg["search"])
            if not items:
                print(f"{C_YEL}no results for {initial_query!r} — edit and try again "
                      f"({C_DIM}↑ recalls last query{C_OFF}{C_YEL}){C_OFF}")
                state = "search"
                continue
            query, selected = initial_query, items
            state = "refine"
            continue
        if state == "refine":
            query, selected, status = refine(query, selected, cfg["search"])
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

def _run_noninteractive(parsed: ParsedArgs, cfg: AnirssConfig,
                        initial_query: str, force_url: str | None) -> None:
    """Execute the chosen action without ever opening fzf or prompting."""
    # `-T <path>` already gave us a validated file — upload it and exit.
    if parsed.local_torrent_path is not None:
        qbt = _login_for(parsed, cfg)
        name = parsed.name or "anirss"
        do_upload_local_torrent(qbt, parsed.local_torrent_path, name,
                                _save_base_for(parsed, cfg))
        maybe_refresh_feed_cache(qbt)
        log("INFO", "done (non-interactive, -T)")
        print(f"{C_GRN}done.{C_OFF}")
        return

    # Local .torrent file — upload bytes, no nyaa search.
    if force_url is None and initial_query:
        kind = classify_url(initial_query)
        if kind == UrlKind.LOCAL_TORRENT:
            if not os.path.isfile(initial_query):
                die(f"file not found: {initial_query}")
            qbt = _login_for(parsed, cfg)
            name = parsed.name or "anirss"
            do_upload_local_torrent(qbt, initial_query, name,
                                    _save_base_for(parsed, cfg))
            maybe_refresh_feed_cache(qbt)
            log("INFO", "done (non-interactive, local torrent)")
            print(f"{C_GRN}done.{C_OFF}")
            return
        if kind == UrlKind.ONE_SHOT:
            qbt = _login_for(parsed, cfg)
            name = parsed.name or "anirss"
            do_download(qbt, [initial_query], name,
                        _save_base_for(parsed, cfg))
            maybe_refresh_feed_cache(qbt)
            log("INFO", "done (non-interactive, one-shot URL)")
            print(f"{C_GRN}done.{C_OFF}")
            return

    if force_url is not None:
        items = _fetch_items_from_url(force_url)
        if not items:
            die(f"no items returned by {force_url}")
        feed_url_for_sub = force_url
    elif initial_query:
        items = fetch_items(initial_query, cfg["search"])
        if not items:
            die(f"no results for {initial_query!r}")
        feed_url_for_sub = search_url(initial_query, cfg["search"])
    else:
        die("non-interactive action requires a query or -S <url>")

    default_name = parsed.name or show_name(items[0].title) or initial_query or "anirss"
    qbt = _login_for(parsed, cfg)

    if parsed.subscribe:
        do_subscribe(qbt, feed_url_for_sub, default_name, _save_base_for(parsed, cfg))
    elif parsed.download_all:
        links = [it.link for it in items]
        do_download(qbt, links, default_name, _save_base_for(parsed, cfg))
    elif parsed.download_n is not None:
        chosen = _pick_top_n(items, parsed.download_n)
        links = [it.link for it in chosen]
        do_download(qbt, links, default_name, _save_base_for(parsed, cfg))
    elif parsed.movie:
        top = _pick_top_n(items, 1)[0]
        do_movie(qbt, top.title, top.link, cfg["downloads"]["movie_path"])

    maybe_refresh_feed_cache(qbt)
    log("INFO", "done (non-interactive)")
    print(f"{C_GRN}done.{C_OFF}")


# ---- interactive runner ----

# Chrome printed *outside* show_titles in the -S URL action-menu flow:
#   1 — "Subscribe URL: …"
#   1 — "fetching feed…"
#   1 — "{N} item(s):"
# Plus the action picker (capped at FILTER_PICKER_LINES).
_URL_FLOW_CHROME_LINES = 3 + FILTER_PICKER_LINES


def _run_interactive(initial_query: str, force_url: str | None,
                     parsed: ParsedArgs, cfg: AnirssConfig) -> None:
    """Two-phase flow:

      Phase 1 (alt screen): search → refine → action picker. Wiped on
        exit so no list/picker debris is left in your scrollback.
      Phase 2 (normal output): login, do_*, single ✓ summary line.

    Cancellation at any picker leaves silently — the absence of a ✓ line
    is the signal that nothing happened. Validation errors that need to
    persist (bad URL, no items) are raised *before* alt screen entry.
    """
    feed_url: str | None = None
    download_links: list[str] | None = None
    local_torrent_path: str | None = None
    movie_choice: Item | None = None
    default_name: str | None = None

    # `-T <path>` short-circuits URL handling entirely: path was already
    # validated by _handle_op_flag.
    if parsed.local_torrent_path is not None:
        local_torrent_path = parsed.local_torrent_path
        default_name = parsed.name or "anirss"
        print(f"{C_BLD}Local torrent:{C_OFF} {local_torrent_path}")
    elif force_url is None:
        arg = initial_query
        kind = classify_url(arg) if arg else UrlKind.NOT_URL
        if kind == UrlKind.OTHER_HTTP:
            die(f"bare URL only supported for nyaa.si — use `anirss -S {arg}` to subscribe")
        if kind == UrlKind.NYAA_RSS:
            extracted = extract_nyaa_query(arg)
            if not extracted:
                die("nyaa URL has no `q=` parameter to search with — "
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

    # Phase 1 — gather intent inside the alt screen (only if we still
    # have a UI to show; ONE_SHOT / LOCAL_TORRENT bypassed it above).
    if download_links is None and local_torrent_path is None:
        with terminal.alt_screen():
            if force_url is not None:
                print(f"{C_BLD}Subscribe URL:{C_OFF} {force_url}")
                print(f"{C_DIM}fetching feed...{C_OFF}")
                items = _fetch_items_from_url(force_url)
                if not items:
                    die(f"no items returned by {force_url}")
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
                query, selected, action = _run_search_state_machine(initial_query, cfg)
                if action == ACT_CANCEL:
                    return
                default_name = initial_query or "anirss"
                if selected:
                    default_name = show_name(selected[0].title) or default_name
                if action == ACT_SUB:
                    feed_url = search_url(query, cfg["search"])
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

    # Phase 2 — normal screen: name prompt, login, action, summary.
    name = "" if movie_choice is not None else get_name(parsed.name or default_name)
    movie_title_log = repr(movie_choice.title) if movie_choice else None
    n_dl = len(download_links) if download_links else 0
    log("INFO", f"name={name!r} feed={feed_url!r} downloads={n_dl} movie={movie_title_log}")

    qbt = _login_for(parsed, cfg)
    save_base = _save_base_for(parsed, cfg)
    hidden_tag = f" {C_DIM}(hidden){C_OFF}" if parsed.hidden else ""
    summary: str | None = None
    if feed_url:
        do_subscribe(qbt, feed_url, name, save_base)
        summary = (f"{C_GRN}✓{C_OFF} Subscribed: {C_BLD}{name}{C_OFF}{hidden_tag}"
                   f"  {C_DIM}(feed: {feed_url}){C_OFF}")
    elif download_links:
        do_download(qbt, download_links, name, save_base)
        plural = "" if n_dl == 1 else "s"
        summary = (f"{C_GRN}✓{C_OFF} Downloaded {C_BLD}{n_dl}{C_OFF} "
                   f"torrent{plural} → {C_BLD}{name}{C_OFF}{hidden_tag}")
    elif local_torrent_path:
        do_upload_local_torrent(qbt, local_torrent_path, name, save_base)
        summary = (f"{C_GRN}✓{C_OFF} Uploaded "
                   f"{C_BLD}{os.path.basename(local_torrent_path)}{C_OFF}"
                   f" → {C_BLD}{name}{C_OFF}{hidden_tag}")
    elif movie_choice:
        do_movie(qbt, movie_choice.title, movie_choice.link, cfg["downloads"]["movie_path"])
        summary = f"{C_GRN}✓{C_OFF} Movie: {C_BLD}{movie_choice.title}{C_OFF}"

    maybe_refresh_feed_cache(qbt)
    log("INFO", "done")
    if summary:
        print(summary)


# ---- entry point ----

def main() -> None:
    cfg = load_config()
    responsive.set_display(cfg["display"])

    argv = sys.argv[1:]

    # --_search-rss is special: stay quiet, no readline, no log init —
    # this fires on every keystroke pause inside the live search picker.
    if argv and argv[0] == "--_search-rss":
        query = " ".join(argv[1:]).strip()
        if query:
            _print_search_rss_rows(query, cfg)
        return

    if _handle_meta_flags(argv, cfg):
        return

    init_log(cfg["logging"]["log_path"])
    setup_readline()
    log("INFO", f"=== anirss invoked: argv={sys.argv[1:]} ===")

    parsed = parse_cli_args(argv)
    handled, force_url = _handle_op_flag(parsed.positional, cfg, parsed)
    if handled:
        return

    # If -S consumed the URL into force_url, drop -S and the URL from positional.
    if force_url is not None:
        # The first positional was the op flag (`-S`), the second was the URL.
        # Remove both so the rest of positional is empty (no extra args expected).
        parsed.positional = parsed.positional[2:]

    # Same dance for `-T <path>` — the path is already on parsed; drop the
    # two positional tokens so they don't get treated as a search query.
    if parsed.local_torrent_path is not None:
        parsed.positional = parsed.positional[2:]

    initial_query = " ".join(parsed.positional).strip()

    if parsed.non_interactive:
        _run_noninteractive(parsed, cfg, initial_query, force_url)
        return

    _run_interactive(initial_query, force_url, parsed, cfg)
