"""`anirss --play` and `anirss --status`: watching what anirss downloaded in
your own player, and seeing what qBittorrent is fetching now."""

import os
import sys
import time

from anirss_lib import library
from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_YEL
from anirss_lib.errors import AnirssError
from anirss_lib.format import short_home
from anirss_lib.logging import die


def _session(cfg, *, interactive: bool):
    """A qBittorrent session: the usual login when a person is there, else
    only what works without asking (saved session, password, env)."""
    from anirss_lib.qbt.session import login_with_password, login_with_retry
    if interactive:
        return login_with_retry(cfg["qbittorrent"])
    pw = os.environ.get("ANIRSS_QBT_PASSWORD", "")
    return login_with_password(cfg["qbittorrent"], pw, autostart=False, notify=None)


def _roots(cfg) -> dict[str, str]:
    from anirss_lib.qbt.clients import download_root
    client = cfg["qbittorrent"]
    return {k: download_root(cfg, client, k)
            for k in ("save_base", "movie_path", "hidden_base")}


# ---- --play --------------------------------------------------------------------

def _parse(args: list[str]) -> tuple[str, bool]:
    pick = False
    words = []
    for a in args:
        if a == "--pick":
            pick = True
        elif a.startswith("--"):
            die(f"unknown flag for --play: {a}")
        else:
            words.append(a)
    return " ".join(words).strip(), pick


def _incomplete(cfg) -> set[str]:
    """What qBittorrent has not finished, when it can say without asking."""
    from anirss_lib.qbt.actions import list_torrents
    try:
        qbt = _session(cfg, interactive=False)
        torrents = list_torrents(qbt)
    except (AnirssError, OSError):
        return set()
    base = _roots(cfg)["save_base"]
    return library.incomplete_paths(torrents, base)


def cmd_play(cfg, args: list[str]) -> None:
    from anirss_lib.cli.repos import Cancelled, choose
    from anirss_lib.qbt.clients import local_files
    query, pick = _parse(args)
    client = cfg["qbittorrent"]
    if not local_files(client):
        die(f"{client.get('name') or 'this qBittorrent'} keeps its files on another "
            f"machine, so there is nothing here to play. Set local_files = true in its "
            f"[[qbittorrent.client]] block if that folder is mounted here.")
    base = _roots(cfg)["save_base"]
    all_shows = library.shows(base, skip=_incomplete(cfg))
    if not all_shows:
        die(f"nothing downloaded yet in {short_home(base)}. Subscribe to a show or "
            f"download one: anirss <show name>")
    try:
        if query:
            show = library.find_show(all_shows, query)
        else:
            labels = []
            for s in all_shows:
                nxt = library.next_episode(s)
                state = f"next {nxt.label}" if nxt else "caught up"
                labels.append(f"{s.name}  {C_DIM}{len(s.episodes)} episodes · {state}{C_OFF}")
            i = choose(labels, "Pick a show to play")
            if i is None:
                return
            show = all_shows[i]
        ep = library.next_episode(show)
        if pick or (ep is None and not query):
            ep = _pick_episode(show, choose)
            if ep is None:
                return
        elif ep is None:
            last = library.last_played(show)
            print(f"{C_GRN}✓{C_OFF} You're caught up on {C_BLD}{show.name}{C_OFF}"
                  f"{f' (last played: {os.path.basename(last)})' if last else ''}.")
            print(f"  {C_DIM}Pick one to watch again: anirss --play "
                  f"{_quote(show.name)} --pick{C_OFF}")
            return
    except Cancelled:
        return
    player = library.play(ep.path, cfg.get("player", {}).get("command", ""))
    library.mark_played(show, ep)
    print(f"{C_GRN}▶{C_OFF} {C_BLD}{show.name}{C_OFF} {ep.label} in {player}")
    print(f"  {C_DIM}{ep.name}{C_OFF}")
    after = library.next_episode(show)
    if after is not None:
        print(f"  {C_DIM}next time: {after.label}{C_OFF}")


def _pick_episode(show, choose):
    last = library.last_played(show)
    rels = [os.path.relpath(e.path, show.path) for e in show.episodes]
    upto = rels.index(last) if last in rels else -1
    labels = []
    for i, e in enumerate(show.episodes):
        mark = f"{C_GRN}✓{C_OFF}" if i <= upto else (f"{C_YEL}▶{C_OFF}" if i == upto + 1 else " ")
        labels.append(f"{mark} {e.label:<8} {C_DIM}{e.name}{C_OFF}")
    i = choose(labels, f"{show.name}: pick an episode (✓ played, ▶ next)")
    return None if i is None else show.episodes[i]


def _quote(name: str) -> str:
    return f'"{name}"' if " " in name else name


# ---- --status --------------------------------------------------------------------

_STATES = {"stalledDL": "stalled, no peers", "pausedDL": "paused", "stoppedDL": "paused",
           "queuedDL": "queued", "metaDL": "getting its info", "checkingDL": "checking",
           "error": "error", "missingFiles": "files missing", "moving": "moving"}


def _bar(progress: float, width: int = 12) -> str:
    cells = progress * width
    full = int(cells)
    eighths = "▏▎▍▌▋▊▉"
    step = int((cells - full) * 8)
    part = eighths[step - 1] if full < width and step else ""
    return f"{C_GRN}{'█' * full}{part}{C_OFF}{C_DIM}{'·' * (width - full - len(part))}{C_OFF}"


def _eta(seconds: int | None) -> str:
    if not seconds or seconds >= 8640000:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _ago(ts: float, now: float) -> str:
    s = max(0, int(now - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    days = s // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


def cmd_status(cfg, args: list[str]) -> None:
    from anirss_lib import terminal
    from anirss_lib.qbt.actions import _human_bytes, list_torrents
    days = 7.0
    for a in args:
        if a.startswith("--days="):
            try:
                days = float(a.split("=", 1)[1])
            except ValueError:
                die(f"--days takes a number, got {a.split('=', 1)[1]!r}")
        else:
            die(f"unknown flag for --status: {a}")
    qbt = _session(cfg, interactive=sys.stdin.isatty())
    roots = _roots(cfg)
    act = library.activity(list_torrents(qbt), list(roots.values()), days=days)
    now = time.time()
    cols = terminal.get_size().columns
    shown = act.downloading + act.finished[:15]
    show_w = min(24, max((len(library.show_of(t, roots["save_base"]) or "movie")
                          for t in shown), default=5))
    # Downloading rows: bar, percent and state take 41 columns before the name.
    name_w = max(16, min(70, cols - 41 - 2 - show_w - 1))
    done_w = max(16, min(90, cols - 14 - 2 - show_w - 1))
    if act.downloading:
        print(f"{C_BLD}Downloading{C_OFF} {C_DIM}({len(act.downloading)}){C_OFF}")
        for t in act.downloading:
            p = t.get("progress") or 0
            state = _STATES.get(t.get("state") or "")
            if state is None:
                speed = f"{_human_bytes(t.get('dlspeed') or 0)}/s"
                state = f"{speed:>10}  {_eta(t.get('eta')):>7}"
            show = _cut(library.show_of(t, roots["save_base"]) or "movie", show_w)
            print(f"  {_bar(p)} {p * 100:3.0f}%  {state:<19}  "
                  f"{_cut(t.get('name') or '', name_w):<{name_w}}  {C_CYN}{show}{C_OFF}")
    else:
        print(f"{C_DIM}Nothing downloading right now.{C_OFF}")
    print()
    span = "today" if days < 1.01 else f"in the last {days:g} days"
    if act.finished:
        print(f"{C_BLD}Finished {span}{C_OFF} {C_DIM}({len(act.finished)}){C_OFF}")
        for t in act.finished[:15]:
            show = _cut(library.show_of(t, roots["save_base"]) or "movie", show_w)
            print(f"  {C_DIM}{_ago(t.get('completion_on') or 0, now):<11}{C_OFF} "
                  f"{_cut(t.get('name') or '', done_w):<{done_w}}  {C_CYN}{show}{C_OFF}")
        if len(act.finished) > 15:
            print(f"  {C_DIM}and {len(act.finished) - 15} more{C_OFF}")
        print()
        print(f"{C_DIM}Watch the next episode of a show: anirss --play <show>{C_OFF}")
    else:
        print(f"{C_DIM}Nothing finished {span}.{C_OFF}")
